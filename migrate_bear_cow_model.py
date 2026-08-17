import argparse
import json
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import coordinates
from resource_catalog import MAP_COW_TASKS, infer_legacy_anchor_specs, policy_for_task


MIGRATION_SOURCE = "bear_cow_model_v1"
ACTUAL_BEAR_ACTIONS = {
    "bear1": (15, "bear1:bear", "长白山熊"),
    "bear_tianshan": (13, "bear_tianshan:1", "天山熊"),
}


def parse_args():
    parser = argparse.ArgumentParser(description="Correct legacy bear/cow resource state and ledger records.")
    parser.add_argument("--state-file", default="scheduler_state.json")
    parser.add_argument("--resource-history", default="resource_history.jsonl")
    parser.add_argument("--event-file", action="append", required=True)
    parser.add_argument("--runtime-state", default="runtime_control.json")
    parser.add_argument("--backup-dir", default="runs/state_backups")
    return parser.parse_args()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_jsonl(paths) -> list[dict]:
    records = []
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def migrate(*, state_file, history_file, event_files, runtime_state=None, backup_root=None) -> dict:
    state_path = Path(state_file)
    history_path = Path(history_file)
    runtime_path = Path(runtime_state) if runtime_state else None
    state = read_json(state_path)
    history = read_jsonl([history_path])
    events = read_jsonl(event_files)

    backup_path = None
    if backup_root:
        backup_path = Path(backup_root) / f"bear_cow_model_{datetime.now():%Y%m%d_%H%M%S}"
        backup_path.mkdir(parents=True, exist_ok=False)
        for path in (state_path, history_path, runtime_path):
            if path and path.exists():
                shutil.copy2(path, backup_path / path.name)

    specs_by_task = {}
    for task_name in ("bear1", "bear_tianshan", *MAP_COW_TASKS):
        route = getattr(coordinates, task_name)
        specs_by_task[task_name] = infer_legacy_anchor_specs(
            task_name,
            task_name,
            route,
            coordinates.pos,
            default_interval_minutes=60,
        )

    action_events = {
        task_name: sorted(
            (
                event
                for event in events
                if event.get("event") == "legacy_action_dispatched"
                and event.get("task") == task_name
                and int(event.get("index", 0)) == action_index
                and event.get("dispatched_at")
            ),
            key=lambda event: event["dispatched_at"],
        )
        for task_name, (action_index, _point_id, _label) in ACTUAL_BEAR_ACTIONS.items()
    }

    for task_name, specs in specs_by_task.items():
        task_state = state.setdefault(task_name, {})
        policy = policy_for_task(task_name, 60)
        task_state.update(
            {
                "interval_minutes": 60.0,
                "resource_category": policy.category,
                "resource_name": policy.display_name,
                "anchor_mode": policy.anchor_mode,
            }
        )
        old_points = task_state.get("resource_points") or {}
        migrated_points = {}
        for spec in specs:
            old_point_id = "bear1:1" if spec.point_id == "bear1:cow" else spec.point_id
            point = dict(old_points.get(old_point_id) or {})
            point.update(
                {
                    "label": spec.label,
                    "category": spec.category,
                    "estimated_quantity": spec.estimated_quantity,
                    "unit": spec.unit,
                    "record_acquisition": spec.record_acquisition,
                }
            )
            if spec.point_id in {"bear1:bear", "bear_tianshan:1"}:
                latest = action_events[task_name][-1] if action_events[task_name] else None
                if latest:
                    anchored_at = datetime.fromisoformat(latest["dispatched_at"])
                    point["last_refresh_anchor"] = anchored_at.isoformat(timespec="milliseconds")
                    point["next_due"] = (anchored_at + timedelta(hours=1)).isoformat(timespec="milliseconds")
                    point["anchor_source"] = "legacy_named_action_migration"
                    point["samples"] = max(int(point.get("samples", 0)), len(action_events[task_name]))
            migrated_points[spec.point_id] = point
        task_state["resource_points"] = migrated_points
        due_values = [
            datetime.fromisoformat(point["next_due"])
            for point in migrated_points.values()
            if point.get("next_due")
        ]
        anchor_values = [
            datetime.fromisoformat(point["last_refresh_anchor"])
            for point in migrated_points.values()
            if point.get("last_refresh_anchor")
        ]
        if due_values:
            task_state["next_due"] = max(due_values).isoformat(timespec="milliseconds")
        if anchor_values:
            task_state["last_refresh_anchor"] = max(anchor_values).isoformat(timespec="milliseconds")

    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)

    appended = []
    if not any(record.get("source") == MIGRATION_SOURCE for record in history):
        old_wild_bears = [
            record
            for record in history
            if record.get("event") == "resource_acquired"
            and record.get("category") == "wild_bear"
            and record.get("task") in {"bear1", *MAP_COW_TASKS}
        ]
        for task_name in ("bear1", *MAP_COW_TASKS):
            old_records = [record for record in old_wild_bears if record.get("task") == task_name]
            quantity = sum(float(record.get("quantity", 0.0)) for record in old_records)
            if not quantity:
                continue
            appended.append(
                _adjustment(
                    task_name,
                    "wild_bear",
                    -quantity,
                    f"纠正旧模型中误记为野熊的 {task_name} 记录",
                )
            )
            cow_label = "长白山牛" if task_name == "bear1" else MAP_COW_TASKS[task_name]
            cow_point_id = "bear1:cow" if task_name == "bear1" else f"{task_name}:1"
            for record in old_records:
                appended.append(
                    _acquisition(
                        task_name=task_name,
                        point_id=cow_point_id,
                        point_label=cow_label,
                        category="map_cow",
                        occurred_at=record["time"],
                        next_due=record.get("next_due"),
                        migration_of=record.get("id"),
                    )
                )

        changbai_records = [record for record in old_wild_bears if record.get("task") == "bear1"]
        unused_bear_events = list(action_events["bear1"])
        for record in changbai_records:
            matched = _nearest_preceding_event(unused_bear_events, record.get("time"))
            if matched is None:
                continue
            unused_bear_events.remove(matched)
            anchored_at = datetime.fromisoformat(matched["dispatched_at"])
            appended.append(
                _acquisition(
                    task_name="bear1",
                    point_id="bear1:bear",
                    point_label="长白山熊",
                    category="wild_bear",
                    occurred_at=anchored_at.isoformat(timespec="milliseconds"),
                    next_due=(anchored_at + timedelta(hours=1)).isoformat(timespec="milliseconds"),
                    migration_of=record.get("id"),
                )
            )

        for event in action_events["bear_tianshan"]:
            anchored_at = datetime.fromisoformat(event["dispatched_at"])
            appended.append(
                _acquisition(
                    task_name="bear_tianshan",
                    point_id="bear_tianshan:1",
                    point_label="天山熊",
                    category="wild_bear",
                    occurred_at=anchored_at.isoformat(timespec="milliseconds"),
                    next_due=(anchored_at + timedelta(hours=1)).isoformat(timespec="milliseconds"),
                    migration_of=None,
                )
            )

        if appended:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            with history_path.open("a", encoding="utf-8") as file:
                for record in appended:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")

    return {
        "backup": str(backup_path) if backup_path else None,
        "state_tasks_migrated": len(specs_by_task),
        "history_records_appended": len(appended),
        "bear_points": ["bear1:bear", "bear_tianshan:1"],
        "map_cow_points": ["bear1:cow", *(f"{task}:1" for task in MAP_COW_TASKS)],
    }


def _nearest_preceding_event(events: list[dict], occurred_at) -> dict | None:
    try:
        target = datetime.fromisoformat(str(occurred_at))
    except ValueError:
        return None
    candidates = []
    for event in events:
        try:
            moment = datetime.fromisoformat(event["dispatched_at"])
        except (KeyError, ValueError):
            continue
        delta = (target - moment).total_seconds()
        if 0 <= delta <= 180:
            candidates.append((delta, event))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _adjustment(task_name: str, category: str, quantity: float, note: str) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "event": "resource_adjustment",
        "time": datetime.now().isoformat(timespec="milliseconds"),
        "task": task_name,
        "point_id": None,
        "point_label": "熊牛模型纠正",
        "category": category,
        "quantity": float(quantity),
        "unit": "只",
        "estimated": False,
        "source": MIGRATION_SOURCE,
        "note": note,
        "next_due": None,
    }


def _acquisition(
    *, task_name, point_id, point_label, category, occurred_at, next_due, migration_of
) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "event": "resource_acquired",
        "time": occurred_at,
        "task": task_name,
        "point_id": point_id,
        "point_label": point_label,
        "category": category,
        "quantity": 1.0,
        "unit": "只",
        "estimated": True,
        "source": MIGRATION_SOURCE,
        "migration_of": migration_of,
        "next_due": next_due,
    }


def main():
    args = parse_args()
    result = migrate(
        state_file=args.state_file,
        history_file=args.resource_history,
        event_files=args.event_file,
        runtime_state=args.runtime_state,
        backup_root=args.backup_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
