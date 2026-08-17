import json
import tempfile
import unittest
from pathlib import Path

from migrate_bear_cow_model import MIGRATION_SOURCE, migrate
from resource_catalog import ResourceLedger


class BearCowMigrationTests(unittest.TestCase):
    def test_migration_reclassifies_map_cows_and_backfills_two_bear_points(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            history_path = root / "history.jsonl"
            events_path = root / "events.jsonl"
            state_path.write_text(
                json.dumps(
                    {
                        "bear1": {
                            "next_due": "2026-08-17T11:00:20.000",
                            "last_refresh_anchor": "2026-08-17T10:00:20.000",
                            "last_status": "ok",
                            "resource_points": {
                                "bear1:1": {
                                    "last_refresh_anchor": "2026-08-17T10:00:20.000",
                                    "next_due": "2026-08-17T11:00:20.000",
                                }
                            },
                        },
                        "bear2": {
                            "next_due": "2026-08-17T11:01:20.000",
                            "last_refresh_anchor": "2026-08-17T10:01:20.000",
                            "last_status": "ok",
                            "resource_points": {
                                "bear2:1": {
                                    "last_refresh_anchor": "2026-08-17T10:01:20.000",
                                    "next_due": "2026-08-17T11:01:20.000",
                                }
                            },
                        },
                        "bear_tianshan": {
                            "next_due": "2026-08-17T11:02:30.000",
                            "last_refresh_anchor": "2026-08-17T10:02:30.000",
                            "last_status": "ok",
                            "resource_points": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            history = [
                {
                    "id": "old-bear1",
                    "event": "resource_acquired",
                    "time": "2026-08-17T10:00:20.000",
                    "task": "bear1",
                    "point_id": "bear1:1",
                    "point_label": "野熊 1",
                    "category": "wild_bear",
                    "quantity": 1,
                    "unit": "只",
                    "next_due": "2026-08-17T11:00:20.000",
                },
                {
                    "id": "old-bear2",
                    "event": "resource_acquired",
                    "time": "2026-08-17T10:01:20.000",
                    "task": "bear2",
                    "point_id": "bear2:1",
                    "point_label": "野熊 2",
                    "category": "wild_bear",
                    "quantity": 1,
                    "unit": "只",
                    "next_due": "2026-08-17T11:01:20.000",
                },
            ]
            history_path.write_text(
                "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in history),
                encoding="utf-8",
            )
            events = [
                {
                    "event": "legacy_action_dispatched",
                    "task": "bear1",
                    "index": 15,
                    "dispatched_at": "2026-08-17T10:00:00.000",
                },
                {
                    "event": "legacy_action_dispatched",
                    "task": "bear_tianshan",
                    "index": 13,
                    "dispatched_at": "2026-08-17T10:02:00.000",
                },
            ]
            events_path.write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
                encoding="utf-8",
            )

            first = migrate(
                state_file=state_path,
                history_file=history_path,
                event_files=[events_path],
                backup_root=root / "backups",
            )
            migrated_state = json.loads(state_path.read_text(encoding="utf-8"))
            records_after_first = ResourceLedger(history_path).recent(limit=1000)
            summary = ResourceLedger(history_path).summary()
            second = migrate(
                state_file=state_path,
                history_file=history_path,
                event_files=[events_path],
            )
            records_after_second = ResourceLedger(history_path).recent(limit=1000)

        self.assertEqual(set(migrated_state["bear1"]["resource_points"]), {"bear1:bear", "bear1:cow"})
        self.assertEqual(migrated_state["bear2"]["resource_category"], "map_cow")
        self.assertEqual(migrated_state["bear2"]["resource_points"]["bear2:1"]["label"], "姑苏牛")
        self.assertEqual(migrated_state["bear_tianshan"]["resource_points"]["bear_tianshan:1"]["label"], "天山熊")
        self.assertEqual(summary["by_category"]["wild_bear"], 2.0)
        self.assertEqual(summary["by_category"]["map_cow"], 2.0)
        self.assertTrue(any(record.get("source") == MIGRATION_SOURCE for record in records_after_first))
        self.assertGreater(first["history_records_appended"], 0)
        self.assertEqual(second["history_records_appended"], 0)
        self.assertEqual(records_after_first, records_after_second)


if __name__ == "__main__":
    unittest.main()
