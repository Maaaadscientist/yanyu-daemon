import atexit
import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from AppKit import (
    NSApplicationActivateAllWindows,
    NSApplicationActivateIgnoringOtherApps,
    NSRunningApplication,
    NSWorkspace,
)
from Quartz import (
    CGEventCreate,
    CGEventCreateMouseEvent,
    CGEventGetLocation,
    CGEventPost,
    CGEventPostToPid,
    CGEventSetIntegerValueField,
    CGEventSourceCreate,
    CGWarpMouseCursorPosition,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGEventSourceStateHIDSystemState,
    kCGEventSourceStatePrivate,
    kCGEventTargetUnixProcessID,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGMouseEventClickState,
    kCGMouseEventWindowUnderMousePointer,
    kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent,
)

from automation import GameAutomation
from coordinates import pos


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compare PID-targeted and global Quartz click behavior."
    )
    parser.add_argument("point_name", choices=sorted(pos), help="Named reference point to click once.")
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    parser.add_argument("--output-dir", help="Directory for screenshots and report.json.")
    parser.add_argument("--source", choices=("private", "hid"), default="private")
    parser.add_argument("--set-routing-fields", action="store_true")
    parser.add_argument("--prime-move", action="store_true")
    parser.add_argument("--delivery", choices=("pid", "hid-tap"), default="pid")
    parser.add_argument("--activate-game", action="store_true")
    parser.add_argument("--cleanup-point-name", choices=sorted(pos))
    return parser.parse_args()


def cursor_position():
    point = CGEventGetLocation(CGEventCreate(None))
    return [round(float(point.x), 3), round(float(point.y), 3)]


def frontmost_application():
    application = NSWorkspace.sharedWorkspace().frontmostApplication()
    if application is None:
        return None
    return {
        "name": str(application.localizedName()),
        "pid": int(application.processIdentifier()),
    }


def activate_application(pid):
    application = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if application is None:
        return False
    return bool(
        application.activateWithOptions_(
            NSApplicationActivateIgnoringOtherApps | NSApplicationActivateAllWindows
        )
    )


def post_click(
    pid,
    window_number,
    point,
    *,
    source_state,
    set_routing_fields,
    prime_move,
    delivery,
):
    source = CGEventSourceCreate(source_state)
    event_types = [kCGEventLeftMouseDown, kCGEventLeftMouseUp]
    if prime_move:
        event_types.insert(0, kCGEventMouseMoved)
    cursor_samples = []
    for event_type in event_types:
        event = CGEventCreateMouseEvent(source, event_type, point, kCGMouseButtonLeft)
        if event_type != kCGEventMouseMoved:
            CGEventSetIntegerValueField(event, kCGMouseEventClickState, 1)
        if set_routing_fields:
            CGEventSetIntegerValueField(event, kCGEventTargetUnixProcessID, pid)
            CGEventSetIntegerValueField(event, kCGMouseEventWindowUnderMousePointer, window_number)
            CGEventSetIntegerValueField(
                event,
                kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent,
                window_number,
            )
        if delivery == "pid":
            CGEventPostToPid(pid, event)
        else:
            CGEventPost(kCGHIDEventTap, event)
        time.sleep(0.05)
        cursor_samples.append(
            {
                "event_type": int(event_type),
                "cursor": cursor_position(),
            }
        )
    return cursor_samples


def image_change(before, after, threshold=12):
    before_pixels = np.asarray(before.convert("RGB"), dtype=np.int16)
    after_pixels = np.asarray(after.convert("RGB"), dtype=np.int16)
    delta = np.abs(after_pixels - before_pixels)
    changed = np.max(delta, axis=2) >= threshold
    return {
        "mean_absolute_delta": round(float(delta.mean()), 4),
        "changed_pixel_ratio": round(float(changed.mean()), 6),
    }


def main():
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir or f"runs/quartz_probe_{timestamp}")
    output_dir.mkdir(parents=True, exist_ok=True)

    automation = GameAutomation(position_names={value: key for key, value in pos.items()})
    if not automation.window_owner_pid:
        raise RuntimeError("The game window has no owner PID.")

    before = automation.capture_image()
    if before is None:
        raise RuntimeError("Cannot capture the game window before the probe.")
    before_path = output_dir / "before.png"
    before.save(before_path)

    reference_point = pos[args.point_name]
    screen_point = automation.screen_point(reference_point)
    cursor_before = cursor_position()
    frontmost_initial = frontmost_application()
    activation_succeeded = None
    if args.activate_game:
        if frontmost_initial:
            atexit.register(activate_application, frontmost_initial["pid"])
        activation_succeeded = activate_application(automation.window_owner_pid)
        time.sleep(1.25)
    frontmost_before = frontmost_application()
    if args.activate_game and (
        not frontmost_before or frontmost_before["pid"] != automation.window_owner_pid
    ):
        raise RuntimeError(f"Game activation did not stabilize: {frontmost_before}")
    if args.delivery == "hid-tap" and (
        not frontmost_before or frontmost_before["pid"] != automation.window_owner_pid
    ):
        raise RuntimeError("Global HID probe requires the game to be the confirmed frontmost application.")
    if args.delivery == "hid-tap":
        atexit.register(CGWarpMouseCursorPosition, tuple(cursor_before))

    source_state = (
        kCGEventSourceStateHIDSystemState if args.source == "hid" else kCGEventSourceStatePrivate
    )
    dispatch_cursor_samples = post_click(
        automation.window_owner_pid,
        automation.window_number,
        screen_point,
        source_state=source_state,
        set_routing_fields=args.set_routing_fields,
        prime_move=args.prime_move,
        delivery=args.delivery,
    )
    time.sleep(max(0.0, args.settle_seconds))

    cursor_after = cursor_position()
    frontmost_after = frontmost_application()
    after = automation.capture_image()
    if after is None:
        raise RuntimeError("Cannot capture the game window after the probe.")
    after_path = output_dir / "after.png"
    after.save(after_path)

    cleanup = None
    if args.cleanup_point_name:
        cleanup_reference_point = pos[args.cleanup_point_name]
        cleanup_screen_point = automation.screen_point(cleanup_reference_point)
        cleanup_cursor_samples = post_click(
            automation.window_owner_pid,
            automation.window_number,
            cleanup_screen_point,
            source_state=source_state,
            set_routing_fields=args.set_routing_fields,
            prime_move=args.prime_move,
            delivery=args.delivery,
        )
        time.sleep(max(0.0, args.settle_seconds))
        cleanup_image = automation.capture_image()
        if cleanup_image is None:
            raise RuntimeError("Cannot capture the game window after the cleanup click.")
        cleanup_path = output_dir / "after_cleanup.png"
        cleanup_image.save(cleanup_path)
        cleanup = {
            "point_name": args.cleanup_point_name,
            "reference_point": list(cleanup_reference_point),
            "screen_point": [round(float(value), 3) for value in cleanup_screen_point],
            "image_change_from_before": image_change(before, cleanup_image),
            "dispatch_cursor_samples": cleanup_cursor_samples,
            "image": str(cleanup_path),
        }

    if args.activate_game and frontmost_initial:
        activate_application(frontmost_initial["pid"])
        time.sleep(0.5)
    cursor_before_restore = cursor_position()
    if args.delivery == "hid-tap":
        CGWarpMouseCursorPosition(tuple(cursor_before))
        time.sleep(0.1)
    cursor_final = cursor_position()
    frontmost_final = frontmost_application()

    report = {
        "time": datetime.now().isoformat(timespec="milliseconds"),
        "point_name": args.point_name,
        "reference_point": list(reference_point),
        "screen_point": [round(float(value), 3) for value in screen_point],
        "game_pid": int(automation.window_owner_pid),
        "window_number": int(automation.window_number),
        "source": args.source,
        "set_routing_fields": args.set_routing_fields,
        "prime_move": args.prime_move,
        "delivery": args.delivery,
        "activate_game": args.activate_game,
        "activation_succeeded": activation_succeeded,
        "frontmost_initial": frontmost_initial,
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "dispatch_cursor_samples": dispatch_cursor_samples,
        "cursor_distance": round(math.dist(cursor_before, cursor_after), 4),
        "cursor_before_restore": cursor_before_restore,
        "cursor_final": cursor_final,
        "cursor_final_distance": round(math.dist(cursor_before, cursor_final), 4),
        "frontmost_before": frontmost_before,
        "frontmost_after": frontmost_after,
        "frontmost_unchanged": frontmost_before == frontmost_after,
        "frontmost_final": frontmost_final,
        "frontmost_restored": frontmost_initial == frontmost_final,
        "image_change": image_change(before, after),
        "cleanup": cleanup,
        "before_image": str(before_path),
        "after_image": str(after_path),
    }
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
