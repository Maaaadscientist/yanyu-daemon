import json
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import coordinates
import tracking_click
from coordinates import bear1, bear2, bear7, bear14, bear_tianshan, cow2, pig1, pos
from monitor_server import MonitorData, MonitoringServer
from resource_catalog import MAP_COW_TASKS, ResourceLedger, infer_legacy_anchor_specs


class FakeRouteAutomation:
    def __init__(self, base_time, runtime_control=None):
        self.base_time = base_time
        self.runtime_control = runtime_control

    def run_actions(self, actions, *, action_logger, **_kwargs):
        skipped = set(_kwargs.get("skip_action_indexes", ()))
        for index, _action in enumerate(actions, start=1):
            if index in skipped:
                action_logger(
                    {"event": "legacy_action_skipped", "index": index, "reason": "resource_cooldown"}
                )
                continue
            dispatched_at = self.base_time + timedelta(seconds=index)
            action_logger(
                {
                    "event": "legacy_action_dispatched",
                    "index": index,
                    "dispatched_at": dispatched_at.isoformat(timespec="milliseconds"),
                }
            )

    def capture_screenshot(self, *_args, **_kwargs):
        return None


class FakeRuntimeControl:
    def __init__(self, total_pause_seconds):
        self.total_pause_seconds = total_pause_seconds
        self.is_paused = False
        self.context = {}

    def replace_context(self, **values):
        self.context = values

    def set_context(self, **values):
        self.context.update(values)

    def clear_checkpoint(self):
        return None


class ResourcesAndMonitorTests(unittest.TestCase):
    def test_legacy_livestock_routes_find_only_primary_confirmation_anchors(self):
        cases = (("pig1", pig1, 1), ("cow2", cow2, 3), ("bear7", bear7, 5), ("bear14", bear14, 4))
        for task, route, expected in cases:
            with self.subTest(task=task):
                specs = infer_legacy_anchor_specs(
                    task,
                    task,
                    route,
                    pos,
                    default_interval_minutes=180,
                )
                self.assertEqual(len(specs), expected)

        cow_specs = infer_legacy_anchor_specs(
            "cow2",
            "cow2",
            cow2,
            pos,
            default_interval_minutes=180,
        )
        self.assertEqual([spec.action_index for spec in cow_specs], [21, 27, 32])

    def test_changbai_route_tracks_one_bear_and_one_cow(self):
        changbai = infer_legacy_anchor_specs(
            "bear1", "bear1", bear1, pos, default_interval_minutes=60
        )
        tianshan = infer_legacy_anchor_specs(
            "bear_tianshan", "bear_tianshan", bear_tianshan, pos, default_interval_minutes=60
        )
        map_cow = infer_legacy_anchor_specs(
            "bear2", "bear2", bear2, pos, default_interval_minutes=60
        )

        self.assertEqual(
            [(item.action_index, item.label, item.category) for item in changbai],
            [(15, "长白山熊", "wild_bear"), (21, "长白山牛", "map_cow")],
        )
        self.assertEqual([(item.action_index, item.label) for item in tianshan], [(13, "天山熊")])
        self.assertEqual(map_cow[0].category, "map_cow")
        self.assertEqual((map_cow[0].action_index, map_cow[0].label), (14, "姑苏牛"))
        self.assertTrue(all(item.record_acquisition for item in (*changbai, *tianshan, *map_cow)))

    def test_every_legacy_regional_route_is_one_map_cow(self):
        expected = {
            "bear2": ("姑苏牛", 14),
            "bear3": ("杭州牛", 15),
            "bear4": ("泉州牛", 12),
            "bear5": ("洛阳牛", 14),
            "bear6": ("南阳渡牛", 13),
            "bear8": ("落霞镇牛", 12),
            "bear9": ("峨眉山牛", 11),
            "bear10": ("明月峰牛", 11),
            "bear11": ("龙泉镇牛", 13),
            "bear12": ("双王镇牛", 13),
            "bear13": ("华山牛", 16),
            "bear15": ("凤鸣集牛", 14),
        }

        self.assertEqual(MAP_COW_TASKS, {task: value[0] for task, value in expected.items()})
        for task_name, (label, action_index) in expected.items():
            with self.subTest(task=task_name):
                specs = infer_legacy_anchor_specs(
                    task_name,
                    task_name,
                    getattr(coordinates, task_name),
                    coordinates.pos,
                    default_interval_minutes=60,
                )
                self.assertEqual(len(specs), 1)
                self.assertEqual(
                    (specs[0].point_id, specs[0].label, specs[0].category, specs[0].action_index),
                    (f"{task_name}:1", label, "map_cow", action_index),
                )

    def test_map_cow_keeps_exact_schedule_and_counts_one_acquisition(self):
        base = datetime(2026, 8, 17, 10, 0, 0)
        task = tracking_click.ScheduledTask("bear2", 60, 0, ("bear2",))
        state = {
            "bear2": {
                "next_due": base,
                "last_started": None,
                "last_completed": None,
                "last_refresh_anchor": None,
                "last_status": "new",
                "failures": 0,
                "lead_seconds": 0,
                "lead_samples": 0,
                "resource_points": {},
                "human_pause_seconds": 0,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                state_file=str(Path(directory, "state.json")),
                log_jsonl=str(Path(directory, "events.jsonl")),
                capture="none",
                capture_dir=str(Path(directory, "captures")),
                log_actions=False,
                dry_run=False,
                completion_padding_seconds=0,
                retry_minutes=10,
            )
            ledger = ResourceLedger(Path(directory, "resources.jsonl"))
            tracking_click.run_task(
                task,
                FakeRouteAutomation(base),
                smart_runner=None,
                args=args,
                state=state,
                ledger=ledger,
            )
            records = ledger.recent()

        anchored_at = base + timedelta(seconds=14)
        self.assertEqual(state["bear2"]["last_refresh_anchor"], anchored_at)
        self.assertEqual(state["bear2"]["next_due"], anchored_at + timedelta(hours=1))
        self.assertEqual(state["bear2"]["resource_points"]["bear2:1"]["category"], "map_cow")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["point_label"], "姑苏牛")

    def test_old_dali_cow_state_migrates_from_three_hours_to_one_hour(self):
        anchor = datetime(2026, 8, 17, 11, 22, 53, 519000)
        task = tracking_click.ScheduledTask("dali_cow", 60, 0, ("dali_cow",), lead_seconds=75)
        raw = {
            "dali_cow": {
                "next_due": (anchor + timedelta(hours=3)).isoformat(timespec="milliseconds"),
                "last_refresh_anchor": anchor.isoformat(timespec="milliseconds"),
                "last_status": "ok",
                "lead_seconds": 75,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            path.write_text(json.dumps(raw), encoding="utf-8")

            state = tracking_click.load_state(path, {"dali_cow": task})

        self.assertEqual(state["dali_cow"]["next_due"], anchor + timedelta(hours=1))
        self.assertEqual(state["dali_cow"]["interval_minutes"], 60)

    def test_legacy_anchor_immediately_updates_point_state_and_ledger(self):
        base = datetime(2026, 8, 17, 10, 0, 0)
        task = tracking_click.ScheduledTask("pig1", 60, 0, ("pig1",))
        state = {
            "pig1": {
                "next_due": base,
                "last_started": None,
                "last_completed": None,
                "last_refresh_anchor": None,
                "last_status": "new",
                "failures": 0,
                "lead_seconds": 0,
                "lead_samples": 0,
                "resource_points": {},
                "human_pause_seconds": 0,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                state_file=str(Path(directory, "state.json")),
                log_jsonl=str(Path(directory, "events.jsonl")),
                capture="none",
                capture_dir=str(Path(directory, "captures")),
                log_actions=False,
                dry_run=False,
                completion_padding_seconds=0,
                retry_minutes=10,
            )
            ledger = ResourceLedger(Path(directory, "resources.jsonl"))

            tracking_click.run_task(
                task,
                FakeRouteAutomation(base),
                smart_runner=None,
                args=args,
                state=state,
                ledger=ledger,
            )

            records = ledger.recent()

        anchored_at = base + timedelta(seconds=12)
        self.assertEqual(state["pig1"]["last_refresh_anchor"], anchored_at)
        self.assertEqual(state["pig1"]["next_due"], anchored_at + timedelta(hours=1))
        self.assertEqual(state["pig1"]["resource_points"]["pig1:1"]["last_refresh_anchor"], anchored_at)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["point_id"], "pig1:1")

    def test_cooldown_livestock_point_skips_its_interaction_group(self):
        anchor = datetime.now()
        due = anchor + timedelta(hours=1)
        task = tracking_click.ScheduledTask("pig1", 60, 0, ("pig1",))
        state = {
            "pig1": {
                "next_due": due,
                "last_started": None,
                "last_completed": None,
                "last_refresh_anchor": anchor,
                "last_status": "partial_failed",
                "failures": 1,
                "lead_seconds": 0,
                "lead_samples": 0,
                "resource_points": {
                    "pig1:1": {
                        "label": "南岭猪",
                        "last_refresh_anchor": anchor,
                        "next_due": due,
                    }
                },
                "human_pause_seconds": 0,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                state_file=str(Path(directory, "state.json")),
                log_jsonl=str(Path(directory, "events.jsonl")),
                capture="none",
                capture_dir=str(Path(directory, "captures")),
                log_actions=True,
                dry_run=False,
                completion_padding_seconds=0,
                retry_minutes=10,
            )
            ledger = ResourceLedger(Path(directory, "resources.jsonl"))

            tracking_click.run_task(
                task,
                FakeRouteAutomation(anchor),
                smart_runner=None,
                args=args,
                state=state,
                ledger=ledger,
            )
            events = [json.loads(line) for line in Path(args.log_jsonl).read_text(encoding="utf-8").splitlines()]

        skipped_indexes = {
            event["index"]
            for event in events
            if event.get("event") == "legacy_action_skipped" and event.get("reason") == "resource_cooldown"
        }
        self.assertEqual(skipped_indexes, {10, 11, 12, 13})
        self.assertEqual(state["pig1"]["next_due"], due)
        self.assertEqual(ledger.recent(), [])

    def test_monitor_serves_status_and_accepts_ledger_adjustment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "state.json"
            anchor = datetime.now() - timedelta(hours=2)
            state_file.write_text(
                json.dumps(
                    {
                        "pig1": {
                            "next_due": (anchor + timedelta(hours=1)).isoformat(),
                            "last_refresh_anchor": anchor.isoformat(),
                            "last_status": "ok",
                            "interval_minutes": 60,
                            "resource_points": {},
                        }
                    }
                ),
                encoding="utf-8",
            )
            data = MonitorData(
                state_file=state_file,
                event_file=root / "events.jsonl",
                resource_history=root / "resources.jsonl",
            )
            server = MonitoringServer(host="127.0.0.1", port=0, data=data)
            server.start()
            host, port = server.address
            try:
                with urllib.request.urlopen(f"http://{host}:{port}/api/status") as response:
                    status = json.loads(response.read())
                request = urllib.request.Request(
                    f"http://{host}:{port}/api/acquisitions/adjust",
                    data=json.dumps(
                        {"task": "pig1", "category": "pen_livestock", "quantity": 2, "unit": "只"}
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    adjustment = json.loads(response.read())
            finally:
                server.stop()

        self.assertEqual(status["tasks"][0]["status"], "ready")
        self.assertTrue(adjustment["ok"])
        self.assertEqual(adjustment["record"]["quantity"], 2)

    def test_resumed_task_uses_original_pause_baseline_and_start_time(self):
        original_start = datetime.now() - timedelta(minutes=8)
        runtime = FakeRuntimeControl(total_pause_seconds=125)
        task = tracking_click.ScheduledTask("checkpoint_test", 60, 0, ("save",))
        state = {
            "checkpoint_test": {
                "next_due": datetime.now(),
                "last_started": None,
                "last_completed": None,
                "last_refresh_anchor": None,
                "last_status": "running",
                "failures": 0,
                "lead_seconds": 0,
                "lead_samples": 0,
                "resource_points": {},
                "human_pause_seconds": 4,
            }
        }
        checkpoint = {
            "task": "checkpoint_test",
            "phase": "before_action",
            "route_index": 1,
            "next_action_index": 1,
            "task_started_at": original_start.isoformat(timespec="milliseconds"),
            "task_pause_baseline": 25,
            "scheduled_wait_seconds": 7,
        }
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                state_file=str(Path(directory, "state.json")),
                log_jsonl=str(Path(directory, "events.jsonl")),
                capture="none",
                capture_dir=str(Path(directory, "captures")),
                log_actions=False,
                dry_run=False,
                completion_padding_seconds=0,
                retry_minutes=10,
            )
            tracking_click.run_task(
                task,
                FakeRouteAutomation(datetime.now(), runtime),
                smart_runner=None,
                args=args,
                state=state,
                resume_checkpoint=checkpoint,
            )

        self.assertEqual(state["checkpoint_test"]["last_started"], original_start.replace(microsecond=original_start.microsecond // 1000 * 1000))
        self.assertEqual(state["checkpoint_test"]["human_pause_seconds"], 104)


if __name__ == "__main__":
    unittest.main()
