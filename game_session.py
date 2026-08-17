import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import cv2
import numpy as np

from smart_automation import StateReadError, VisionGameStateReader


LOGIN_STATES = frozenset(
    {
        "login_provider",
        "account_form",
        "announcement",
        "game_home",
        "server_download_prompt",
        "remote_logout",
    }
)

ANNOUNCEMENT_CLOSE_POINT = (1783, 455)
AGREEMENT_CHECKBOX_POINT = (525, 1412)


class SessionRecoveryError(RuntimeError):
    pass


class GamePeriodError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionSnapshot:
    state: str
    map_name: str | None = None
    coordinate: tuple[int, int] | None = None
    marker: str | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "map": self.map_name,
            "coordinate": list(self.coordinate) if self.coordinate else None,
            "marker": self.marker,
        }


@dataclass(frozen=True)
class GamePeriodObservation:
    period: str | None
    similarity: float
    candidate_box: tuple[int, int, int, int] | None = None

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "similarity": round(self.similarity, 4),
            "candidate_box": list(self.candidate_box) if self.candidate_box else None,
        }


class GameSessionManager:
    """Detect login/session screens and restore the latest server-side save."""

    def __init__(
        self,
        automation,
        *,
        state_reader: VisionGameStateReader | None = None,
        event_logger: Callable[[dict], None] | None = None,
        capture_dir: str | Path = "scheduler_captures",
    ) -> None:
        self.automation = automation
        self.state_reader = state_reader or VisionGameStateReader(automation)
        self.event_logger = event_logger
        self.capture_dir = Path(capture_dir)
        self._lock = threading.RLock()

    def detect(self) -> SessionSnapshot:
        with self._lock:
            observations = self.state_reader.observations()
        game_state = self._parse_game_state(observations)
        texts = [self._normalized(item.text) for item in observations]

        marker = self._matching_text(
            texts,
            (
                "异地登录",
                "其他设备登录",
                "账号已在其他设备",
                "登录状态失效",
                "登录已过期",
                "重新登录",
                "被迫下线",
            ),
        )
        if marker:
            return SessionSnapshot("remote_logout", marker=marker)

        marker = self._matching_text(texts, ("强制下载存档", "服务器存档"))
        if marker and (
            self._matching_text(texts, ("其他设备", "设备登出", "更新版本存档"))
            or "强制下载存档" in marker
        ):
            return SessionSnapshot("server_download_prompt", marker="server_save_newer")

        if game_state.map_name and game_state.coordinate:
            return SessionSnapshot(
                "in_game",
                map_name=game_state.map_name,
                coordinate=game_state.coordinate,
                marker="map_coordinate",
            )

        if self._matching_text(texts, ("登录游戏",)) and self._matching_text(
            texts, ("游戏账号", "游戏密码")
        ):
            return SessionSnapshot("account_form", marker="account_login_form")

        if self._matching_text(texts, ("更新公告",)):
            return SessionSnapshot("announcement", marker="update_announcement")

        if self._matching_text(texts, ("开始游戏",)):
            return SessionSnapshot("game_home", marker="start_game")

        if self._matching_text(texts, ("账号登录",)) and self._matching_text(
            texts, ("QQ登录", "微信登录", "Apple登录")
        ):
            return SessionSnapshot("login_provider", marker="login_provider_picker")

        return SessionSnapshot("unknown")

    def recover_latest_server_save(
        self,
        *,
        force: bool = False,
        timeout: float = 75.0,
    ) -> dict:
        """Reach an in-game map without ever selecting a local upload."""
        started = time.monotonic()
        last_state = None
        stagnant_steps = 0
        self._emit({"event": "session_recovery_started", "force": bool(force)})

        for _step in range(16):
            if time.monotonic() - started > max(10.0, float(timeout)):
                break
            try:
                snapshot = self.detect()
            except StateReadError as exc:
                if force:
                    return {"state": "forced_unknown", "error": repr(exc)}
                self._fail(f"cannot read the game session: {exc}")

            self._emit({"event": "session_state_detected", **snapshot.as_dict()})
            if snapshot.state == "in_game":
                self._emit({"event": "session_recovery_completed", **snapshot.as_dict()})
                return snapshot.as_dict()

            if snapshot.state == last_state:
                stagnant_steps += 1
            else:
                stagnant_steps = 0
                last_state = snapshot.state

            if stagnant_steps >= 3:
                self._fail(f"session recovery did not leave state {snapshot.state}")

            if snapshot.state == "login_provider":
                self._click_text("账号登录", exact=False)
                self._action("open_account_login")
            elif snapshot.state == "account_form":
                self._click_text("登录", exact=True, region=(0.48, 0.68, 0.76, 0.84))
                self._action("submit_saved_account")
            elif snapshot.state == "announcement":
                self._click_point(ANNOUNCEMENT_CLOSE_POINT)
                self._action("close_announcement")
            elif snapshot.state == "game_home":
                if not self.agreement_checked():
                    self._click_point(AGREEMENT_CHECKBOX_POINT)
                    self._action("accept_login_agreement")
                    self._sleep(0.5)
                self._click_text("开始游戏", exact=True)
                self._action("start_game")
            elif snapshot.state == "server_download_prompt":
                self._click_text("确认", exact=True, region=(0.50, 0.55, 0.78, 0.76))
                self._action("confirm_latest_server_save")
            elif snapshot.state == "remote_logout":
                try:
                    self._click_text("确认", exact=True)
                    self._action("acknowledge_remote_logout")
                except (StateReadError, RuntimeError):
                    self._fail("remote logout was detected but its confirmation control is unreadable")
            elif force:
                return {"state": "forced_unknown"}
            else:
                self._fail("the game is not on a recognized login or in-game screen")

            self._sleep(2.0 if snapshot.state != "server_download_prompt" else 8.0)

        self._fail("session recovery timed out")

    def agreement_checked(self) -> bool:
        image = self.automation.capture_image()
        if image is None:
            raise SessionRecoveryError("cannot capture the agreement checkbox")
        rgb = np.asarray(image.convert("RGB"))
        height, width = rgb.shape[:2]
        crop = rgb[
            round(height * 0.83) : round(height * 0.90),
            round(width * 0.20) : round(width * 0.32),
        ]
        hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
        orange = cv2.inRange(hsv, np.array([0, 80, 40]), np.array([30, 255, 255]))
        return int(np.count_nonzero(orange)) >= 150

    def _click_text(self, text: str, *, exact: bool, region=None) -> None:
        with self._lock:
            observation = self.state_reader.find_text(text, exact=exact, region=region)
        point = (
            round(observation.center_x * self.automation.reference_width),
            round(observation.top_center_y * self.automation.reference_height),
        )
        self._click_point(point)

    def _click_point(self, point) -> None:
        self.automation.focus_window(settle_seconds=0.0)
        self._sleep(0.25)
        self.automation.click_reference(point)

    def _sleep(self, seconds: float) -> None:
        stop_event = getattr(self.automation, "stop_event", None)
        if stop_event is not None:
            if stop_event.wait(max(0.0, float(seconds))):
                raise KeyboardInterrupt("Scheduler stop requested during session recovery.")
            return
        time.sleep(max(0.0, float(seconds)))

    def _action(self, action: str) -> None:
        self._emit({"event": "session_recovery_action", "action": action})

    def _fail(self, message: str):
        capture = None
        try:
            capture = self.automation.capture_screenshot(self.capture_dir, "session_recovery_failure")
        except Exception:
            pass
        self._emit(
            {
                "event": "session_recovery_failed",
                "error": message,
                "capture": str(capture) if capture else None,
            }
        )
        raise SessionRecoveryError(message)

    def _emit(self, event: dict) -> None:
        if self.event_logger:
            self.event_logger(event)

    @staticmethod
    def _normalized(text: str) -> str:
        return "".join(str(text).split()).replace("⑨", "").replace("④", "")

    @staticmethod
    def _matching_text(texts: list[str], needles: tuple[str, ...]) -> str | None:
        return next((text for text in texts if any(needle in text for needle in needles)), None)

    @staticmethod
    def _parse_game_state(observations):
        from smart_automation import parse_game_state

        return parse_game_state(observations)


_ZI_GLYPH_ROWS = (
    "...........##....#######............",
    "........###################.........",
    "........####################........",
    "........####################........",
    "........#######....#########........",
    "........#####...############........",
    "...............##########...........",
    "..##################################",
    ".###################################",
    "####################################",
    "###################################.",
    "#############.##.#################..",
    "####.............######.......#.....",
    "###..............######.............",
    "..................#####.............",
    ".................######.............",
    ".......##........######.............",
    ".......################.............",
    "......################..............",
    ".......###############..............",
    "...........##########...............",
    "............########................",
)


class GamePeriodReader:
    """Recognize the cyan current-period glyph in the stable right sidebar."""

    def __init__(self, automation, *, min_similarity: float = 0.65) -> None:
        self.automation = automation
        self.min_similarity = max(0.0, min(1.0, float(min_similarity)))
        self._template = self._normalized_mask(
            np.array([[255 if value == "#" else 0 for value in row] for row in _ZI_GLYPH_ROWS], dtype=np.uint8)
        )

    def read(self) -> GamePeriodObservation:
        image = self.automation.capture_image()
        if image is None:
            raise GamePeriodError("cannot capture the game period")
        rgb = np.asarray(image.convert("RGB"))
        height, width = rgb.shape[:2]
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        cyan = cv2.inRange(hsv, np.array([70, 20, 20]), np.array([130, 255, 255]))
        _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(cyan)
        candidates = []
        for raw in stats[1:]:
            x, y, item_width, item_height, area = (int(value) for value in raw)
            if not width * 0.86 <= x <= width * 0.99:
                continue
            if not height * 0.295 <= y <= height * 0.33:
                continue
            if not width * 0.009 <= item_width <= width * 0.03:
                continue
            if not height * 0.008 <= item_height <= height * 0.03:
                continue
            if area < max(40, round(width * height * 0.00004)):
                continue
            candidates.append((area, x, y, item_width, item_height))

        if not candidates:
            return GamePeriodObservation(None, 0.0)

        _area, x, y, item_width, item_height = max(candidates)
        candidate = self._normalized_mask(cyan[y : y + item_height, x : x + item_width])
        intersection = int(np.count_nonzero(np.logical_and(candidate, self._template)))
        union = int(np.count_nonzero(np.logical_or(candidate, self._template)))
        similarity = intersection / union if union else 0.0
        period = "子" if similarity >= self.min_similarity else None
        return GamePeriodObservation(period, similarity, (x, y, item_width, item_height))

    def wait_for_zi(
        self,
        *,
        timeout: float = 8.0,
        stable_samples: int = 2,
        poll_seconds: float = 0.2,
    ) -> GamePeriodObservation:
        started = time.monotonic()
        consecutive = 0
        last = GamePeriodObservation(None, 0.0)
        while time.monotonic() - started <= max(0.1, float(timeout)):
            last = self.read()
            if last.period == "子":
                consecutive += 1
                if consecutive >= max(1, int(stable_samples)):
                    return last
            else:
                consecutive = 0
            self.automation.wait_seconds(max(0.01, float(poll_seconds)))
        raise GamePeriodError(
            f"子时 verification failed (best/current similarity {last.similarity:.3f}, "
            f"required {self.min_similarity:.3f})"
        )

    @staticmethod
    def _normalized_mask(mask: np.ndarray) -> np.ndarray:
        height, width = mask.shape[:2]
        canvas = np.zeros((64, 64), dtype=np.uint8)
        if not width or not height:
            return canvas
        scale = min(56.0 / width, 56.0 / height)
        resized_width = max(1, round(width * scale))
        resized_height = max(1, round(height * scale))
        resized = cv2.resize(
            mask,
            (resized_width, resized_height),
            interpolation=cv2.INTER_NEAREST,
        )
        top = (64 - resized_height) // 2
        left = (64 - resized_width) // 2
        canvas[top : top + resized_height, left : left + resized_width] = resized
        return canvas


class SessionWatchdog:
    def __init__(
        self,
        manager: GameSessionManager,
        runtime_control,
        *,
        interval_seconds: float = 15.0,
        event_logger: Callable[[dict], None] | None = None,
    ) -> None:
        self.manager = manager
        self.runtime_control = runtime_control
        self.interval_seconds = max(2.0, float(interval_seconds))
        self.event_logger = event_logger
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="game-session-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set() and not self.runtime_control.stop_event.is_set():
            if self.runtime_control.is_paused:
                if self._stop_event.wait(self.interval_seconds):
                    break
                continue
            try:
                snapshot = self.manager.detect()
                self.runtime_control.set_context(session_state=snapshot.state)
                if snapshot.state in LOGIN_STATES:
                    changed = self.runtime_control.request_pause(
                        reason=f"session_{snapshot.state}",
                        source="session_watchdog",
                        details=snapshot.as_dict(),
                    )
                    if changed:
                        self._emit({"event": "session_logout_detected", **snapshot.as_dict()})
            except Exception as exc:
                self._emit({"event": "session_watchdog_read_failed", "error": repr(exc)})
            if self._stop_event.wait(self.interval_seconds):
                break

    def _emit(self, event: dict) -> None:
        if self.event_logger:
            self.event_logger(event)
