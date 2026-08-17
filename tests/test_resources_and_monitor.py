import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import coordinates
import tracking_click
from coordinates import bear1, bear2, bear7, bear14, bear_tianshan, cow2, pig1, pos
from monitor_server import (
    WRITE_REQUEST_HEADER,
    WRITE_REQUEST_VALUE,
    MonitorData,
    MonitoringServer,
    ThreadingTLSHTTPServer,
    read_auth_token_file,
)
from resource_catalog import MAP_COW_TASKS, ResourceLedger, infer_legacy_anchor_specs


class FakeRouteAutomation:
    def __init__(self, base_time, runtime_control=None):
        self.base_time = base_time
        self.runtime_control = runtime_control
        self.calls = []

    def run_actions(self, actions, *, action_logger=None, **_kwargs):
        self.calls.append(dict(_kwargs))
        skipped = set(_kwargs.get("skip_action_indexes", ()))
        for index, _action in enumerate(actions, start=1):
            if index in skipped:
                action_logger(
                    {"event": "legacy_action_skipped", "index": index, "reason": "resource_cooldown"}
                )
                continue
            if action_logger:
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


class FakeWebRuntimeControl:
    def __init__(self):
        self.stop_event = threading.Event()
        self.resume_requests = []

    def status_snapshot(self):
        return {
            "paused": True,
            "reason": "test",
            "source": "test",
            "paused_at": None,
            "active_pause_seconds": 0,
            "total_pause_seconds": 0,
            "checkpoint": {"task": "bear5", "next_action_index": 6},
            "context": {"phase": "before_action"},
        }

    def request_pause(self, **_kwargs):
        return False

    def request_resume(self, **kwargs):
        self.resume_requests.append(kwargs)
        return True


class ResourcesAndMonitorTests(unittest.TestCase):
    def test_continue_checkpoint_must_match_the_current_task_route(self):
        task = tracking_click.ScheduledTask("checkpoint", 60, 0, ("save",))

        tracking_click.validate_continue_checkpoint(
            {
                "task": "checkpoint",
                "route": "save",
                "route_index": 1,
                "next_action_index": 1,
                "route_revision": tracking_click.route_revision("save"),
            },
            {"checkpoint": task},
        )
        with self.assertRaisesRegex(ValueError, "route changed"):
            tracking_click.validate_continue_checkpoint(
                {
                    "task": "checkpoint",
                    "route": "old_save",
                    "route_index": 1,
                    "next_action_index": 1,
                    "route_revision": tracking_click.route_revision("save"),
                },
                {"checkpoint": task},
            )

    def test_monitor_uses_explicit_continue_and_restart_modes(self):
        runtime = FakeWebRuntimeControl()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = MonitorData(
                state_file=root / "state.json",
                event_file=root / "events.jsonl",
                resource_history=root / "resources.jsonl",
                runtime_control=runtime,
            )

            data.control("continue")
            data.control("restart")

        self.assertEqual(
            runtime.resume_requests,
            [
                {"source": "web", "force": False, "mode": "continue_step"},
                {"source": "web", "force": False, "mode": "restart_task"},
            ],
        )

    def test_carriage_metadata_extracts_destination_and_safe_travel_prefix(self):
        self.assertEqual(tracking_click.legacy_carriage_destination("sleep1"), "乌思雪原")
        self.assertEqual(tracking_click.legacy_carriage_destination("bear5"), "洛阳")
        self.assertEqual(tracking_click.legacy_carriage_destination("cow2"), "幽州")
        self.assertTrue(tracking_click.carriage_destination_matches("逻邪河谷", "乌思雪原"))
        prefix = tracking_click.legacy_carriage_prefix("bear5")
        self.assertLess(len(prefix), len(coordinates.bear5))
        self.assertEqual(prefix[-1][0], coordinates.pos["洛阳出发"])

    def test_point_level_offsets_start_a_staggered_route_for_its_first_window(self):
        now = datetime.now()
        task_state = {
            "next_due": now + timedelta(minutes=8),
            "lead_seconds": 0,
            "resource_points": {
                "first": {
                    "next_due": now + timedelta(minutes=5),
                    "anchor_offset_seconds": 80,
                },
                "second": {
                    "next_due": now + timedelta(minutes=8),
                    "anchor_offset_seconds": 140,
                },
            },
        }

        self.assertEqual(
            tracking_click.task_start_time(task_state),
            now + timedelta(minutes=5, seconds=-80),
        )
        self.assertFalse(tracking_click.all_resources_ready({"route": task_state}, ["route"], now=now))
        for point in task_state["resource_points"].values():
            point["next_due"] = now - timedelta(seconds=1)
        self.assertTrue(tracking_click.all_resources_ready({"route": task_state}, ["route"], now=now))

    def test_point_timing_uses_an_ewma_without_discarding_samples(self):
        point = {}
        tracking_click.update_point_anchor_timing(point, 100, 20)
        tracking_click.update_point_anchor_timing(point, 110, 30)

        self.assertEqual(point["anchor_offset_seconds"], 103.0)
        self.assertEqual(point["segment_seconds"], 23.0)
        self.assertEqual(point["anchor_offset_samples"], 2)

    def test_retry_gate_overrides_an_already_due_point_window(self):
        now = datetime.now()
        retry_at = now + timedelta(minutes=10)
        task_state = {
            "next_due": retry_at,
            "last_status": "partial_failed",
            "retry_not_before": retry_at,
            "lead_seconds": 0,
            "resource_points": {
                "failed_point": {
                    "next_due": now - timedelta(minutes=1),
                    "anchor_offset_seconds": 120,
                }
            },
        }

        self.assertEqual(tracking_click.task_start_time(task_state), retry_at)

    def test_load_state_migrates_the_old_failed_next_due_into_a_retry_gate(self):
        retry_at = datetime.now() + timedelta(minutes=10)
        retry_at = retry_at.replace(microsecond=retry_at.microsecond // 1000 * 1000)
        old_point_due = datetime.now() - timedelta(minutes=5)
        raw = {
            "bear7": {
                "next_due": retry_at.isoformat(timespec="milliseconds"),
                "last_status": "partial_failed",
                "resource_points": {
                    "bear7:1": {
                        "next_due": old_point_due.isoformat(timespec="milliseconds"),
                    }
                },
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps(raw), encoding="utf-8")
            state = tracking_click.load_state(state_file, {"bear7": tracking_click.TASKS["bear7"]})

        self.assertEqual(state["bear7"]["retry_not_before"], retry_at)
        self.assertEqual(tracking_click.task_start_time(state["bear7"]), retry_at)

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
                    headers={
                        "Content-Type": "application/json",
                        WRITE_REQUEST_HEADER: WRITE_REQUEST_VALUE,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    adjustment = json.loads(response.read())
            finally:
                server.stop()

        self.assertEqual(status["tasks"][0]["status"], "ready")
        self.assertTrue(adjustment["ok"])
        self.assertEqual(adjustment["record"]["quantity"], 2)

    def test_authenticated_monitor_rejects_missing_credentials_and_cross_site_posts(self):
        token = "a" * 32
        runtime = FakeWebRuntimeControl()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = MonitorData(
                state_file=root / "state.json",
                event_file=root / "events.jsonl",
                resource_history=root / "resources.jsonl",
                runtime_control=runtime,
            )
            server = MonitoringServer(
                host="127.0.0.1",
                port=0,
                data=data,
                auth_username="yanyu",
                auth_token=token,
            )
            server.start()
            host, port = server.address
            base_url = f"http://{host}:{port}"
            authorization = "Basic " + base64.b64encode(f"yanyu:{token}".encode()).decode()
            try:
                with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
                    urllib.request.urlopen(f"{base_url}/api/status")
                self.assertEqual(unauthenticated.exception.code, 401)
                self.assertIn("Basic", unauthenticated.exception.headers["WWW-Authenticate"])

                status_request = urllib.request.Request(
                    f"{base_url}/api/status",
                    headers={"Authorization": authorization},
                )
                with urllib.request.urlopen(status_request) as response:
                    status = json.loads(response.read())

                untrusted_post = urllib.request.Request(
                    f"{base_url}/api/control/force-restart",
                    data=b"{}",
                    headers={"Authorization": authorization, "Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as forbidden:
                    urllib.request.urlopen(untrusted_post)
                self.assertEqual(forbidden.exception.code, 403)

                trusted_post = urllib.request.Request(
                    f"{base_url}/api/control/force-restart",
                    data=b"{}",
                    headers={
                        "Authorization": authorization,
                        "Content-Type": "application/json",
                        WRITE_REQUEST_HEADER: WRITE_REQUEST_VALUE,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(trusted_post) as response:
                    resumed = json.loads(response.read())
            finally:
                server.stop()

        self.assertTrue(status["scheduler_attached"])
        self.assertTrue(resumed["ok"])
        self.assertEqual(
            runtime.resume_requests,
            [{"source": "web", "force": True, "mode": "restart_task"}],
        )

    def test_lan_scheduler_control_requires_authentication_and_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = MonitorData(
                state_file=root / "state.json",
                event_file=root / "events.jsonl",
                resource_history=root / "resources.jsonl",
                runtime_control=FakeWebRuntimeControl(),
            )
            with self.assertRaisesRegex(ValueError, "authentication token"):
                MonitoringServer(host="0.0.0.0", port=0, data=data)
            with self.assertRaisesRegex(ValueError, "TLS is required"):
                MonitoringServer(host="0.0.0.0", port=0, data=data, auth_token="b" * 32)

    def test_unauthenticated_lan_monitor_rejects_all_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = MonitorData(
                state_file=root / "state.json",
                event_file=root / "events.jsonl",
                resource_history=root / "resources.jsonl",
            )
            server = MonitoringServer(host="0.0.0.0", port=0, data=data)
            server.start()
            _host, port = server.address
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/acquisitions/adjust",
                data=json.dumps({"task": "pig1", "quantity": 1}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with self.assertRaises(urllib.error.HTTPError) as forbidden:
                    urllib.request.urlopen(request)
            finally:
                server.stop()

        self.assertEqual(forbidden.exception.code, 403)

    def test_auth_token_file_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory, "web-token")
            token_path.write_text("c" * 32, encoding="utf-8")
            token_path.chmod(0o600)
            self.assertEqual(read_auth_token_file(token_path), "c" * 32)
            token_path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "mode 600"):
                read_auth_token_file(token_path)

    def test_tls_server_defers_handshake_until_the_client_worker(self):
        class FakeConnection:
            def __init__(self):
                self.timeout = None

            def settimeout(self, value):
                self.timeout = value

            def close(self):
                return None

        class FakeSecureConnection:
            pass

        class FakeContext:
            def __init__(self):
                self.arguments = None
                self.secure_connection = FakeSecureConnection()

            def wrap_socket(self, connection, **kwargs):
                self.arguments = (connection, kwargs)
                return self.secure_connection

        raw_connection = FakeConnection()
        context = FakeContext()
        server = object.__new__(ThreadingTLSHTTPServer)
        server.ssl_context = context
        server.handshake_timeout = 5.0
        with mock.patch.object(
            ThreadingHTTPServer,
            "get_request",
            return_value=(raw_connection, ("192.168.1.7", 50000)),
        ):
            secure_connection, address = server.get_request()

        self.assertIs(secure_connection, context.secure_connection)
        self.assertEqual(address, ("192.168.1.7", 50000))
        self.assertEqual(raw_connection.timeout, 5.0)
        self.assertIs(context.arguments[0], raw_connection)
        self.assertFalse(context.arguments[1]["do_handshake_on_connect"])

    def test_restart_ticket_discards_the_mid_action_checkpoint_and_restarts(self):
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
            automation = FakeRouteAutomation(datetime.now(), runtime)
            tracking_click.run_task(
                task,
                automation,
                smart_runner=None,
                args=args,
                state=state,
                recovery_ticket={
                    "task": "checkpoint_test",
                    "recovery_mode": "restart_task",
                    "abandoned_checkpoint": checkpoint,
                },
            )

        self.assertGreater(state["checkpoint_test"]["last_started"], original_start)
        self.assertEqual(state["checkpoint_test"]["human_pause_seconds"], 4)
        self.assertEqual(automation.calls[0].get("start_index"), 1)

    def test_continue_ticket_starts_at_the_recorded_action_and_keeps_pause_baseline(self):
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
            "phase": "continue_step",
            "recovery_mode": "continue_step",
            "route": "save",
            "route_index": 1,
            "next_action_index": 3,
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
            automation = FakeRouteAutomation(datetime.now(), runtime)
            tracking_click.run_task(
                task,
                automation,
                smart_runner=None,
                args=args,
                state=state,
                resume_checkpoint=checkpoint,
            )

        expected_start = original_start.replace(microsecond=original_start.microsecond // 1000 * 1000)
        self.assertEqual(state["checkpoint_test"]["last_started"], expected_start)
        self.assertEqual(state["checkpoint_test"]["human_pause_seconds"], 104)
        self.assertEqual(automation.calls[0].get("start_index"), 3)


if __name__ == "__main__":
    unittest.main()
