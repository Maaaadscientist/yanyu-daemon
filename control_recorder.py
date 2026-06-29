import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from pynput import keyboard, mouse

from automation import GameAutomation


def parse_args():
    parser = argparse.ArgumentParser(description="Record manual game control as reusable route data.")
    parser.add_argument("--name", default="recorded_route", help="Route/procedure name.")
    parser.add_argument("--app", default="烟雨江湖", help="Application/window name to record.")
    parser.add_argument("--reference", default="game_screenshot.png", help="Reference screenshot for coordinate scaling.")
    parser.add_argument("--output-dir", default="recordings", help="Directory for recording output.")
    parser.add_argument("--drag-threshold", type=float, default=8.0, help="Minimum screen pixels to treat as a drag.")
    parser.add_argument("--min-delay", type=float, default=0.05, help="Smallest delay written to route actions.")
    parser.add_argument("--include-outside", action="store_true", help="Record actions even if they start outside the game window.")
    return parser.parse_args()


class ControlRecorder:
    def __init__(self, args):
        self.args = args
        self.automation = GameAutomation(game_title=args.app, reference_image=args.reference)
        self.output_dir = Path(args.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.base_path = self.output_dir / f"{args.name}_{timestamp}"
        self.jsonl_path = self.base_path.with_suffix(".jsonl")
        self.py_path = self.base_path.with_suffix(".py")
        self.events = []
        self.pressed_at = None
        self.last_action_time = None
        self.paused = False
        self.done = False
        self.jsonl_file = open(self.jsonl_path, "a", encoding="utf-8")

    def close(self):
        self.jsonl_file.close()

    def inside_window(self, x, y):
        return (
            self.automation.window_left <= x <= self.automation.window_left + self.automation.window_width
            and self.automation.window_top <= y <= self.automation.window_top + self.automation.window_height
        )

    def reference_point(self, x, y):
        ref_x = round((x - self.automation.window_left) / self.automation.scale_x)
        ref_y = round((y - self.automation.window_top) / self.automation.scale_y)
        return int(ref_x), int(ref_y)

    def action_delay(self):
        now = time.monotonic()
        if self.last_action_time is None:
            delay = 1.0
        else:
            delay = max(self.args.min_delay, now - self.last_action_time)
        self.last_action_time = now
        return round(delay, 3)

    def write_event(self, event):
        self.events.append(event)
        self.jsonl_file.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.jsonl_file.flush()
        print(format_event(event))

    def on_click(self, x, y, button, pressed):
        if self.paused or self.done:
            return
        if pressed:
            self.pressed_at = {
                "screen": (x, y),
                "reference": self.reference_point(x, y),
                "time": time.monotonic(),
                "inside": self.inside_window(x, y),
                "button": str(button),
            }
            return

        if not self.pressed_at:
            return
        start = self.pressed_at
        self.pressed_at = None
        end_inside = self.inside_window(x, y)
        if not self.args.include_outside and not (start["inside"] and end_inside):
            return

        end_ref = self.reference_point(x, y)
        dx = x - start["screen"][0]
        dy = y - start["screen"][1]
        distance = (dx * dx + dy * dy) ** 0.5
        delay = self.action_delay()
        duration = round(time.monotonic() - start["time"], 3)

        if distance >= self.args.drag_threshold:
            event = {
                "time": datetime.now().isoformat(timespec="milliseconds"),
                "procedure": self.args.name,
                "type": "drag",
                "delay": delay,
                "duration": duration,
                "start_reference": start["reference"],
                "end_reference": end_ref,
                "start_screen": start["screen"],
                "end_screen": (x, y),
                "distance": round(distance, 2),
            }
        else:
            event = {
                "time": datetime.now().isoformat(timespec="milliseconds"),
                "procedure": self.args.name,
                "type": "click",
                "delay": delay,
                "duration": duration,
                "reference": end_ref,
                "screen": (x, y),
                "button": start["button"],
            }
        self.write_event(event)

    def on_key_press(self, key):
        if key == keyboard.Key.f8:
            self.paused = not self.paused
            print("paused" if self.paused else "recording")
        elif key == keyboard.Key.f9:
            self.done = True
            return False

    def save_route(self):
        lines = [f"{self.args.name} = ["]
        for event in self.events:
            if event["type"] == "click":
                x, y = event["reference"]
                lines.append(f"    (({x}, {y}), {event['delay']}),")
            else:
                sx, sy = event["start_reference"]
                ex, ey = event["end_reference"]
                lines.append(f"    ((({sx}, {sy}), ({ex}, {ey})), {event['delay']}),")
        lines.append("]")
        self.py_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {self.jsonl_path}")
        print(f"wrote {self.py_path}")


def format_event(event):
    if event["type"] == "click":
        return f"click ref={tuple(event['reference'])} delay={event['delay']} duration={event['duration']}"
    return (
        f"drag ref={tuple(event['start_reference'])}->{tuple(event['end_reference'])} "
        f"delay={event['delay']} duration={event['duration']} distance={event['distance']}"
    )


def main():
    args = parse_args()
    recorder = ControlRecorder(args)
    print("Recording manual control.")
    print("F8 pauses/resumes. F9 stops and writes the route snippet.")
    print("Operate the game window now.")

    mouse_listener = mouse.Listener(on_click=recorder.on_click)
    keyboard_listener = keyboard.Listener(on_press=recorder.on_key_press)
    mouse_listener.start()
    keyboard_listener.start()
    try:
        while keyboard_listener.running and not recorder.done:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        mouse_listener.stop()
        keyboard_listener.stop()
        recorder.save_route()
        recorder.close()


if __name__ == "__main__":
    main()
