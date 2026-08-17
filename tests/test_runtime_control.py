import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime_control import ResumeValidationError, RuntimeControl


class RuntimeControlTests(unittest.TestCase):
    def make_control(self, directory, **kwargs):
        return RuntimeControl(
            stop_event=threading.Event(),
            state_file=Path(directory, "runtime.json"),
            resume_delay_seconds=0,
            detect_human_input=False,
            **kwargs,
        )

    def test_synthetic_mouse_events_are_ignored_but_later_human_input_pauses(self):
        clock = [10.0]
        with tempfile.TemporaryDirectory() as directory:
            control = self.make_control(directory, monotonic=lambda: clock[0], synthetic_grace_seconds=0.2)
            control.set_context(task="pig1", route="pig1", next_action_index=12, phase="before_action")

            with control.automation_input():
                control._handle_physical_input("mouse_move", {"x": 1, "y": 2})
            self.assertFalse(control.is_paused)

            clock[0] += 0.3
            control._handle_physical_input("mouse_move", {"x": 3, "y": 4})

            self.assertTrue(control.is_paused)
            checkpoint = control.pending_checkpoint()
            self.assertEqual(checkpoint["task"], "pig1")
            self.assertEqual(checkpoint["next_action_index"], 12)

    def test_resume_requires_the_recorded_map_and_coordinate(self):
        current = {"map": "大理", "coordinate": [29, 3]}
        with tempfile.TemporaryDirectory() as directory:
            control = self.make_control(directory)
            control.set_state_provider(lambda: current)
            control.set_context(task="dali_pig", route="dali_pig", next_action_index=8)
            control.request_pause(reason="keyboard")
            control._capture_checkpoint_state()

            current = {"map": "大理", "coordinate": [28, 11]}
            with self.assertRaises(ResumeValidationError):
                control._validate_resume_state()

            current = {"map": "大理", "coordinate": [29, 3]}
            control.request_resume(source="test")
            control.wait_if_paused()

            self.assertFalse(control.is_paused)
            resumed = control.consume_resume_checkpoint()
            self.assertEqual(resumed["coordinate"], [29, 3])

    def test_paused_checkpoint_survives_process_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.make_control(directory)
            first.set_context(task="cow2", route="cow2", route_index=2, next_action_index=21)
            first.request_pause(reason="mouse_click")

            second = self.make_control(directory)

            self.assertTrue(second.is_paused)
            self.assertEqual(second.pending_checkpoint()["task"], "cow2")
            raw = json.loads(Path(directory, "runtime.json").read_text(encoding="utf-8"))
            self.assertTrue(raw["paused"])

            second.request_resume(source="test", force=True)
            second.wait_if_paused()
            self.assertFalse(second.is_paused)

    def test_listeners_share_one_keyboard_listener_for_all_hotkeys(self):
        hotkey_instances = []
        keyboard_listeners = []
        mouse_listeners = []

        class FakeHotKey:
            @staticmethod
            def parse(value):
                return value

            def __init__(self, keys, callback):
                self.keys = keys
                self.callback = callback
                hotkey_instances.append(self)

            def press(self, _key):
                return None

            def release(self, _key):
                return None

        class FakeListener:
            def __init__(self, **callbacks):
                self.callbacks = callbacks
                self.started = False
                self.stopped = False

            def canonical(self, key):
                return key

            def start(self):
                self.started = True

            def stop(self):
                self.stopped = True

            def is_alive(self):
                return False

        class FakeKeyboardListener(FakeListener):
            def __init__(self, **callbacks):
                super().__init__(**callbacks)
                keyboard_listeners.append(self)

        class FakeMouseListener(FakeListener):
            def __init__(self, **callbacks):
                super().__init__(**callbacks)
                mouse_listeners.append(self)

        fake_pynput = SimpleNamespace(
            keyboard=SimpleNamespace(HotKey=FakeHotKey, Listener=FakeKeyboardListener),
            mouse=SimpleNamespace(Listener=FakeMouseListener),
        )
        with tempfile.TemporaryDirectory() as directory:
            control = RuntimeControl(
                stop_event=threading.Event(),
                state_file=Path(directory, "runtime.json"),
                detect_human_input=True,
            )
            with patch.dict(sys.modules, {"pynput": fake_pynput}):
                control.start_listeners(extra_hotkeys={"<ctrl>+c": lambda: None})

            self.assertEqual(len(keyboard_listeners), 1)
            self.assertEqual(len(mouse_listeners), 1)
            self.assertEqual([item.keys for item in hotkey_instances], ["<ctrl>+<alt>+r", "<ctrl>+c"])
            self.assertTrue(keyboard_listeners[0].started)
            self.assertTrue(mouse_listeners[0].started)

            control.stop_listeners()
            self.assertTrue(keyboard_listeners[0].stopped)
            self.assertTrue(mouse_listeners[0].stopped)

    def test_modifier_key_alone_does_not_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            control = RuntimeControl(
                stop_event=threading.Event(),
                state_file=Path(directory, "runtime.json"),
                detect_human_input=True,
            )

            control._on_key_press("Key.ctrl")
            self.assertFalse(control.is_paused)

            control._on_key_press("'x'")
            self.assertTrue(control.is_paused)


if __name__ == "__main__":
    unittest.main()
