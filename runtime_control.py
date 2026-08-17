import copy
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from input_takeover import MouseTakeoverDetector


RUNTIME_CONTROL_SCHEMA_VERSION = 3
CONTINUE_STEP = "continue_step"
RESTART_TASK = "restart_task"
RESUME_MODES = frozenset({CONTINUE_STEP, RESTART_TASK})
RESUMABLE_PHASES = frozenset(
    {
        "route_start",
        "before_action",
        "after_action",
        "between_routes",
    }
)

_MODIFIER_KEY_NAMES = frozenset(
    {
        "Key.alt",
        "Key.alt_l",
        "Key.alt_r",
        "Key.alt_gr",
        "Key.cmd",
        "Key.cmd_l",
        "Key.cmd_r",
        "Key.ctrl",
        "Key.ctrl_l",
        "Key.ctrl_r",
        "Key.shift",
        "Key.shift_l",
        "Key.shift_r",
    }
)


class ResumeValidationError(RuntimeError):
    pass


class AutomationRecoveryHandoff(BaseException):
    """Leave the interrupted call stack so the scheduler consumes one recovery ticket."""

    def __init__(self, mode: str) -> None:
        super().__init__(mode)
        self.mode = mode


class RuntimeControl:
    """Coordinate human-input pausing, checkpoints, and safe resumption."""

    def __init__(
        self,
        *,
        stop_event: threading.Event,
        state_file: str | Path = "runtime_control.json",
        resume_hotkey: str = "<ctrl>+<alt>+r",
        restart_hotkey: str = "<ctrl>+<alt>+<shift>+r",
        pause_hotkey: str = "<ctrl>+<alt>+p",
        resume_delay_seconds: float = 3.0,
        coordinate_tolerance: int = 0,
        detect_human_input: bool = True,
        input_pause_policy: str = "gesture",
        takeover_window_seconds: float = 3.0,
        takeover_required_seconds: float = 2.0,
        takeover_max_gap_seconds: float = 0.30,
        takeover_min_distance_pixels: float = 1.0,
        takeover_hud=None,
        synthetic_grace_seconds: float = 0.2,
        event_logger: Callable[[dict], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stop_event = stop_event
        self.state_file = Path(state_file)
        self.resume_hotkey = str(resume_hotkey)
        self.restart_hotkey = str(restart_hotkey)
        self.pause_hotkey = str(pause_hotkey)
        self.resume_delay_seconds = max(0.0, float(resume_delay_seconds))
        self.coordinate_tolerance = max(0, int(coordinate_tolerance))
        self.detect_human_input = bool(detect_human_input)
        self.input_pause_policy = str(input_pause_policy)
        if self.input_pause_policy not in {"gesture", "immediate"}:
            raise ValueError("input_pause_policy must be 'gesture' or 'immediate'")
        self.synthetic_grace_seconds = max(0.0, float(synthetic_grace_seconds))
        self.event_logger = event_logger
        self._monotonic = monotonic

        self.pause_event = threading.Event()
        self.resume_requested = threading.Event()
        self._lock = threading.RLock()
        self._context: dict = {"phase": "idle"}
        self._checkpoint: dict | None = None
        self._resume_checkpoint: dict | None = None
        self._resume_request: dict | None = None
        self._resume_in_progress = False
        self._paused_at: datetime | None = None
        self._pause_reason: str | None = None
        self._pause_source: str | None = None
        self._total_pause_seconds = 0.0
        self._last_user_input_monotonic = 0.0
        self._synthetic_depth = 0
        self._ignore_input_until = 0.0
        self._state_provider: Callable[[], object] | None = None
        self._continue_validator: Callable[[Mapping], None] | None = None
        self._resume_preparer: Callable[..., Mapping | None] | None = None
        self._listeners: list[object] = []
        self._gesture_detector = MouseTakeoverDetector(
            window_seconds=takeover_window_seconds,
            required_seconds=takeover_required_seconds,
            max_gap_seconds=takeover_max_gap_seconds,
            min_distance_pixels=takeover_min_distance_pixels,
        )
        self._takeover_pending = threading.Event()
        self._gesture_monitor_stop = threading.Event()
        self._gesture_monitor_thread: threading.Thread | None = None
        self._takeover_state_lock = threading.RLock()
        self._last_takeover_update = float("-inf")
        self._gesture_announced = False
        self._takeover_hold_started: float | None = None
        self._takeover_progress = self._gesture_detector.poll(now=self._monotonic())
        self._takeover_hud = takeover_hud
        self._load()

    @property
    def is_paused(self) -> bool:
        return self.pause_event.is_set()

    @property
    def total_pause_seconds(self) -> float:
        with self._lock:
            total = self._total_pause_seconds
            if self.pause_event.is_set() and self._paused_at:
                total += max(0.0, (datetime.now() - self._paused_at).total_seconds())
            if self._takeover_hold_started is not None:
                total += max(0.0, self._monotonic() - self._takeover_hold_started)
            return total

    def set_state_provider(self, provider: Callable[[], object] | None) -> None:
        self._state_provider = provider

    def set_continue_validator(self, validator: Callable[[Mapping], None] | None) -> None:
        self._continue_validator = validator

    def set_resume_preparer(self, preparer: Callable[..., Mapping | None] | None) -> None:
        """Set the preflight used before issuing a from-the-beginning recovery ticket."""
        self._resume_preparer = preparer

    def set_context(self, **values) -> None:
        with self._lock:
            self._context.update({key: value for key, value in values.items() if value is not None})
            self._context["updated_at"] = datetime.now().isoformat(timespec="milliseconds")
            if self.pause_event.is_set() and self._checkpoint is not None:
                self._checkpoint.update(copy.deepcopy(self._context))
                self._save_locked()

    def replace_context(self, **values) -> None:
        with self._lock:
            self._context = {key: value for key, value in values.items() if value is not None}
            self._context["updated_at"] = datetime.now().isoformat(timespec="milliseconds")
            if self.pause_event.is_set() and self._checkpoint is not None:
                preserved = {
                    key: value
                    for key, value in self._checkpoint.items()
                    if key in {"map", "coordinate", "state_captured_at", "paused_at", "reason", "source", "input"}
                }
                self._checkpoint = {**copy.deepcopy(self._context), **preserved}
                self._save_locked()

    def before_action(
        self,
        *,
        route: str,
        route_index: int,
        total_routes: int,
        action_index: int,
        total_actions: int,
        action_type: str,
        action_label: str | None,
    ) -> None:
        self.set_context(
            route=route,
            route_index=route_index,
            total_routes=total_routes,
            action_index=action_index,
            next_action_index=action_index,
            total_actions=total_actions,
            action_type=action_type,
            action_label=action_label,
            phase="before_action",
        )

    def action_dispatched(self, action_index: int, dispatched_at: datetime | None = None) -> None:
        self.set_context(
            action_index=action_index,
            next_action_index=action_index + 1,
            phase="after_action",
            last_dispatched_at=(
                dispatched_at.isoformat(timespec="milliseconds") if dispatched_at else datetime.now().isoformat()
            ),
        )

    def request_pause(
        self,
        *,
        reason: str,
        source: str = "human_input",
        details: Mapping | None = None,
    ) -> bool:
        now = datetime.now()
        with self._lock:
            self._last_user_input_monotonic = self._monotonic()
            if self.stop_event.is_set() or self.pause_event.is_set():
                return False
            self._paused_at = now
            self._pause_reason = str(reason)
            self._pause_source = str(source)
            self._checkpoint = copy.deepcopy(self._context)
            self._checkpoint.update(
                {
                    "paused_at": now.isoformat(timespec="milliseconds"),
                    "reason": self._pause_reason,
                    "source": self._pause_source,
                    "input": dict(details or {}),
                }
            )
            self.pause_event.set()
            self._save_locked()
        self._clear_takeover(show_paused=source in {"human_input", "hotkey"})
        self._emit(
            {
                "event": "automation_paused",
                "reason": reason,
                "source": source,
                "checkpoint": self.pending_checkpoint(),
            }
        )
        print(f"Automation paused ({source}: {reason}).")
        return True

    def request_resume(
        self,
        *,
        source: str = "resume_hotkey",
        force: bool = False,
        mode: str = CONTINUE_STEP,
    ) -> bool:
        mode = RESTART_TASK if force else str(mode)
        if mode not in RESUME_MODES:
            raise ValueError(f"unknown resume mode: {mode}")
        with self._lock:
            if (
                not self.pause_event.is_set()
                or self.stop_event.is_set()
                or self.resume_requested.is_set()
                or self._resume_in_progress
            ):
                return False
            requested_at = self._monotonic()
            # Ignore the remaining key events from the resume chord itself.
            self._ignore_input_until = max(self._ignore_input_until, requested_at + 0.5)
            self._resume_request = {
                "source": str(source),
                "force": bool(force),
                "mode": mode,
                "requested_at": requested_at,
            }
            self.resume_requested.set()
        self._emit(
            {
                "event": "automation_resume_requested",
                "source": source,
                "force": bool(force),
                "mode": mode,
            }
        )
        return True

    def pending_checkpoint(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._checkpoint)

    def consume_resume_checkpoint(self) -> dict | None:
        with self._lock:
            checkpoint = copy.deepcopy(self._resume_checkpoint)
            self._resume_checkpoint = None
            self._save_locked()
            return checkpoint

    def peek_resume_checkpoint(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._resume_checkpoint)

    def clear_checkpoint(self) -> None:
        with self._lock:
            if self.pause_event.is_set():
                return
            self._checkpoint = None
            self._resume_checkpoint = None
            self._save_locked()

    def status_snapshot(self) -> dict:
        with self._lock:
            active_pause = 0.0
            if self.pause_event.is_set() and self._paused_at:
                active_pause = max(0.0, (datetime.now() - self._paused_at).total_seconds())
            active_takeover_hold = (
                max(0.0, self._monotonic() - self._takeover_hold_started)
                if self._takeover_hold_started is not None
                else 0.0
            )
            return {
                "paused": self.pause_event.is_set(),
                "reason": self._pause_reason,
                "source": self._pause_source,
                "paused_at": self._paused_at.isoformat(timespec="milliseconds") if self._paused_at else None,
                "active_pause_seconds": round(active_pause, 3),
                "total_pause_seconds": round(
                    self._total_pause_seconds + active_pause + active_takeover_hold,
                    3,
                ),
                "resume_hotkey": self.resume_hotkey,
                "restart_hotkey": self.restart_hotkey,
                "pause_hotkey": self.pause_hotkey,
                "resume_policy": "explicit_continue_or_restart",
                "resume_modes": sorted(RESUME_MODES),
                "input_pause_policy": self.input_pause_policy,
                "resume_pending": self.resume_requested.is_set() or self._resume_in_progress,
                "takeover": {
                    **self._takeover_progress.as_dict(),
                    "hold_seconds": round(active_takeover_hold, 3),
                },
                "checkpoint": copy.deepcopy(self._checkpoint),
                "context": copy.deepcopy(self._context),
            }

    @contextmanager
    def automation_input(self):
        with self._lock:
            self._synthetic_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._synthetic_depth = max(0, self._synthetic_depth - 1)
                self._ignore_input_until = max(
                    self._ignore_input_until,
                    self._monotonic() + self.synthetic_grace_seconds,
                )

    def wait(self, seconds: float) -> None:
        """Wait for active automation time; human-paused time does not consume the delay."""
        remaining = max(0.0, float(seconds))
        self._raise_if_stopped()
        self.wait_if_paused()
        while remaining > 0:
            started = self._monotonic()
            if self.stop_event.wait(min(0.1, remaining)):
                self._raise_if_stopped()
            elapsed = max(0.0, self._monotonic() - started)
            remaining = max(0.0, remaining - elapsed)
            self.wait_if_paused()

    def wait_until(self, moment: datetime) -> float:
        """Wait for a wall-clock deadline and return only actively waited seconds."""
        active_wait = 0.0
        while True:
            self._raise_if_stopped()
            self.wait_if_paused()
            remaining = (moment - datetime.now()).total_seconds()
            if remaining <= 0:
                return active_wait
            started = self._monotonic()
            if self.stop_event.wait(min(0.1, remaining)):
                self._raise_if_stopped()
            active_wait += max(0.0, self._monotonic() - started)

    def wait_if_paused(self) -> float:
        self._wait_if_takeover_pending()
        if not self.pause_event.is_set():
            self._raise_if_stopped()
            return 0.0

        self._capture_checkpoint_state()
        entered = self._monotonic()
        accepted_mode = None
        while self.pause_event.is_set():
            self._raise_if_stopped()
            if not self.resume_requested.wait(0.1):
                continue
            with self._lock:
                request = dict(self._resume_request or {})
                self._resume_request = None
                self.resume_requested.clear()
                self._resume_in_progress = bool(request)
            if not request:
                continue

            force = bool(request.get("force"))
            source = str(request.get("source", "unknown"))
            mode = str(request.get("mode", CONTINUE_STEP))
            try:
                if mode == CONTINUE_STEP:
                    current_state = self._validate_resume_state()
                elif self._resume_preparer is not None:
                    prepared = self._resume_preparer(
                        force=force,
                        checkpoint=self.pending_checkpoint() or {},
                    )
                    current_state = dict(prepared or {})
                else:
                    current_state = None if force else self._validate_restart_state()
            except Exception as exc:
                self._finish_resume_attempt()
                print(f"Resume rejected: {exc}")
                self._emit(
                    {
                        "event": "automation_resume_rejected",
                        "source": source,
                        "reason": str(exc),
                        "checkpoint": self.pending_checkpoint(),
                    }
                )
                continue

            requested_at = float(request.get("requested_at", self._monotonic()))
            self._wait_resume_delay()
            with self._lock:
                new_input = self._last_user_input_monotonic > requested_at
            if new_input and not force:
                self._finish_resume_attempt()
                message = "human input was detected during the resume countdown"
                print(f"Resume rejected: {message}.")
                self._emit({"event": "automation_resume_rejected", "source": source, "reason": message})
                continue
            self._finish_resume(
                source=source,
                force=force,
                mode=mode,
                current_state=current_state,
            )
            accepted_mode = mode

        if accepted_mode is not None:
            raise AutomationRecoveryHandoff(accepted_mode)
        return max(0.0, self._monotonic() - entered)

    def start_listeners(self, extra_hotkeys: Mapping[str, Callable[[], object]] | None = None) -> None:
        if self._listeners:
            return
        from pynput import keyboard, mouse

        callbacks = {
            self.resume_hotkey: lambda: self.request_resume(
                source="resume_hotkey",
                mode=CONTINUE_STEP,
            ),
            self.restart_hotkey: lambda: self.request_resume(
                source="restart_hotkey",
                mode=RESTART_TASK,
            ),
            self.pause_hotkey: lambda: self.request_pause(
                reason="pause_hotkey",
                source="hotkey",
                details={"hotkey": self.pause_hotkey},
            ),
            **dict(extra_hotkeys or {}),
        }
        hotkeys = [keyboard.HotKey(keyboard.HotKey.parse(keys), callback) for keys, callback in callbacks.items()]
        keyboard_listener = None

        def on_press(key) -> None:
            canonical = keyboard_listener.canonical(key)
            for hotkey in hotkeys:
                hotkey.press(canonical)
            if not self.stop_event.is_set():
                self._on_key_press(key)

        def on_release(key) -> None:
            canonical = keyboard_listener.canonical(key)
            for hotkey in hotkeys:
                hotkey.release(canonical)

        # macOS can abort when several pynput keyboard event taps are created in
        # one process. One listener dispatches resume, stop, and human input.
        keyboard_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listeners = [keyboard_listener]
        if self.detect_human_input:
            self._listeners.append(
                mouse.Listener(
                    on_move=self._on_mouse_move,
                    on_click=self._on_mouse_click,
                    on_scroll=self._on_mouse_scroll,
                )
            )
        for listener in self._listeners:
            listener.start()
        if self.detect_human_input and self.input_pause_policy == "gesture":
            self._start_gesture_monitor()
            self._hud_call("start")

    def stop_listeners(self) -> None:
        self._gesture_monitor_stop.set()
        if self._gesture_monitor_thread and self._gesture_monitor_thread.is_alive():
            self._gesture_monitor_thread.join(timeout=1.0)
        self._gesture_monitor_thread = None
        self._clear_takeover()
        self._hud_call("stop")
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            listener.stop()
        for listener in listeners:
            if listener.is_alive():
                listener.join(timeout=1.0)

    def _on_key_press(self, key) -> None:
        if str(key) in _MODIFIER_KEY_NAMES:
            return
        if self.input_pause_policy == "immediate":
            self._handle_physical_input("keyboard", {"key": str(key)})
        else:
            self._note_physical_input()

    def _on_mouse_move(self, x, y) -> None:
        details = {"x": int(x), "y": int(y)}
        if self.input_pause_policy == "immediate":
            self._handle_physical_input("mouse_move", details)
            return
        now = self._monotonic()
        if not self._note_physical_input(now=now) or self.pause_event.is_set():
            return
        progress = self._gesture_detector.feed(x, y, now=now)
        self._update_takeover(progress)
        if progress.triggered:
            self.request_pause(
                reason="mouse_takeover_gesture",
                source="human_input",
                details={**details, **progress.as_dict()},
            )

    def _on_mouse_click(self, x, y, button, pressed) -> None:
        if pressed:
            details = {"x": int(x), "y": int(y), "button": str(button)}
            if self.input_pause_policy == "immediate":
                self._handle_physical_input("mouse_click", details)
            else:
                self._note_physical_input()

    def _on_mouse_scroll(self, x, y, dx, dy) -> None:
        details = {"x": int(x), "y": int(y), "dx": int(dx), "dy": int(dy)}
        if self.input_pause_policy == "immediate":
            self._handle_physical_input("mouse_scroll", details)
        else:
            self._note_physical_input()

    def _handle_physical_input(self, input_type: str, details: Mapping) -> None:
        now = self._monotonic()
        if not self._note_physical_input(now=now):
            return
        if not self.pause_event.is_set():
            self.request_pause(reason=input_type, source="human_input", details=details)

    def _note_physical_input(self, *, now: float | None = None) -> bool:
        now = self._monotonic() if now is None else float(now)
        with self._lock:
            if self._synthetic_depth or now <= self._ignore_input_until:
                return False
            self._last_user_input_monotonic = now
        return True

    def _start_gesture_monitor(self) -> None:
        if self._gesture_monitor_thread and self._gesture_monitor_thread.is_alive():
            return
        self._gesture_monitor_stop.clear()
        self._gesture_monitor_thread = threading.Thread(
            target=self._gesture_monitor_loop,
            name="mouse-takeover-monitor",
            daemon=True,
        )
        self._gesture_monitor_thread.start()

    def _gesture_monitor_loop(self) -> None:
        while not self._gesture_monitor_stop.wait(0.05) and not self.stop_event.is_set():
            if self.pause_event.is_set():
                continue
            progress = self._gesture_detector.poll(now=self._monotonic())
            self._update_takeover(progress)

    def _update_takeover(self, progress) -> None:
        with self._takeover_state_lock:
            if progress.observed_at < self._last_takeover_update:
                return
            self._last_takeover_update = progress.observed_at
            self._takeover_progress = progress
            if progress.active and not progress.triggered:
                with self._lock:
                    if self._takeover_hold_started is None:
                        self._takeover_hold_started = self._monotonic()
                self._takeover_pending.set()
                self._hud_call("show_progress", progress, pause_hotkey=self.pause_hotkey)
                if not self._gesture_announced:
                    self._gesture_announced = True
                    self._emit({"event": "automation_takeover_gesture_started", **progress.as_dict()})
                return
            if not progress.active and self._takeover_pending.is_set():
                hold_seconds = self._finish_takeover_hold()
                self._takeover_pending.clear()
                self._hud_call("hide")
                if self._gesture_announced:
                    self._emit(
                        {
                            "event": "automation_takeover_gesture_cancelled",
                            "hold_seconds": round(hold_seconds, 3),
                        }
                    )
                self._gesture_announced = False

    def _clear_takeover(self, *, show_paused: bool = False) -> None:
        with self._takeover_state_lock:
            self._finish_takeover_hold()
            self._gesture_detector.reset()
            self._takeover_progress = self._gesture_detector.poll(now=self._monotonic())
            self._last_takeover_update = self._takeover_progress.observed_at
            self._takeover_pending.clear()
            self._gesture_announced = False
            if show_paused:
                self._hud_call("show_paused")
            else:
                self._hud_call("hide")

    def _finish_takeover_hold(self) -> float:
        with self._lock:
            if self._takeover_hold_started is None:
                return 0.0
            duration = max(0.0, self._monotonic() - self._takeover_hold_started)
            self._takeover_hold_started = None
            self._total_pause_seconds += duration
            self._save_locked()
        return duration

    def _wait_if_takeover_pending(self) -> None:
        while self._takeover_pending.is_set() and not self.pause_event.is_set():
            self._raise_if_stopped()
            if self.stop_event.wait(0.05):
                self._raise_if_stopped()

    def _hud_call(self, method: str, *args, **kwargs) -> None:
        hud = self._takeover_hud
        if hud is None:
            return
        try:
            getattr(hud, method)(*args, **kwargs)
        except Exception as exc:
            self._emit({"event": "takeover_hud_failed", "method": method, "error": repr(exc)})

    def _capture_checkpoint_state(self) -> None:
        with self._lock:
            if self._checkpoint is None or self._checkpoint.get("state_captured_at"):
                return
        state = self._read_state()
        with self._lock:
            if self._checkpoint is None or self._checkpoint.get("state_captured_at"):
                return
            self._checkpoint.update(state)
            self._checkpoint["state_captured_at"] = datetime.now().isoformat(timespec="milliseconds")
            self._save_locked()
        self._emit({"event": "automation_checkpoint_captured", "checkpoint": self.pending_checkpoint()})

    def _validate_resume_state(self) -> dict:
        checkpoint = self.pending_checkpoint() or {}
        if (
            not checkpoint.get("task")
            or not checkpoint.get("route")
            or checkpoint.get("phase") not in RESUMABLE_PHASES
        ):
            raise ResumeValidationError(
                "the checkpoint has no resumable task step; use restart from the beginning"
            )
        try:
            route_index = int(checkpoint.get("route_index", 0))
            next_action_index = int(checkpoint.get("next_action_index", 0))
        except (TypeError, ValueError) as exc:
            raise ResumeValidationError("the checkpoint action index is invalid") from exc
        if route_index < 1 or next_action_index < 1:
            raise ResumeValidationError("the checkpoint action index is invalid")
        if self._continue_validator is not None:
            try:
                self._continue_validator(copy.deepcopy(checkpoint))
            except ResumeValidationError:
                raise
            except Exception as exc:
                raise ResumeValidationError(str(exc)) from exc
        expected_map = checkpoint.get("map")
        expected_coordinate = self._coordinate(checkpoint.get("coordinate"))
        if not expected_map or expected_coordinate is None:
            raise ResumeValidationError(
                "the checkpoint has no readable map and coordinate; use restart from the beginning"
            )

        current = self._read_state()
        current_map = current.get("map")
        current_coordinate = self._coordinate(current.get("coordinate"))
        if expected_map and current_map != expected_map:
            raise ResumeValidationError(f"map mismatch: expected {expected_map}, got {current_map or 'unreadable'}")
        if expected_coordinate is not None:
            if current_coordinate is None:
                raise ResumeValidationError("the current coordinate is unreadable")
            dx = abs(expected_coordinate[0] - current_coordinate[0])
            dy = abs(expected_coordinate[1] - current_coordinate[1])
            if max(dx, dy) > self.coordinate_tolerance:
                raise ResumeValidationError(
                    f"coordinate mismatch: expected {expected_coordinate}, got {current_coordinate}"
                )
        return current

    def _validate_restart_state(self) -> dict:
        current = self._read_state()
        if not current.get("map") or self._coordinate(current.get("coordinate")) is None:
            raise ResumeValidationError(
                "the current game map and coordinate are unreadable; login recovery is required"
            )
        return current

    def _read_state(self) -> dict:
        if self._state_provider is None:
            return {"map": None, "coordinate": None}
        try:
            state = self._state_provider()
            if hasattr(state, "as_dict"):
                state = state.as_dict()
            if not isinstance(state, Mapping):
                raise TypeError(f"state provider returned {type(state).__name__}")
            coordinate = self._coordinate(state.get("coordinate"))
            return {
                "map": state.get("map") or state.get("map_name"),
                "coordinate": list(coordinate) if coordinate else None,
            }
        except Exception as exc:
            self._emit({"event": "automation_state_read_failed", "error": repr(exc)})
            return {"map": None, "coordinate": None}

    def _wait_resume_delay(self) -> None:
        remaining = self.resume_delay_seconds
        while remaining > 0:
            self._raise_if_stopped()
            started = self._monotonic()
            if self.stop_event.wait(min(0.1, remaining)):
                self._raise_if_stopped()
            remaining -= max(0.0, self._monotonic() - started)

    def _finish_resume(
        self,
        *,
        source: str,
        force: bool,
        mode: str,
        current_state: dict | None,
    ) -> None:
        now = datetime.now()
        with self._lock:
            duration = max(0.0, (now - self._paused_at).total_seconds()) if self._paused_at else 0.0
            self._total_pause_seconds += duration
            abandoned = copy.deepcopy(self._checkpoint) or {}
            if mode == CONTINUE_STEP:
                self._resume_checkpoint = {
                    **abandoned,
                    "checkpoint_phase": abandoned.get("phase"),
                    "phase": CONTINUE_STEP,
                    "recovery_mode": CONTINUE_STEP,
                    "requested_at": now.isoformat(timespec="milliseconds"),
                    "source": source,
                    "force": False,
                    "recovery_state": copy.deepcopy(current_state),
                }
            else:
                self._resume_checkpoint = {
                    "task": abandoned.get("task"),
                    "phase": RESTART_TASK,
                    "recovery_mode": RESTART_TASK,
                    "route_index": 1,
                    "next_action_index": 1,
                    "requested_at": now.isoformat(timespec="milliseconds"),
                    "source": source,
                    "force": bool(force),
                    "recovery_state": copy.deepcopy(current_state),
                    "abandoned_checkpoint": abandoned,
                }
            self.pause_event.clear()
            self._paused_at = None
            self._pause_reason = None
            self._pause_source = None
            self._checkpoint = None
            self._resume_request = None
            self._resume_in_progress = False
            self.resume_requested.clear()
            self._save_locked()
        self._hud_call("hide")
        if mode == CONTINUE_STEP:
            print(
                f"Automation resumed after {duration:.1f}s pause at "
                f"route {abandoned.get('route_index')}, action {abandoned.get('next_action_index')}."
            )
        else:
            print(
                f"Automation recovery accepted after {duration:.1f}s pause; "
                "task will restart from its beginning."
            )
            self._emit(
                {
                    "event": "automation_checkpoint_abandoned",
                    "source": source,
                    "task": abandoned.get("task"),
                    "route": abandoned.get("route"),
                    "next_action_index": abandoned.get("next_action_index"),
                    "reason": "restart_from_task_beginning",
                }
            )
        self._emit(
            {
                "event": "automation_resumed",
                "source": source,
                "force": force,
                "mode": mode,
                "pause_seconds": round(duration, 3),
                "state": current_state,
            }
        )

    def _finish_resume_attempt(self) -> None:
        with self._lock:
            self._resume_in_progress = False

    def _raise_if_stopped(self) -> None:
        if self.stop_event.is_set():
            raise KeyboardInterrupt("Scheduler stop requested.")

    @staticmethod
    def _coordinate(value) -> tuple[int, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None

    def _emit(self, event: dict) -> None:
        if not self.event_logger:
            return
        try:
            self.event_logger(event)
        except Exception as exc:
            print(f"Could not write runtime-control event: {exc}")

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
        except (OSError, ValueError):
            raw = {}
        self._total_pause_seconds = max(0.0, float(raw.get("total_pause_seconds", 0.0)))
        if isinstance(raw.get("resume_checkpoint"), Mapping):
            self._resume_checkpoint = dict(raw["resume_checkpoint"])
        if raw.get("paused") and isinstance(raw.get("checkpoint"), Mapping):
            self._checkpoint = dict(raw["checkpoint"])
            self._pause_reason = raw.get("reason")
            self._pause_source = raw.get("source")
            try:
                self._paused_at = datetime.fromisoformat(raw.get("paused_at"))
            except (TypeError, ValueError):
                self._paused_at = datetime.now()
            self.pause_event.set()

    def _save_locked(self) -> None:
        payload = {
            "schema_version": RUNTIME_CONTROL_SCHEMA_VERSION,
            "paused": self.pause_event.is_set(),
            "reason": self._pause_reason,
            "source": self._pause_source,
            "paused_at": self._paused_at.isoformat(timespec="milliseconds") if self._paused_at else None,
            "total_pause_seconds": round(self._total_pause_seconds, 3),
            "checkpoint": self._checkpoint,
            "resume_checkpoint": self._resume_checkpoint,
            "updated_at": datetime.now().isoformat(timespec="milliseconds"),
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_file)
