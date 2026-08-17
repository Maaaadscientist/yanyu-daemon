import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from runtime_control import (
    CONTINUE_STEP,
    RESTART_TASK,
    AutomationRecoveryHandoff,
    ResumeValidationError,
    RuntimeControl,
)


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
            control = self.make_control(
                directory,
                monotonic=lambda: clock[0],
                synthetic_grace_seconds=0.2,
                input_pause_policy="immediate",
            )
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

    def test_resume_abandons_the_old_position_and_issues_a_restart_ticket(self):
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

            control.request_resume(source="test", mode=RESTART_TASK)
            with self.assertRaises(AutomationRecoveryHandoff) as handoff:
                control.wait_if_paused()

            self.assertFalse(control.is_paused)
            self.assertEqual(handoff.exception.mode, RESTART_TASK)
            resumed = control.consume_resume_checkpoint()
            self.assertEqual(resumed["recovery_mode"], "restart_task")
            self.assertEqual(resumed["next_action_index"], 1)
            self.assertEqual(resumed["abandoned_checkpoint"]["coordinate"], [29, 3])
            self.assertEqual(current["coordinate"], [28, 11])

    def test_normal_restart_requires_a_readable_current_game_state(self):
        with tempfile.TemporaryDirectory() as directory:
            control = self.make_control(directory)
            control.set_state_provider(lambda: {"map": None, "coordinate": None})
            with self.assertRaises(ResumeValidationError):
                control._validate_restart_state()

    def test_resume_preparer_can_restore_a_login_session_before_restart(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            control = self.make_control(directory)
            control.set_context(task="bear5", route="bear5", next_action_index=6)
            control.set_resume_preparer(
                lambda **request: calls.append(request) or {
                    "state": "in_game",
                    "map": "逻邪河谷",
                    "coordinate": [12, 6],
                }
            )
            control.request_pause(reason="session_login_provider")
            control.request_resume(source="web", mode=RESTART_TASK)
            with self.assertRaises(AutomationRecoveryHandoff):
                control.wait_if_paused()

            ticket = control.consume_resume_checkpoint()

        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["force"])
        self.assertEqual(ticket["recovery_state"]["map"], "逻邪河谷")
        self.assertEqual(ticket["abandoned_checkpoint"]["next_action_index"], 6)

    def test_continue_step_keeps_the_route_and_next_action_after_strict_state_validation(self):
        current = {"map": "泉州", "coordinate": [17, 3]}
        with tempfile.TemporaryDirectory() as directory:
            control = self.make_control(directory)
            control.set_state_provider(lambda: current)
            control.set_context(
                task="bear4",
                route="bear4",
                route_index=1,
                next_action_index=10,
                phase="before_action",
            )
            control.request_pause(reason="pause_hotkey")
            control._capture_checkpoint_state()
            control.request_resume(source="test", mode=CONTINUE_STEP)
            with self.assertRaises(AutomationRecoveryHandoff) as handoff:
                control.wait_if_paused()

            ticket = control.consume_resume_checkpoint()

        self.assertEqual(ticket["recovery_mode"], CONTINUE_STEP)
        self.assertEqual(handoff.exception.mode, CONTINUE_STEP)
        self.assertEqual(ticket["route_index"], 1)
        self.assertEqual(ticket["next_action_index"], 10)
        self.assertEqual(ticket["recovery_state"]["coordinate"], [17, 3])

    def test_a_pending_resume_request_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            control = self.make_control(directory)
            control.set_context(task="bear4", route="bear4", next_action_index=10)
            control.request_pause(reason="pause_hotkey")

            first = control.request_resume(source="web", mode=RESTART_TASK)
            second = control.request_resume(source="web", mode=CONTINUE_STEP)

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertTrue(control.status_snapshot()["resume_pending"])

    def test_continue_validator_rejects_a_route_changed_by_an_update(self):
        with tempfile.TemporaryDirectory() as directory:
            control = self.make_control(directory)
            control.set_state_provider(lambda: {"map": "泉州", "coordinate": [17, 3]})
            control.set_continue_validator(Mock(side_effect=ValueError("checkpoint route changed")))
            control.set_context(
                task="bear4",
                route="old_bear4",
                route_index=1,
                next_action_index=10,
                phase="before_action",
            )
            control.request_pause(reason="pause_hotkey")
            control._capture_checkpoint_state()

            with self.assertRaisesRegex(ResumeValidationError, "route changed"):
                control._validate_resume_state()

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
            with self.assertRaises(AutomationRecoveryHandoff):
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
            self.assertEqual(
                [item.keys for item in hotkey_instances],
                [
                    "<ctrl>+<alt>+r",
                    "<ctrl>+<alt>+<shift>+r",
                    "<ctrl>+<alt>+p",
                    "<ctrl>+c",
                ],
            )
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
            self.assertFalse(control.is_paused)

    def test_mouse_takeover_requires_sustained_movement(self):
        clock = [20.0]
        with tempfile.TemporaryDirectory() as directory:
            control = self.make_control(
                directory,
                monotonic=lambda: clock[0],
                input_pause_policy="gesture",
                takeover_window_seconds=0.5,
                takeover_required_seconds=0.2,
                takeover_max_gap_seconds=0.11,
            )
            control.set_context(task="bear5", route="bear5", next_action_index=5)
            control._on_mouse_move(0, 0)
            for index in range(1, 4):
                clock[0] += 0.1
                control._on_mouse_move(index * 5, 0)

            self.assertTrue(control.is_paused)
            self.assertEqual(control.pending_checkpoint()["reason"], "mouse_takeover_gesture")

    def test_cancelled_takeover_hold_is_counted_as_interruption_time(self):
        clock = [20.0]
        with tempfile.TemporaryDirectory() as directory:
            control = self.make_control(
                directory,
                monotonic=lambda: clock[0],
                input_pause_policy="gesture",
                takeover_window_seconds=0.5,
                takeover_required_seconds=0.4,
                takeover_max_gap_seconds=0.11,
            )
            control._on_mouse_move(0, 0)
            clock[0] += 0.1
            control._on_mouse_move(5, 0)
            self.assertTrue(control.status_snapshot()["takeover"]["active"])

            clock[0] += 0.6
            control._update_takeover(control._gesture_detector.poll(now=clock[0]))

            self.assertFalse(control.status_snapshot()["takeover"]["active"])
            self.assertAlmostEqual(control.total_pause_seconds, 0.6)


if __name__ == "__main__":
    unittest.main()
