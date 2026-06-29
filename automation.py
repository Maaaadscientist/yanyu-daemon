import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping, Tuple, Union

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


class GameAutomation:
    def __init__(
        self,
        game_title: str = "烟雨江湖",
        reference_image: str = "game_screenshot.png",
        position_names: Mapping[Point, str] | None = None,
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
        self.scale_x = window_info.width / image_width
        self.scale_y = window_info.height / image_height
        self.position_names = position_names or {}

        print(f"Window Position: ({self.window_left}, {self.window_top})")
        print(f"Window Size: {window_info.width}x{window_info.height}")
        print(f"Image Size: {image_width}x{image_height}")

    def screen_point(self, point: Point) -> tuple[float, float]:
        x, y = point
        return self.window_left + x * self.scale_x, self.window_top + y * self.scale_y

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
        capture_dir: str | None = None,
        capture_each_action: bool = False,
    ) -> None:
        action_list = list(actions)
        route_label = route_name or "route"
        route_start = time.monotonic()
        time.sleep(start_delay)
        for index, (target, delay) in enumerate(action_list, start=1):
            status = self.describe_action(route_label, index, len(action_list), target, delay, route_start, dry_run)
            if print_names or dry_run or step:
                print(format_status(status))
            if action_logger:
                action_logger(status)
            if step:
                input("Press Enter to execute this action...")

            time.sleep(delay)
            if is_point(target):
                x, y = self.screen_point(target)
                if dry_run:
                    continue
                pyautogui.moveTo(x, y, duration=0.1)
                pyautogui.click()
            else:
                start, end = target
                start_x, start_y = self.screen_point(start)
                end_x, end_y = self.screen_point(end)
                if dry_run:
                    continue
                pyautogui.moveTo(start_x, start_y)
                time.sleep(0.2)
                pyautogui.dragTo(end_x, end_y, button="left", duration=0.5)
                time.sleep(0.2)
            if capture_dir and capture_each_action and not dry_run:
                self.capture_screenshot(capture_dir, f"{safe_name(route_label)}_{index:03d}")
        time.sleep(end_delay)
        if capture_dir and not capture_each_action and not dry_run:
            self.capture_screenshot(capture_dir, safe_name(route_label))

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

    def capture_screenshot(self, output_dir: str, prefix: str = "capture") -> Path | None:
        if not self.window_number:
            return None

        region = CGRectMake(self.window_left, self.window_top, self.window_width, self.window_height)
        image_ref = CGWindowListCreateImage(
            region,
            kCGWindowListOptionIncludingWindow,
            self.window_number,
            kCGWindowImageDefault,
        )
        image_width = CGImageGetWidth(image_ref)
        image_height = CGImageGetHeight(image_ref)
        bytes_per_row = CGImageGetBytesPerRow(image_ref)
        data_provider = CGImageGetDataProvider(image_ref)
        data = CGDataProviderCopyData(data_provider)
        image_data = np.frombuffer(data, dtype=np.uint8).reshape((image_height, bytes_per_row // 4, 4))
        image_data = image_data[..., [2, 1, 0, 3]]
        screenshot = Image.fromarray(image_data[:, :image_width, :4])

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = output_path / f"{prefix}_{timestamp}.png"
        screenshot.save(filename)
        return filename


def get_window_info(window_name: str) -> WindowInfo | None:
    window_list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, 0)

    for window in window_list:
        owner_name = window.get("kCGWindowOwnerName", "")
        window_title = window.get("kCGWindowName", "")

        if window_name in window_title or window_name in owner_name:
            bounds = window.get("kCGWindowBounds", {})
            return WindowInfo(
                left=bounds.get("X", 0),
                top=bounds.get("Y", 0),
                width=bounds.get("Width", 0),
                height=bounds.get("Height", 0),
                number=window.get("kCGWindowNumber"),
            )
    return None


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
