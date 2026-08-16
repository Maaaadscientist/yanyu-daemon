import argparse
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from pynput import keyboard, mouse

from automation import GameAutomation
from smart_automation import VisionGameStateReader, build_recorded_procedure, coordinate_tuple


def parse_args():
    parser = argparse.ArgumentParser(description="Record manual game control as reusable route data.")
    parser.add_argument("--name", default="recorded_route", help="Route/procedure name.")
    parser.add_argument("--app", default="烟雨江湖", help="Application/window name to record.")
    parser.add_argument("--reference", default="game_screenshot.png", help="Reference screenshot for coordinate scaling.")
    parser.add_argument("--output-dir", default="recordings", help="Directory for recording output.")
    parser.add_argument("--drag-threshold", type=float, default=8.0, help="Minimum screen pixels to treat as a drag.")
    parser.add_argument("--min-delay", type=float, default=0.05, help="Smallest delay written to route actions.")
    parser.add_argument("--include-outside", action="store_true", help="Record actions even if they start outside the game window.")
    parser.add_argument("--smart", action="store_true", help="Sample map coordinates and write a verified JSON procedure.")
    parser.add_argument("--install", action="store_true", help="Install the smart procedure into procedures/<name>.json.")
    parser.add_argument("--map", dest="map_name", help="Expected game map, for example 大理.")
    parser.add_argument("--start", help="Known starting game coordinate as x,y.")
    parser.add_argument("--target", help="Expected final game coordinate as x,y.")
    parser.add_argument("--interval-minutes", type=float, help="Scheduler refresh interval for an installed procedure.")
    parser.add_argument("--initial-delay-minutes", type=float, default=0.0, help="Initial scheduler delay.")
    parser.add_argument(
        "--lead-seconds",
        type=float,
        default=0.0,
        help="Start this many seconds before a scheduled refresh target.",
    )
    parser.add_argument("--before-route", action="append", default=[], help="Legacy route to run before this procedure.")
    parser.add_argument("--after-route", action="append", default=[], help="Legacy route to run after this procedure.")
    parser.add_argument("--state-sample-seconds", type=float, default=0.2, help="Minimum pause between OCR state samples.")
    parser.add_argument(
        "--rapid-max-gap",
        type=float,
        default=0.5,
        help="Minimum allowed max gap for an F5 rapid-click group.",
    )
    return parser.parse_args()


class StateMonitor:
    def __init__(self, reader, minimum_pause):
        self.reader = reader
        self.minimum_pause = max(0.05, minimum_pause)
        self.samples = []
        self.stop_event = threading.Event()
        self.suspended = threading.Event()
        self.ocr_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="game-state-monitor", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5.0)

    def suspend(self):
        self.suspended.set()
        # Wait for an in-flight OCR request to finish before the jump starts.
        with self.ocr_lock:
            pass

    def resume(self):
        self.suspended.clear()

    def _run(self):
        while not self.stop_event.is_set():
            if self.suspended.is_set():
                self.stop_event.wait(0.02)
                continue
            started = time.monotonic()
            with self.ocr_lock:
                if self.suspended.is_set():
                    continue
                try:
                    state = self.reader.read_state()
                    self.samples.append(
                        {
                            "monotonic": time.monotonic(),
                            "time": datetime.now().isoformat(timespec="milliseconds"),
                            "state": state.as_dict(),
                            "observations": [
                                {
                                    "text": observation.text,
                                    "confidence": round(observation.confidence, 4),
                                    "region": [
                                        round(observation.x, 5),
                                        round(1.0 - observation.y - observation.height, 5),
                                        round(observation.x + observation.width, 5),
                                        round(1.0 - observation.y, 5),
                                    ],
                                }
                                for observation in state.observations
                            ],
                        }
                    )
                except Exception as exc:
                    self.samples.append(
                        {
                            "monotonic": time.monotonic(),
                            "time": datetime.now().isoformat(timespec="milliseconds"),
                            "state": {"map": None, "coordinate": None},
                            "error": repr(exc),
                        }
                    )
            remaining = self.minimum_pause - (time.monotonic() - started)
            self.stop_event.wait(max(0.0, remaining))


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
        self.procedure_path = self.base_path.with_suffix(".procedure.json")
        self.states_path = self.base_path.with_suffix(".states.jsonl")
        self.events = []
        self.pressed_at = None
        self.last_action_time = None
        self.paused = False
        self.done = False
        self.rapid_group = None
        self.rapid_group_counter = 0
        self.jsonl_file = open(self.jsonl_path, "a", encoding="utf-8")
        self.state_monitor = None
        if args.smart:
            self.state_monitor = StateMonitor(
                VisionGameStateReader(self.automation),
                args.state_sample_seconds,
            )

    def start(self):
        if self.state_monitor:
            self.state_monitor.start()

    def close(self):
        if self.state_monitor:
            self.state_monitor.stop()
        self.jsonl_file.close()

    def stop_monitor(self):
        if self.state_monitor:
            self.state_monitor.stop()

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
        action_monotonic = time.monotonic()

        if distance >= self.args.drag_threshold:
            if self.rapid_group is not None:
                self.end_rapid_capture("drag detected; rapid capture closed")
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
                "monotonic": action_monotonic,
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
                "monotonic": action_monotonic,
            }
            if self.rapid_group is not None:
                event["rapid_group"] = self.rapid_group
        self.write_event(event)

    def on_key_press(self, key):
        if key == keyboard.Key.f5:
            self.toggle_rapid_capture()
        elif key == keyboard.Key.f6:
            self.mark_last("checkpoint")
        elif key == keyboard.Key.f7:
            self.mark_last("refresh_anchor", unique=True)
        elif key == keyboard.Key.f8:
            if self.rapid_group is not None:
                self.end_rapid_capture("rapid capture closed before pause")
            self.paused = not self.paused
            print("paused" if self.paused else "recording")
        elif key == keyboard.Key.f9:
            if self.rapid_group is not None:
                self.end_rapid_capture("rapid capture closed")
            self.done = True
            return False

    def toggle_rapid_capture(self):
        if self.rapid_group is not None:
            self.end_rapid_capture("rapid capture closed")
            return
        if self.paused:
            print("cannot start rapid capture while recording is paused")
            return
        if self.state_monitor:
            self.state_monitor.suspend()
        self.rapid_group_counter += 1
        self.rapid_group = self.rapid_group_counter
        print(f"rapid capture {self.rapid_group} armed; perform every jump click now")

    def end_rapid_capture(self, message):
        group = self.rapid_group
        self.rapid_group = None
        if self.state_monitor:
            self.state_monitor.resume()
        print(f"{message}: group {group}")

    def mark_last(self, field, unique=False):
        if not self.events:
            print(f"cannot mark {field}: no recorded action")
            return
        if unique:
            for event in self.events:
                event.pop(field, None)
        self.events[-1][field] = True
        annotation = {
            "record_type": "annotation",
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "procedure": self.args.name,
            "action_index": len(self.events),
            "field": field,
            "value": True,
            "unique": bool(unique),
        }
        self.jsonl_file.write(json.dumps(annotation, ensure_ascii=False) + "\n")
        self.jsonl_file.flush()
        print(f"marked action {len(self.events)} as {field}")

    def save_route(self):
        if any(event.get("rapid_group") is not None for event in self.events):
            lines = [
                "# This recording contains an atomic F5 rapid-click group.",
                "# Replay the generated .procedure.json with procedure_runner.py;",
                "# the legacy tuple runner cannot guarantee safe inter-click timing.",
                f"{self.args.name} = []",
            ]
            self.py_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"wrote {self.jsonl_path}")
            print(f"wrote guarded legacy stub {self.py_path}")
            if self.args.smart:
                self.save_smart_procedure()
            return

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
        if self.args.smart:
            self.save_smart_procedure()

    def save_smart_procedure(self):
        samples = self.state_monitor.samples if self.state_monitor else []
        with open(self.states_path, "w", encoding="utf-8") as file:
            for sample in samples:
                file.write(json.dumps(sample, ensure_ascii=False) + "\n")

        schedule = None
        if self.args.interval_minutes is not None:
            schedule = {
                "interval_minutes": self.args.interval_minutes,
                "initial_delay_minutes": self.args.initial_delay_minutes,
                "lead_seconds": max(0.0, self.args.lead_seconds),
                "before_routes": self.args.before_route,
                "after_routes": self.args.after_route,
            }
        procedure = build_recorded_procedure(
            name=self.args.name,
            events=self.events,
            state_samples=samples,
            metadata={
                "map": self.args.map_name,
                "start_coordinate": coordinate_tuple(self.args.start) if self.args.start else None,
                "target_coordinate": coordinate_tuple(self.args.target) if self.args.target else None,
                "schedule": schedule,
                "enabled": True,
                "reference_size": [
                    self.automation.reference_width,
                    self.automation.reference_height,
                ],
                "rapid_max_gap_seconds": self.args.rapid_max_gap,
            },
            recording_ended_at=time.monotonic(),
        )
        content = json.dumps(procedure, ensure_ascii=False, indent=2) + "\n"
        self.procedure_path.write_text(content, encoding="utf-8")
        print(f"wrote {self.states_path}")
        print(f"wrote {self.procedure_path}")

        if self.args.install:
            install_path = Path("procedures") / f"{self.args.name}.json"
            install_path.parent.mkdir(parents=True, exist_ok=True)
            install_path.write_text(content, encoding="utf-8")
            print(f"installed {install_path}")


def format_event(event):
    if event["type"] == "click":
        rapid = f" rapid_group={event['rapid_group']}" if event.get("rapid_group") is not None else ""
        return (
            f"click ref={tuple(event['reference'])} delay={event['delay']} "
            f"duration={event['duration']}{rapid}"
        )
    return (
        f"drag ref={tuple(event['start_reference'])}->{tuple(event['end_reference'])} "
        f"delay={event['delay']} duration={event['duration']} distance={event['distance']}"
    )


def main():
    args = parse_args()
    if args.install and not args.smart:
        raise SystemExit("--install requires --smart")
    if args.rapid_max_gap <= 0:
        raise SystemExit("--rapid-max-gap must be positive")
    recorder = ControlRecorder(args)
    print("Recording manual control.")
    if args.smart:
        print("F5 starts/ends an atomic rapid-click group and pauses OCR during the group.")
        print("F6 verifies the state after the last action. F7 marks the last action as the refresh anchor.")
    print("F8 pauses/resumes. F9 stops and writes the route files.")
    print("Operate the game window now.")

    mouse_listener = mouse.Listener(on_click=recorder.on_click)
    keyboard_listener = keyboard.Listener(on_press=recorder.on_key_press)
    recorder.start()
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
        recorder.stop_monitor()
        recorder.save_route()
        recorder.close()


if __name__ == "__main__":
    main()
