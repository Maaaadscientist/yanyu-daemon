import math
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping, Tuple, Union

import cv2
import numpy as np
import pyautogui
from PIL import Image
from Quartz import (
    CGDataProviderCopyData,
    CGImageGetBytesPerRow,
    CGImageGetDataProvider,
    CGImageGetHeight,
    CGImageGetWidth,
    CGRectMake,
    CGWindowListCopyWindowInfo,
    CGWindowListCreateImage,
    kCGWindowImageDefault,
    kCGWindowListOptionIncludingWindow,
    kCGWindowListOptionOnScreenOnly,
)

Point = Tuple[int, int]
Drag = Tuple[Point, Point]
ActionTarget = Union[Point, Drag]
ClickAction = Tuple[ActionTarget, float]


@dataclass(frozen=True)
class WindowInfo:
    left: float
    top: float
    width: float
    height: float
    number: int | None = None
    owner_pid: int | None = None


@dataclass(frozen=True)
class RapidClickResult:
    clicked_at: tuple[datetime, ...]
    gaps: tuple[float, ...]


@dataclass(frozen=True)
class ActionDispatch:
    index: int
    action_type: str
    name: str
    dispatched_at: datetime | None
    target: ActionTarget
    paused_seconds: float = 0.0


@dataclass(frozen=True)
class RouteExecutionResult:
    route_name: str
    started_at: datetime
    completed_at: datetime
    actions_completed: int
    dispatches: tuple[ActionDispatch, ...]
    human_pause_seconds: float = 0.0


class RouteExecutionError(RuntimeError):
    def __init__(self, message: str, *, route_name: str, status: Mapping, dispatches: tuple) -> None:
        super().__init__(message)
        self.route_name = route_name
        self.action_index = status.get("index")
        self.action_type = status.get("type")
        self.action_label = status.get("name")
        self.dispatches = dispatches


class RapidClickTimingError(RuntimeError):
    pass


class FeatureAlignmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeatureAlignmentResult:
    point: Point
    matches: int
    inliers: int
    inlier_ratio: float
    scale: float
    rotation_degrees: float


class GameAutomation:
    def __init__(
        self,
        game_title: str = "烟雨江湖",
        reference_image: str = "game_screenshot.png",
        position_names: Mapping[Point, str] | None = None,
        stop_event: threading.Event | None = None,
        runtime_control=None,
    ) -> None:
        screen_width, screen_height = pyautogui.size()
        print(f"Screen width: {screen_width}, Screen height: {screen_height}")

        window_info = get_window_info(game_title)
        if not window_info:
            raise RuntimeError(f"No window found with title or owner '{game_title}'.")

        image_path = Path(reference_image)
        image = Image.open(image_path)
        image_width, image_height = image.size

        self.window_left = window_info.left
        self.window_top = window_info.top
        self.window_width = window_info.width
        self.window_height = window_info.height
        self.window_number = window_info.number
        self.window_owner_pid = window_info.owner_pid
        self.reference_width = image_width
        self.reference_height = image_height
        self.scale_x = window_info.width / image_width
        self.scale_y = window_info.height / image_height
        self.position_names = position_names or {}
        self.stop_event = stop_event
        self.runtime_control = runtime_control

        print(f"Window Position: ({self.window_left}, {self.window_top})")
        print(f"Window Size: {window_info.width}x{window_info.height}")
        print(f"Image Size: {image_width}x{image_height}")

    def wait_seconds(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        runtime_control = getattr(self, "runtime_control", None)
        if runtime_control is not None:
            runtime_control.wait(seconds)
            return
        stop_event = getattr(self, "stop_event", None)
        if stop_event is not None:
            if stop_event.wait(seconds):
                raise KeyboardInterrupt("Scheduler stop requested.")
            return
        time.sleep(seconds)

    def screen_point(self, point: Point) -> tuple[float, float]:
        x, y = point
        return self.window_left + x * self.scale_x, self.window_top + y * self.scale_y

    def focus_window(self, *, settle_seconds: float = 0.2, dry_run: bool = False) -> bool:
        if dry_run:
            return True

        activated = False
        if self.window_owner_pid:
            try:
                from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

                application = NSRunningApplication.runningApplicationWithProcessIdentifier_(self.window_owner_pid)
                if application:
                    activated = bool(
                        application.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                    )
            except Exception as exc:
                print(f"Cannot activate the game window through AppKit: {exc}")

        if activated and settle_seconds > 0:
            self.wait_seconds(settle_seconds)
        return activated

    def click_reference(
        self,
        point: Point,
        *,
        duration: float = 0.1,
        dry_run: bool = False,
    ) -> datetime | None:
        x, y = self.screen_point(point)
        if dry_run:
            return None
        with self._automation_input():
            pyautogui.moveTo(x, y, duration=duration)
            clicked_at = datetime.now()
            pyautogui.click()
        return clicked_at

    def drag_reference(
        self,
        start: Point,
        end: Point,
        *,
        duration: float = 0.5,
        dry_run: bool = False,
    ) -> datetime | None:
        start_x, start_y = self.screen_point(start)
        end_x, end_y = self.screen_point(end)
        if dry_run:
            return None
        with self._automation_input():
            pyautogui.moveTo(start_x, start_y)
            self.wait_seconds(0.2)
            dragged_at = datetime.now()
            pyautogui.dragTo(end_x, end_y, button="left", duration=duration)
        self.wait_seconds(0.2)
        return dragged_at

    def rapid_click_reference(
        self,
        points: Iterable[Point],
        *,
        intervals: Iterable[float] | float,
        max_gap_seconds: float,
        dry_run: bool = False,
    ) -> RapidClickResult:
        """Execute every click even if a deadline is missed, then report timing failure."""
        point_list = list(points)
        if len(point_list) < 2:
            raise ValueError("A rapid click sequence requires at least two points.")
        if isinstance(intervals, (int, float)):
            interval_list = [float(intervals)] * (len(point_list) - 1)
        else:
            interval_list = [float(value) for value in intervals]
        if len(interval_list) != len(point_list) - 1:
            raise ValueError("Rapid click intervals must contain one value between each pair of points.")
        if max_gap_seconds <= 0 or any(value < 0 for value in interval_list):
            raise ValueError("Rapid click intervals and max_gap_seconds must be non-negative.")
        if any(value > max_gap_seconds for value in interval_list):
            raise ValueError("A planned rapid click interval exceeds max_gap_seconds.")
        if dry_run:
            return RapidClickResult((), ())

        click_times = []
        wall_times = []
        started = time.monotonic()
        deadline = started
        old_pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0
        try:
            with self._automation_input():
                for index, point in enumerate(point_list):
                    if index:
                        deadline += interval_list[index - 1]
                        remaining = deadline - time.monotonic()
                        if remaining > 0:
                            time.sleep(remaining)
                    x, y = self.screen_point(point)
                    click_times.append(time.monotonic())
                    wall_times.append(datetime.now())
                    pyautogui.click(x=x, y=y)
        finally:
            pyautogui.PAUSE = old_pause

        gaps = tuple(later - earlier for earlier, later in zip(click_times, click_times[1:]))
        result = RapidClickResult(tuple(wall_times), gaps)
        stop_event = getattr(self, "stop_event", None)
        if stop_event is not None and stop_event.is_set():
            raise KeyboardInterrupt("Scheduler stop requested after completing rapid clicks.")
        missed = [gap for gap in gaps if gap > max_gap_seconds]
        if missed:
            raise RapidClickTimingError(
                f"Rapid click sequence completed, but max gap was {max(missed):.3f}s "
                f"(limit {max_gap_seconds:.3f}s)."
            )
        return result

    def align_reference_point(
        self,
        alignment_image: str | Path,
        point: Point,
        *,
        min_matches: int = 40,
        min_inlier_ratio: float = 0.45,
        max_rotation_degrees: float = 4.0,
    ) -> FeatureAlignmentResult:
        """Map a point from a canonical UI image onto the current captured window."""
        image_path = Path(alignment_image)
        if not image_path.is_absolute() and not image_path.exists():
            image_path = Path(__file__).resolve().parent / image_path
        if not image_path.exists():
            raise FeatureAlignmentError(f"Alignment image does not exist: {alignment_image}")

        current_image = self.capture_image()
        if current_image is None:
            raise FeatureAlignmentError("Cannot capture the current window for feature alignment.")

        with Image.open(image_path) as opened:
            canonical_rgb = np.asarray(opened.convert("RGB"))
        current_rgb = np.asarray(current_image.convert("RGB"))
        height, width = canonical_rgb.shape[:2]
        current_rgb = cv2.resize(current_rgb, (width, height), interpolation=cv2.INTER_AREA)
        canonical_gray = cv2.cvtColor(canonical_rgb, cv2.COLOR_RGB2GRAY)
        current_gray = cv2.cvtColor(current_rgb, cv2.COLOR_RGB2GRAY)

        # Ignore window chrome and edge controls; the map artwork should drive the transform.
        mask = np.zeros((height, width), dtype=np.uint8)
        top = max(1, round(height * 0.04))
        bottom = max(1, round(height * 0.05))
        side = max(1, round(width * 0.02))
        mask[top : height - bottom, side : width - side] = 255

        detector = cv2.ORB_create(nfeatures=5000, fastThreshold=8)
        canonical_points, canonical_descriptors = detector.detectAndCompute(canonical_gray, mask)
        current_points, current_descriptors = detector.detectAndCompute(current_gray, mask)
        if canonical_descriptors is None or current_descriptors is None:
            raise FeatureAlignmentError("Not enough visual features for alignment.")

        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(
            canonical_descriptors,
            current_descriptors,
            k=2,
        )
        matches = [
            first
            for pair in pairs
            if len(pair) == 2
            for first, second in [pair]
            if first.distance < 0.75 * second.distance
        ]
        if len(matches) < min_matches:
            raise FeatureAlignmentError(
                f"Feature alignment found {len(matches)} matches; at least {min_matches} are required."
            )

        source = np.float32([canonical_points[item.queryIdx].pt for item in matches])
        destination = np.float32([current_points[item.trainIdx].pt for item in matches])
        transform, inlier_mask = cv2.estimateAffinePartial2D(
            source,
            destination,
            method=cv2.RANSAC,
            ransacReprojThreshold=3.0,
            maxIters=5000,
            confidence=0.995,
        )
        if transform is None or inlier_mask is None:
            raise FeatureAlignmentError("Cannot estimate a stable feature transform.")

        inliers = int(inlier_mask.sum())
        inlier_ratio = inliers / len(matches)
        scale = math.hypot(float(transform[0, 0]), float(transform[1, 0]))
        rotation_degrees = math.degrees(math.atan2(float(transform[1, 0]), float(transform[0, 0])))
        if inlier_ratio < min_inlier_ratio:
            raise FeatureAlignmentError(
                f"Feature alignment inlier ratio {inlier_ratio:.3f} is below {min_inlier_ratio:.3f}."
            )
        if not 0.85 <= scale <= 1.15:
            raise FeatureAlignmentError(f"Feature alignment scale {scale:.3f} is outside the safe range.")
        if abs(rotation_degrees) > max_rotation_degrees:
            raise FeatureAlignmentError(
                f"Feature alignment rotation {rotation_degrees:.2f} degrees exceeds the safe limit."
            )

        source_point = np.float32(
            [
                [
                    float(point[0]) * width / self.reference_width,
                    float(point[1]) * height / self.reference_height,
                    1.0,
                ]
            ]
        )
        mapped = source_point @ transform.T
        mapped_point = (
            round(float(mapped[0, 0]) * self.reference_width / width),
            round(float(mapped[0, 1]) * self.reference_height / height),
        )
        if not (0 <= mapped_point[0] < self.reference_width and 0 <= mapped_point[1] < self.reference_height):
            raise FeatureAlignmentError(f"Aligned point is outside the game window: {mapped_point}")

        return FeatureAlignmentResult(
            point=mapped_point,
            matches=len(matches),
            inliers=inliers,
            inlier_ratio=inlier_ratio,
            scale=scale,
            rotation_degrees=rotation_degrees,
        )

    def run_actions(
        self,
        actions: Iterable[ClickAction],
        *,
        route_name: str | None = None,
        start_delay: float = 1.0,
        end_delay: float = 2.5,
        print_names: bool = False,
        dry_run: bool = False,
        step: bool = False,
        action_logger: Callable[[dict], None] | None = None,
        before_action_hook: Callable[[dict], None] | None = None,
        capture_dir: str | None = None,
        capture_each_action: bool = False,
        start_index: int = 1,
        route_index: int = 1,
        total_routes: int = 1,
        skip_action_indexes: Iterable[int] = (),
    ) -> RouteExecutionResult:
        action_list = list(actions)
        skipped_indexes = {int(value) for value in skip_action_indexes}
        route_label = route_name or "route"
        if start_index < 1 or start_index > len(action_list) + 1:
            raise ValueError(f"start_index must be between 1 and {len(action_list) + 1}.")
        started_at = datetime.now()
        route_start = time.monotonic()
        runtime_control = getattr(self, "runtime_control", None)
        pause_at_start = runtime_control.total_pause_seconds if runtime_control is not None else 0.0
        if runtime_control is not None:
            runtime_control.set_context(
                route=route_label,
                route_index=route_index,
                total_routes=total_routes,
                next_action_index=start_index,
                total_actions=len(action_list),
                phase="route_start",
            )
        if not dry_run:
            self.wait_seconds(0.0)
            self.focus_window()
            self.wait_seconds(start_delay)
        dispatches = []
        completed = 0
        for index, (target, delay) in enumerate(action_list, start=1):
            if index < start_index:
                continue
            status = self.describe_action(route_label, index, len(action_list), target, delay, route_start, dry_run)
            if runtime_control is not None and not dry_run:
                runtime_control.before_action(
                    route=route_label,
                    route_index=route_index,
                    total_routes=total_routes,
                    action_index=index,
                    total_actions=len(action_list),
                    action_type=status["type"],
                    action_label=status["name"],
                )
                self.wait_seconds(0.0)
            if index in skipped_indexes:
                skipped_status = {**status, "event": "legacy_action_skipped", "reason": "resource_cooldown"}
                if print_names or dry_run or step:
                    print(
                        f"[{route_label} {index:03d}/{len(action_list):03d}] "
                        f"skip {status['name']} (resource cooldown)"
                    )
                if action_logger:
                    action_logger(skipped_status)
                if runtime_control is not None and not dry_run:
                    runtime_control.action_dispatched(index)
                completed += 1
                continue
            if before_action_hook:
                before_action_hook(status)
            if print_names or dry_run or step:
                print(format_status(status))
            if action_logger:
                action_logger(status)
            if step:
                input("Press Enter to execute this action...")

            try:
                if not dry_run:
                    self.wait_seconds(delay)
                if is_point(target):
                    dispatched_at = self.click_reference(target, dry_run=dry_run)
                else:
                    start, end = target
                    dispatched_at = self.drag_reference(start, end, dry_run=dry_run)
                paused_seconds = (
                    max(0.0, runtime_control.total_pause_seconds - pause_at_start)
                    if runtime_control is not None
                    else 0.0
                )
                dispatch = ActionDispatch(
                    index=index,
                    action_type=status["type"],
                    name=status["name"],
                    dispatched_at=dispatched_at,
                    target=target,
                    paused_seconds=paused_seconds,
                )
                dispatches.append(dispatch)
                completed += 1
                if runtime_control is not None and not dry_run:
                    runtime_control.action_dispatched(index, dispatched_at)
                if action_logger and dispatched_at:
                    action_logger(
                        {
                            **status,
                            "event": "legacy_action_dispatched",
                            "dispatched_at": dispatched_at.isoformat(timespec="milliseconds"),
                        }
                    )
                if capture_dir and capture_each_action and not dry_run:
                    self.capture_screenshot(capture_dir, f"{safe_name(route_label)}_{index:03d}")
            except Exception as exc:
                raise RouteExecutionError(
                    f"Route '{route_label}' failed at action {index} ({status['name']}): {exc}",
                    route_name=route_label,
                    status=status,
                    dispatches=tuple(dispatches),
                ) from exc
        if not dry_run:
            self.wait_seconds(end_delay)
        if capture_dir and not capture_each_action and not dry_run:
            self.capture_screenshot(capture_dir, safe_name(route_label))
        completed_at = datetime.now()
        return RouteExecutionResult(
            route_name=route_label,
            started_at=started_at,
            completed_at=completed_at,
            actions_completed=completed,
            dispatches=tuple(dispatches),
            human_pause_seconds=(
                max(0.0, runtime_control.total_pause_seconds - pause_at_start)
                if runtime_control is not None
                else 0.0
            ),
        )

    def _automation_input(self):
        runtime_control = getattr(self, "runtime_control", None)
        if runtime_control is None:
            return nullcontext()
        return runtime_control.automation_input()

    def describe_action(
        self,
        route_name: str,
        index: int,
        total: int,
        target: ActionTarget,
        delay: float,
        route_start: float,
        dry_run: bool,
    ) -> dict:
        if is_point(target):
            screen_x, screen_y = self.screen_point(target)
            return {
                "time": datetime.now().isoformat(timespec="seconds"),
                "route": route_name,
                "index": index,
                "total": total,
                "elapsed": round(time.monotonic() - route_start, 3),
                "delay": delay,
                "type": "click",
                "name": self.position_names.get(target, str(target)),
                "target": target,
                "screen": (round(screen_x, 1), round(screen_y, 1)),
                "dry_run": dry_run,
            }

        start, end = target
        start_x, start_y = self.screen_point(start)
        end_x, end_y = self.screen_point(end)
        return {
            "time": datetime.now().isoformat(timespec="seconds"),
            "route": route_name,
            "index": index,
            "total": total,
            "elapsed": round(time.monotonic() - route_start, 3),
            "delay": delay,
            "type": "drag",
            "name": f"{self.position_names.get(start, start)} -> {self.position_names.get(end, end)}",
            "target": target,
            "screen_start": (round(start_x, 1), round(start_y, 1)),
            "screen_end": (round(end_x, 1), round(end_y, 1)),
            "dry_run": dry_run,
        }

    def capture_cg_image(self):
        if not self.window_number:
            return None

        region = CGRectMake(self.window_left, self.window_top, self.window_width, self.window_height)
        return CGWindowListCreateImage(
            region,
            kCGWindowListOptionIncludingWindow,
            self.window_number,
            kCGWindowImageDefault,
        )

    def capture_image(self) -> Image.Image | None:
        image_ref = self.capture_cg_image()
        if image_ref is None:
            return None

        image_width = CGImageGetWidth(image_ref)
        image_height = CGImageGetHeight(image_ref)
        bytes_per_row = CGImageGetBytesPerRow(image_ref)
        data_provider = CGImageGetDataProvider(image_ref)
        data = CGDataProviderCopyData(data_provider)
        image_data = np.frombuffer(data, dtype=np.uint8).reshape((image_height, bytes_per_row // 4, 4))
        image_data = image_data[..., [2, 1, 0, 3]]
        return Image.fromarray(image_data[:, :image_width, :4])

    def capture_screenshot(self, output_dir: str, prefix: str = "capture") -> Path | None:
        screenshot = self.capture_image()
        if screenshot is None:
            return None

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = output_path / f"{prefix}_{timestamp}.png"
        screenshot.save(filename)
        return filename


def get_window_info(window_name: str) -> WindowInfo | None:
    window_list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, 0)
    matches = []
    for window in window_list or []:
        owner_name = window.get("kCGWindowOwnerName", "")
        window_title = window.get("kCGWindowName", "")

        if window_name in window_title or window_name in owner_name:
            bounds = window.get("kCGWindowBounds", {})
            width = float(bounds.get("Width", 0))
            height = float(bounds.get("Height", 0))
            if width <= 0 or height <= 0:
                continue
            info = WindowInfo(
                left=float(bounds.get("X", 0)),
                top=float(bounds.get("Y", 0)),
                width=width,
                height=height,
                number=window.get("kCGWindowNumber"),
                owner_pid=window.get("kCGWindowOwnerPID"),
            )
            layer_priority = int(window.get("kCGWindowLayer", 0) == 0)
            matches.append((layer_priority, width * height, info))
    return max(matches, key=lambda item: item[:2])[2] if matches else None


def is_point(target: ActionTarget) -> bool:
    return isinstance(target[0], int)


def calculate_event_time(base_time: datetime, interval_minutes: float) -> datetime:
    return base_time + timedelta(minutes=interval_minutes)


def estimate_duration(actions: Iterable[ClickAction], start_delay: float = 1.0, end_delay: float = 2.5) -> float:
    return start_delay + sum(delay for _, delay in actions) + end_delay


def format_status(status: dict) -> str:
    if status["type"] == "click":
        return (
            f"[{status['route']} {status['index']:03d}/{status['total']:03d} "
            f"+{status['elapsed']:.1f}s wait={status['delay']}] "
            f"click {status['name']} -> screen {status['screen']}"
        )
    return (
        f"[{status['route']} {status['index']:03d}/{status['total']:03d} "
        f"+{status['elapsed']:.1f}s wait={status['delay']}] "
        f"drag {status['name']} -> screen {status['screen_start']} to {status['screen_end']}"
    )


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
