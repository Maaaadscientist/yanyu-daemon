import argparse
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import Quartz
from PIL import Image
from pynput import keyboard


def get_window_by_name(app_name):
    window_info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for window in window_info or []:
        owner_name = window.get("kCGWindowOwnerName", "")
        title = window.get("kCGWindowName", "")
        if app_name in owner_name or app_name in title:
            return window
    return None


def screenshot_app_window_high_res(app_name, output_dir=".", update_reference=False):
    target_window = get_window_by_name(app_name)
    if not target_window:
        print(f"No window found for application named {app_name}")
        return None

    rect = target_window["kCGWindowBounds"]
    x, y, width, height = int(rect["X"]), int(rect["Y"]), int(rect["Width"]), int(rect["Height"])

    region = Quartz.CGRectMake(x, y, width, height)
    image_ref = Quartz.CGWindowListCreateImage(
        region,
        Quartz.kCGWindowListOptionIncludingWindow,
        target_window["kCGWindowNumber"],
        Quartz.kCGWindowImageDefault,
    )

    image_width = Quartz.CGImageGetWidth(image_ref)
    image_height = Quartz.CGImageGetHeight(image_ref)
    bytes_per_row = Quartz.CGImageGetBytesPerRow(image_ref)
    data_provider = Quartz.CGImageGetDataProvider(image_ref)
    data = Quartz.CGDataProviderCopyData(data_provider)
    image_data = np.frombuffer(data, dtype=np.uint8).reshape((image_height, bytes_per_row // 4, 4))
    image_data = image_data[..., [2, 1, 0, 3]]

    screenshot = Image.fromarray(image_data[:, :image_width, :4])

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = output_path / f"screenshot_high_res_{timestamp}.png"
    screenshot.save(filename)
    print(f"Saved {filename} ({image_width}x{image_height})")

    if update_reference:
        shutil.copyfile(filename, "game_screenshot.png")
        print("Updated game_screenshot.png")

    return filename


def parse_args():
    parser = argparse.ArgumentParser(description="Capture high-resolution screenshots of the game window.")
    parser.add_argument("--app", default="烟雨江湖", help="Application/window name to capture.")
    parser.add_argument("--output-dir", default=".", help="Directory for timestamped screenshots.")
    parser.add_argument(
        "--update-reference",
        action="store_true",
        help="Also copy the capture to game_screenshot.png for click scaling.",
    )
    parser.add_argument("--once", action="store_true", help="Capture once and exit.")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.once:
        screenshot_app_window_high_res(args.app, args.output_dir, args.update_reference)
        return

    def on_press(key):
        try:
            if key.char == "`":
                screenshot_app_window_high_res(args.app, args.output_dir, args.update_reference)
        except AttributeError:
            pass

    print("Press ` to capture the app window. Press Ctrl-C to quit.")
    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


if __name__ == "__main__":
    main()
