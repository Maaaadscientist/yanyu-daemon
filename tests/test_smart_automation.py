import json
import threading
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tracking_click
from recording_analyzer import read_events
from smart_automation import (
    GameState,
    ProcedureExecutionError,
    ProcedureError,
    ProcedureResult,
    SmartProcedureRunner,
    StateReadError,
    StateTimeout,
    TextObservation,
    VisionGameStateReader,
    action_timing_key,
    build_recorded_procedure,
    collapse_recorded_events,
    find_text_observation,
    load_procedures,
    parse_game_state,
    validate_procedure,
)
from tracking_click import next_due_from_anchor, task_start_time, update_lead_time


class FakeAutomation:
    reference_width = 2150
    reference_height = 1668

    def __init__(self):
        self.clicks = []
        self.drags = []
        self.rapid_sequences = []
        self.alignment_requests = []
        self.focus_count = 0

    def focus_window(self):
        self.focus_count += 1
        return True

    def click_reference(self, point):
        self.clicks.append(point)

    def drag_reference(self, start, end, duration=0.5):
        self.drags.append((start, end, duration))

    def rapid_click_reference(self, points, intervals, max_gap_seconds):
        points = list(points)
        self.rapid_sequences.append((points, list(intervals), max_gap_seconds))
        return SimpleNamespace(
            clicked_at=tuple(datetime.now() for _ in points),
            gaps=tuple(float(value) for value in intervals),
        )

    def align_reference_point(
        self,
        alignment_image,
        point,
        min_matches=40,
        min_inlier_ratio=0.45,
        max_rotation_degrees=4.0,
    ):
        self.alignment_requests.append((alignment_image, point))
        return SimpleNamespace(
            point=(321, 432),
            matches=100,
            inliers=95,
            inlier_ratio=0.95,
            scale=1.0,
            rotation_degrees=0.0,
        )

    def run_actions(self, *args, **kwargs):
        raise RuntimeError("post-anchor save failed")

    def capture_screenshot(self, *args, **kwargs):
        return None


class FakeStateReader:
    def wait_for_state(self, expected, timeout):
        state = GameState(expected.get("map"), tuple(expected.get("coordinate")))
        return state, 0.25

    def find_text(self, text, region=None, exact=False):
        return TextObservation(text, 0.9, 0.90, 0.48, 0.02, 0.04)

    def wait_for_text(self, text, present=True, region=None, exact=False, timeout=5.0, stable_samples=2):
        observation = self.find_text(text, region=region, exact=exact) if present else None
        return observation, 0.25


class SequenceStateReader(VisionGameStateReader):
    def __init__(self, states):
        self.states = iter(states)

    def read_state(self):
        return next(self.states)


class SmartAutomationTests(unittest.TestCase):
    def test_parse_game_state_joins_map_and_coordinate_line(self):
        observations = [
            TextObservation("大理", 0.99, 0.883, 0.71, 0.04, 0.03),
            TextObservation("（28,11）", 0.98, 0.936, 0.709, 0.05, 0.03),
            TextObservation("牛", 0.95, 0.89, 0.55, 0.04, 0.03),
        ]

        state = parse_game_state(observations)

        self.assertEqual(state.map_name, "大理")
        self.assertEqual(state.coordinate, (28, 11))

    def test_parse_game_state_accepts_ocr_period_as_coordinate_separator(self):
        observations = [
            TextObservation("大理", 0.99, 0.883, 0.71, 0.04, 0.03),
            TextObservation("（29.6）", 0.80, 0.936, 0.709, 0.05, 0.03),
        ]

        state = parse_game_state(observations)

        self.assertEqual(state.map_name, "大理")
        self.assertEqual(state.coordinate, (29, 6))

    def test_parse_game_state_does_not_use_left_quest_text_as_the_map_name(self):
        observations = [
            TextObservation("项府风云", 0.99, 0.05, 0.70, 0.10, 0.03),
            TextObservation("（12,6）", 0.98, 0.936, 0.709, 0.05, 0.03),
        ]

        state = parse_game_state(observations)

        self.assertIsNone(state.map_name)
        self.assertEqual(state.coordinate, (12, 6))

    def test_wait_for_state_requires_stable_samples(self):
        reader = SequenceStateReader(
            [
                GameState("大理", (28, 10)),
                GameState("大理", (28, 11)),
                GameState("大理", (28, 12)),
                GameState("大理", (28, 11)),
                GameState("大理", (28, 11)),
            ]
        )

        state, _ = reader.wait_for_state(
            {"map": "大理", "coordinate": [28, 11]},
            timeout=2.0,
            poll_seconds=0.0,
        )

        self.assertEqual(state.coordinate, (28, 11))

    def test_parse_game_state_accepts_map_and_coordinate_in_one_ocr_observation(self):
        state = parse_game_state(
            [
                TextObservation("逻邪河谷（12.6）", 0.8, 0.88, 0.709, 0.11, 0.03),
            ]
        )

        self.assertEqual(state.map_name, "逻邪河谷")
        self.assertEqual(state.coordinate, (12, 6))

    def test_negative_state_guard_blocks_same_map_before_any_click(self):
        automation = FakeAutomation()
        procedure = {
            "schema_version": 1,
            "name": "leave_before_returning",
            "actions": [
                {
                    "type": "assert_not_state",
                    "expect": {"map": "大理"},
                },
                {"type": "click", "point": [1, 2]},
            ],
        }

        with self.assertRaises(ProcedureExecutionError):
            SmartProcedureRunner(
                automation,
                state_reader=SequenceStateReader([GameState("大理", (29, 3))]),
                timing_file=None,
            ).run(procedure)

        self.assertEqual(automation.clicks, [])

    def test_negative_state_guard_continues_when_current_map_is_different(self):
        automation = FakeAutomation()
        procedure = {
            "schema_version": 1,
            "name": "leave_before_returning",
            "actions": [
                {"type": "assert_not_state", "expect": {"map": "大理"}},
                {"type": "click", "point": [1, 2]},
            ],
        }

        result = SmartProcedureRunner(
            automation,
            state_reader=SequenceStateReader([GameState("乌思雪原", (12, 6))]),
            timing_file=None,
        ).run(procedure)

        self.assertEqual(result.actions_completed, 2)
        self.assertEqual(automation.clicks, [(1, 2)])

    def test_negative_map_guard_rejects_an_unreadable_map_name(self):
        automation = FakeAutomation()
        procedure = {
            "schema_version": 1,
            "name": "unknown_start",
            "actions": [{"type": "assert_not_state", "expect": {"map": "大理"}}],
        }

        with self.assertRaises(ProcedureExecutionError):
            SmartProcedureRunner(
                automation,
                state_reader=SequenceStateReader([GameState(None, (12, 6))]),
                timing_file=None,
            ).run(procedure)

    def test_negative_map_guard_can_allow_unknown_when_later_checks_are_safe(self):
        automation = FakeAutomation()
        procedure = {
            "schema_version": 1,
            "name": "unknown_but_guarded_later",
            "actions": [
                {"type": "assert_not_state", "expect": {"map": "大理"}, "allow_unknown": True},
                {"type": "click", "point": [1, 2]},
            ],
        }

        SmartProcedureRunner(
            automation,
            state_reader=SequenceStateReader([GameState(None, (12, 6))]),
            timing_file=None,
        ).run(procedure)

        self.assertEqual(automation.clicks, [(1, 2)])

    def test_recording_becomes_verified_actions(self):
        events = [
            {"type": "click", "reference": [900, 800], "delay": 1.0, "monotonic": 10.0},
            {
                "type": "click",
                "reference": [1950, 704],
                "delay": 2.0,
                "monotonic": 15.0,
                "refresh_anchor": True,
            },
        ]
        samples = [
            {"monotonic": 9.0, "state": {"map": "大理", "coordinate": [32, 18]}},
            {"monotonic": 11.0, "state": {"map": "大理", "coordinate": [28, 11]}},
            {"monotonic": 12.0, "state": {"map": "大理", "coordinate": [28, 11]}},
            {"monotonic": 16.0, "state": {"map": "大理", "coordinate": [28, 11]}},
            {"monotonic": 17.0, "state": {"map": "大理", "coordinate": [28, 11]}},
        ]

        procedure = build_recorded_procedure(
            name="dali_cow",
            events=events,
            state_samples=samples,
            metadata={"map": "大理", "schedule": {"interval_minutes": 180}},
            recording_ended_at=18.0,
        )

        self.assertEqual(procedure["actions"][0]["expect"]["coordinate"], [28, 11])
        self.assertNotIn("retries", procedure["actions"][0])
        self.assertTrue(procedure["actions"][1]["refresh_anchor"])

    def test_timing_key_survives_delay_and_index_changes_but_not_target_changes(self):
        action = {
            "type": "click",
            "label": "到达猪的位置",
            "point": [1150, 850],
            "expect": {"map": "大理", "coordinate": [29, 3]},
            "before_delay": 0.2,
        }
        retimed = {**action, "before_delay": 0.8}
        moved = {**action, "point": [1160, 850]}

        self.assertEqual(action_timing_key(action), action_timing_key(retimed))
        self.assertNotEqual(action_timing_key(action), action_timing_key(moved))

    def test_runner_returns_exact_marked_click_anchor(self):
        automation = FakeAutomation()
        runner = SmartProcedureRunner(
            automation,
            state_reader=FakeStateReader(),
            timing_file=None,
        )
        procedure = {
            "schema_version": 1,
            "name": "dali_cow",
            "enabled": True,
            "actions": [
                {
                    "type": "click",
                    "point": [900, 800],
                    "expect": {"map": "大理", "coordinate": [28, 11]},
                },
                {"type": "click", "point": [1950, 704], "refresh_anchor": True},
            ],
        }

        result = runner.run(procedure)

        self.assertEqual(automation.clicks, [(900, 800), (1950, 704)])
        self.assertIsNotNone(result.refresh_anchor)

    def test_runner_retries_a_reversible_ui_click_when_its_text_does_not_appear(self):
        class FlakyTextReader(FakeStateReader):
            def __init__(self):
                self.calls = 0

            def wait_for_text(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise StateTimeout("panel did not open")
                return super().wait_for_text(*args, **kwargs)

        automation = FakeAutomation()
        reader = FlakyTextReader()
        procedure = {
            "schema_version": 1,
            "name": "open_inventory",
            "actions": [
                {
                    "type": "click",
                    "point": [485, 1605],
                    "expect_text": "叫唤马车",
                    "retries": 1,
                }
            ],
        }

        SmartProcedureRunner(automation, state_reader=reader, timing_file=None).run(procedure)

        self.assertEqual(automation.clicks, [(485, 1605), (485, 1605)])
        self.assertEqual(reader.calls, 2)

    def test_runner_prefers_recorded_text_over_stale_pixel(self):
        automation = FakeAutomation()
        procedure = {
            "schema_version": 1,
            "name": "text_click",
            "enabled": True,
            "actions": [
                {
                    "type": "click",
                    "point": [100, 100],
                    "target_text": "牛",
                    "text_region": [0.8, 0.3, 1.0, 0.6],
                }
            ],
        }

        SmartProcedureRunner(automation, state_reader=FakeStateReader(), timing_file=None).run(procedure)

        self.assertEqual(automation.clicks, [(1956, 834)])

    def test_runner_can_offset_ocr_text_to_click_an_icon_hotspot(self):
        automation = FakeAutomation()
        procedure = {
            "schema_version": 1,
            "name": "pig_hotspot",
            "actions": [
                {
                    "type": "click",
                    "point": [1860, 704],
                    "target_text": "猪",
                    "target_required": True,
                    "exact_text": True,
                    "text_click_offset": [-111, 7],
                }
            ],
        }

        SmartProcedureRunner(automation, state_reader=FakeStateReader(), timing_file=None).run(procedure)

        self.assertEqual(automation.clicks, [(1846, 841)])

    def test_exact_text_does_not_treat_pigpen_as_pig(self):
        observations = [TextObservation("猪圈", 0.99, 0.9, 0.5, 0.04, 0.04)]

        with self.assertRaises(StateReadError):
            find_text_observation(observations, "猪", exact=True)

        self.assertEqual(find_text_observation(observations, "猪", exact=False).text, "猪圈")

    def test_recorded_rapid_group_collapses_to_one_atomic_action(self):
        events = [
            {
                "type": "click",
                "reference": [100, 200],
                "delay": 1.0,
                "monotonic": 10.0,
                "rapid_group": 1,
            },
            {
                "type": "click",
                "reference": [200, 300],
                "delay": 0.18,
                "monotonic": 10.18,
                "rapid_group": 1,
            },
            {
                "type": "click",
                "reference": [300, 400],
                "delay": 0.17,
                "monotonic": 10.35,
                "rapid_group": 1,
                "checkpoint": True,
            },
        ]

        collapsed = collapse_recorded_events(events, rapid_max_gap_seconds=0.4)

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]["type"], "rapid_clicks")
        self.assertEqual(collapsed[0]["points"], [[100, 200], [200, 300], [300, 400]])
        self.assertEqual(collapsed[0]["intervals"], [0.18, 0.17])
        self.assertTrue(collapsed[0]["checkpoint"])

    def test_raw_recording_annotations_rebuild_checkpoint_and_unique_anchor(self):
        records = [
            {"type": "click", "delay": 0.1, "reference": [1, 2]},
            {"type": "click", "delay": 0.1, "reference": [3, 4]},
            {"record_type": "annotation", "action_index": 1, "field": "checkpoint", "value": True},
            {
                "record_type": "annotation",
                "action_index": 1,
                "field": "refresh_anchor",
                "value": True,
                "unique": True,
            },
            {
                "record_type": "annotation",
                "action_index": 2,
                "field": "refresh_anchor",
                "value": True,
                "unique": True,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "recording.jsonl")
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            events = read_events(path)

        self.assertTrue(events[0]["checkpoint"])
        self.assertNotIn("refresh_anchor", events[0])
        self.assertTrue(events[1]["refresh_anchor"])

    def test_recording_with_lead_time_adds_a_gate_before_refresh_click(self):
        events = [
            {"type": "click", "reference": [1859, 704], "delay": 1.0, "monotonic": 10.0},
            {
                "type": "click",
                "reference": [1282, 758],
                "delay": 0.5,
                "monotonic": 11.0,
                "refresh_anchor": True,
            },
        ]

        procedure = build_recorded_procedure(
            name="scheduled_pig",
            events=events,
            state_samples=[],
            metadata={
                "schedule": {"interval_minutes": 60, "lead_seconds": 45},
                "reference_size": [2150, 1668],
            },
            recording_ended_at=12.0,
        )

        self.assertTrue(procedure["actions"][0]["wait_until_scheduled"])
        self.assertTrue(procedure["actions"][1]["refresh_anchor"])
        validate_procedure(procedure, require_actions=True)

    def test_runner_executes_rapid_clicks_as_one_automation_call(self):
        automation = FakeAutomation()
        procedure = {
            "schema_version": 1,
            "name": "multi_jump",
            "actions": [
                {
                    "type": "rapid_clicks",
                    "points": [[100, 200], [200, 300], [300, 400]],
                    "intervals": [0.18, 0.17],
                    "max_gap_seconds": 0.4,
                }
            ],
        }

        SmartProcedureRunner(automation, state_reader=FakeStateReader(), timing_file=None).run(procedure)

        self.assertEqual(
            automation.rapid_sequences,
            [([(100, 200), (200, 300), (300, 400)], [0.18, 0.17], 0.4)],
        )

    def test_runner_aligns_a_world_map_click_and_verifies_destination_text(self):
        automation = FakeAutomation()
        events = []
        procedure = {
            "schema_version": 1,
            "name": "aligned_destination",
            "actions": [
                {
                    "type": "aligned_click",
                    "point": [100, 200],
                    "alignment_image": "assets/world_map_reference.jpg",
                    "expect_text": "大理",
                    "expect_text_exact": True,
                }
            ],
        }

        SmartProcedureRunner(automation, state_reader=FakeStateReader(), timing_file=None).run(
            procedure,
            action_logger=events.append,
        )

        self.assertEqual(automation.alignment_requests, [("assets/world_map_reference.jpg", (100, 200))])
        self.assertEqual(automation.clicks, [(321, 432)])
        dispatched = next(event for event in events if event["event"] == "smart_action_dispatched")
        self.assertEqual(dispatched["aligned_point"], [321, 432])
        self.assertEqual(dispatched["alignment_inliers"], 95)

    def test_validation_rejects_rapid_plan_over_its_gap_limit(self):
        procedure = {
            "schema_version": 1,
            "name": "unsafe_jump",
            "actions": [
                {
                    "type": "rapid_clicks",
                    "points": [[1, 2], [3, 4]],
                    "intervals": [0.6],
                    "max_gap_seconds": 0.4,
                }
            ],
        }

        with self.assertRaises(ProcedureError):
            validate_procedure(procedure, require_actions=True)

    def test_runner_exposes_anchor_when_later_action_fails(self):
        class FailingAutomation(FakeAutomation):
            def click_reference(self, point):
                super().click_reference(point)
                if len(self.clicks) == 2:
                    raise RuntimeError("later action failed")

        procedure = {
            "schema_version": 1,
            "name": "anchored",
            "enabled": True,
            "actions": [
                {"type": "click", "point": [1, 2], "refresh_anchor": True},
                {"type": "click", "point": [3, 4]},
            ],
        }

        with self.assertRaises(ProcedureExecutionError) as raised:
            SmartProcedureRunner(FailingAutomation(), timing_file=None).run(procedure)

        self.assertIsNotNone(raised.exception.refresh_anchor)
        self.assertEqual(raised.exception.action_index, 2)

    def test_scheduled_gate_wait_is_reported_for_lead_time_learning(self):
        automation = FakeAutomation()
        events = []
        procedure = {
            "schema_version": 1,
            "name": "scheduled_resource",
            "actions": [
                {
                    "type": "click",
                    "point": [1, 2],
                    "wait_until_scheduled": True,
                    "refresh_anchor": True,
                }
            ],
        }
        target = datetime.now() + timedelta(seconds=30)

        with patch("smart_automation.sleep_until_datetime", return_value=12.5) as wait:
            result = SmartProcedureRunner(automation, timing_file=None).run(
                procedure,
                not_before=target,
                action_logger=events.append,
            )

        wait.assert_called_once_with(target, stop_event=None)
        self.assertEqual(result.scheduled_wait_seconds, 12.5)
        gate = next(event for event in events if event["event"] == "smart_scheduled_gate_reached")
        self.assertEqual(gate["scheduled_wait_seconds"], 12.5)
        self.assertEqual(gate["scheduled_for"], target.isoformat(timespec="milliseconds"))

    def test_scheduler_stop_hotkey_is_idempotent_and_logged(self):
        stop_event = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory, "events.jsonl")
            hotkey = tracking_click.SchedulerStopHotkey("<ctrl>+c", stop_event, log_path)

            hotkey.request_stop()
            hotkey.request_stop()

            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(stop_event.is_set())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "scheduler_hotkey_stop_requested")
        self.assertEqual(events[0]["hotkey"], "<ctrl>+c")

    def test_recording_keeps_text_target_and_pixel_fallback(self):
        event = {"type": "click", "reference": [1950, 704], "delay": 0.5, "monotonic": 10.0}
        samples = [
            {
                "monotonic": 9.5,
                "state": {"map": "大理", "coordinate": [28, 11]},
                "observations": [
                    {
                        "text": "牛",
                        "confidence": 0.98,
                        "region": [0.88, 0.40, 0.94, 0.46],
                    }
                ],
            }
        ]

        procedure = build_recorded_procedure(
            name="dali_cow",
            events=[event],
            state_samples=samples,
            metadata={"reference_size": [2150, 1668]},
            recording_ended_at=11.0,
        )

        action = procedure["actions"][0]
        self.assertEqual(action["target_text"], "牛")
        self.assertEqual(action["point"], [1950, 704])

    def test_recording_learns_sidebar_icon_offset_from_nearby_text(self):
        event = {"type": "click", "reference": [1859, 704], "delay": 0.5, "monotonic": 10.0}
        samples = [
            {
                "monotonic": 9.5,
                "state": {"map": "大理", "coordinate": [29, 3]},
                "observations": [
                    {
                        "text": "猪",
                        "confidence": 0.98,
                        "region": [0.89, 0.40, 0.95, 0.46],
                    }
                ],
            }
        ]

        procedure = build_recorded_procedure(
            name="pig_icon",
            events=[event],
            state_samples=samples,
            metadata={"reference_size": [2150, 1668]},
            recording_ended_at=11.0,
        )

        action = procedure["actions"][0]
        self.assertEqual(action["target_text"], "猪")
        self.assertEqual(action["text_click_offset"], [-119, -13])

    def test_installed_procedure_overrides_builtin_draft(self):
        builtin = {
            "dali_cow": {
                "schema_version": 1,
                "name": "dali_cow",
                "enabled": False,
                "actions": [],
            }
        }
        installed = {
            "schema_version": 1,
            "name": "dali_cow",
            "enabled": True,
            "actions": [{"type": "click", "point": [1, 2]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "dali_cow.json").write_text(json.dumps(installed), encoding="utf-8")
            procedures = load_procedures(directory, builtins=builtin)

        self.assertTrue(procedures["dali_cow"]["enabled"])
        self.assertEqual(len(procedures["dali_cow"]["actions"]), 1)

    def test_loader_expands_reusable_procedure_segments(self):
        segment = {
            "schema_version": 1,
            "name": "_travel",
            "enabled": False,
            "library": True,
            "actions": [{"type": "click", "point": [1, 2]}],
        }
        route = {
            "schema_version": 1,
            "name": "resource",
            "enabled": True,
            "actions": [
                {"type": "include", "procedure": "_travel"},
                {"type": "click", "point": [3, 4]},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "_travel.json").write_text(json.dumps(segment), encoding="utf-8")
            Path(directory, "resource.json").write_text(json.dumps(route), encoding="utf-8")

            procedures = load_procedures(directory)

        self.assertEqual([action["point"] for action in procedures["resource"]["actions"]], [[1, 2], [3, 4]])
        self.assertEqual(procedures["resource"]["actions"][0]["segment"], "_travel")

    def test_loader_rejects_circular_procedure_segments(self):
        first = {
            "schema_version": 1,
            "name": "first",
            "actions": [{"type": "include", "procedure": "second"}],
        }
        second = {
            "schema_version": 1,
            "name": "second",
            "actions": [{"type": "include", "procedure": "first"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "first.json").write_text(json.dumps(first), encoding="utf-8")
            Path(directory, "second.json").write_text(json.dumps(second), encoding="utf-8")

            with self.assertRaises(ProcedureError):
                load_procedures(directory)

    def test_installed_dali_pig_keeps_the_verified_six_step_branch(self):
        procedure = load_procedures("procedures")["dali_pig"]
        coordinates = [
            tuple(action["expect"]["coordinate"])
            for action in procedure["actions"]
            if action.get("expect", {}).get("coordinate")
        ]

        self.assertEqual(
            coordinates[-6:],
            [(26, 11), (27, 10), (27, 8), (29, 6), (28, 3), (29, 3)],
        )
        anchors = [action for action in procedure["actions"] if action.get("refresh_anchor")]
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["target_text"], "宰杀")
        self.assertEqual(procedure["actions"][-1]["type"], "wait_text")

    def test_scheduler_due_time_uses_refresh_anchor(self):
        anchor = datetime(2026, 8, 16, 12, 0, 0, 125000)

        due = next_due_from_anchor(anchor, 180, 1.5)

        self.assertEqual(due, anchor + timedelta(minutes=180, seconds=1.5))

    def test_external_smart_run_syncs_only_its_exact_refresh_schedule(self):
        anchor = datetime(2026, 8, 17, 5, 3, 33, 629000)
        result = ProcedureResult(
            "dali_cow",
            anchor - timedelta(seconds=25),
            anchor + timedelta(seconds=2),
            anchor,
            19,
        )
        procedure = {
            "name": "dali_cow",
            "schedule": {"interval_minutes": 180, "lead_seconds": 75},
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory, "state.json")
            state_path.write_text(json.dumps({"unrelated": {"last_status": "ok"}}), encoding="utf-8")
            due = tracking_click.sync_external_procedure_result(state_path, procedure, result)
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(due, anchor + timedelta(minutes=60))
        self.assertEqual(state["dali_cow"]["next_due"], due.isoformat(timespec="milliseconds"))
        self.assertEqual(state["dali_cow"]["last_status"], "external_ok")
        self.assertEqual(state["dali_cow"]["lead_seconds"], 75.0)
        self.assertIn("unrelated", state)

    def test_scheduler_registers_installed_procedure_without_code_edit(self):
        installed = {
            "schema_version": 1,
            "name": "new_sheep",
            "enabled": True,
            "schedule": {
                "interval_minutes": 180,
                "before_routes": ["sleep1"],
                "after_routes": ["save"],
            },
            "actions": [{"type": "click", "point": [10, 20]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "new_sheep.json").write_text(json.dumps(installed), encoding="utf-8")
            tracking_click.register_smart_procedures(directory)
            try:
                self.assertIn("new_sheep", tracking_click.TASKS)
                self.assertEqual(tracking_click.TASKS["new_sheep"].routes, ("sleep1", "new_sheep", "save"))
                self.assertIn("new_sheep", tracking_click.GROUPS["smart"])
            finally:
                tracking_click.register_smart_procedures(Path(directory, "empty"))

    def test_failure_after_anchor_preserves_full_refresh_interval(self):
        anchor = datetime.now()

        class FakeSmartRunner:
            def run(self, *args, **kwargs):
                return ProcedureResult("smart_test", anchor, anchor, anchor, 1)

        task = tracking_click.ScheduledTask("smart_test", 180, 0, ("smart_test", "save"))
        state = {
            "smart_test": {
                "next_due": datetime.now(),
                "last_started": None,
                "last_completed": None,
                "last_refresh_anchor": None,
                "last_status": "new",
                "failures": 0,
            }
        }
        old_procedures = tracking_click.PROCEDURES
        tracking_click.PROCEDURES = {"smart_test": {"enabled": True}}
        try:
            with tempfile.TemporaryDirectory() as directory:
                args = SimpleNamespace(
                    state_file=str(Path(directory, "state.json")),
                    log_jsonl=str(Path(directory, "events.jsonl")),
                    capture="none",
                    capture_dir=str(Path(directory, "captures")),
                    log_actions=False,
                    dry_run=False,
                    completion_padding_seconds=0.0,
                    retry_minutes=10.0,
                )
                tracking_click.run_task(task, FakeAutomation(), FakeSmartRunner(), args, state)
        finally:
            tracking_click.PROCEDURES = old_procedures

        self.assertEqual(state["smart_test"]["last_status"], "post_anchor_failed")
        self.assertEqual(state["smart_test"]["next_due"], anchor + timedelta(minutes=180))

    def test_due_filter_keeps_inactive_tasks_out_of_execution(self):
        now = datetime.now() - timedelta(seconds=1)
        state = {"active": {"next_due": now}, "inactive": {"next_due": now}}

        due = tracking_click.due_task_names(state, None, {"active": object()})

        self.assertEqual(due, ["active"])

    def test_early_start_window_precedes_already_missed_legacy_task(self):
        now = datetime.now()
        state = {
            "overdue": {"next_due": now - timedelta(hours=1), "lead_seconds": 0},
            "precise": {"next_due": now + timedelta(seconds=30), "lead_seconds": 60},
        }

        due = tracking_click.due_task_names(state, None)

        self.assertEqual(due, ["precise", "overdue"])

    def test_missed_precise_task_precedes_older_legacy_backlog(self):
        now = datetime.now()
        state = {
            "legacy": {"next_due": now - timedelta(hours=9), "lead_seconds": 0},
            "precise": {"next_due": now - timedelta(hours=1), "lead_seconds": 75},
        }

        due = tracking_click.due_task_names(state, None)

        self.assertEqual(due, ["precise", "legacy"])

    def test_scheduler_reserves_a_nearby_future_precision_window(self):
        now = datetime.now()
        state = {
            "overdue": {"next_due": now - timedelta(hours=1), "lead_seconds": 0},
            "precise": {"next_due": now + timedelta(seconds=90), "lead_seconds": 30},
        }

        held = tracking_click.due_task_names(state, None, precision_reserve_seconds=120)
        not_held = tracking_click.due_task_names(state, None, precision_reserve_seconds=30)

        self.assertEqual(held, [])
        self.assertEqual(not_held, ["overdue"])

    def test_task_becomes_due_at_lead_time_before_refresh_target(self):
        target = datetime.now() + timedelta(seconds=30)
        task_state = {"next_due": target, "lead_seconds": 45.0}

        self.assertEqual(task_start_time(task_state), target - timedelta(seconds=45))

    def test_new_smart_task_starts_now_but_targets_refresh_after_initial_lead(self):
        task = tracking_click.ScheduledTask("smart", 60, 0, ("smart",), lead_seconds=75.0)
        before = datetime.now()
        with tempfile.TemporaryDirectory() as directory:
            state = tracking_click.load_state(Path(directory, "missing.json"), {"smart": task})
        after = datetime.now()

        start = task_start_time(state["smart"])
        self.assertLessEqual(before, start)
        self.assertLessEqual(start, after)
        self.assertEqual(state["smart"]["next_due"] - start, timedelta(seconds=75))

    def test_lead_time_learns_actual_pre_anchor_duration_with_margin(self):
        task_state = {"lead_seconds": 75.0, "lead_samples": 0}

        update_lead_time(task_state, 42.0)

        self.assertEqual(task_state["lead_seconds"], 44.0)
        self.assertEqual(task_state["lead_samples"], 1)


if __name__ == "__main__":
    unittest.main()
