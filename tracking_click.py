import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from coordinates import *
from smart_automation import SmartProcedureRunner, load_procedures


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    interval_minutes: float
    initial_delay_minutes: float
    routes: tuple[str, ...]
    enabled: bool = True
    lead_seconds: float = 0.0


def route_task(name, interval_minutes, initial_delay_minutes, *routes, lead_seconds=0.0):
    return ScheduledTask(
        name=name,
        interval_minutes=interval_minutes,
        initial_delay_minutes=initial_delay_minutes,
        routes=routes or (name,),
        lead_seconds=max(0.0, float(lead_seconds)),
    )


TASKS = {
    name: route_task(name, 60, 1, name, "save")
    for name in (
        "bear1",
        "bear_tianshan",
        "bear2",
        "bear3",
        "bear4",
        "bear5",
        "bear6",
        "bear8",
        "bear9",
        "bear10",
        "bear11",
        "bear12",
        "bear13",
        "bear15",
        "pig2",
        "pig1",
    )
}
TASKS.update(
    {
        "xigua": route_task("xigua", 120, 11, "sleep1", "xigua", "save"),
        "jiazhai": route_task("jiazhai", 120, 11, "sleep1", "jiazhai", "save"),
        "bear7": route_task("bear7", 180, 15, "sleep1", "bear7", "save"),
        "cow2": route_task("cow2", 180, 15, "sleep1", "cow2", "save"),
        "cow1": route_task("cow1", 180, 15, "sleep1", "cow1", "save"),
        "bear14": route_task("bear14", 180, 15, "sleep1", "bear14", "save"),
        "xiangjiao": route_task("xiangjiao", 300, 24, "xiangjiao", "save"),
        "shanzha": route_task("shanzha", 300, 24, "shanzha", "save"),
        "pingguo": route_task("pingguo", 300, 24, "pingguo", "save"),
        "changbaipingguo": route_task("changbaipingguo", 300, 24, "changbaipingguo", "save"),
        "lianou": route_task("lianou", 300, 24, "lianou", "save"),
        "jianshui": route_task("jianshui", 360, 21, "jianshui", "save"),
        "hexia1": route_task("hexia1", 360, 21, "hexia1", "save"),
        "hexia2": route_task("hexia2", 360, 21, "hexia2", "save"),
        "suancai": route_task("suancai", 1440, 0, "sleep2", "suancai", "save"),
    }
)

GROUPS = {
    "1_hour": (
        "bear1",
        "bear_tianshan",
        "bear2",
        "bear3",
        "bear4",
        "bear5",
        "bear6",
        "bear8",
        "bear9",
        "bear10",
        "bear11",
        "bear12",
        "bear13",
        "bear15",
        "pig2",
        "pig1",
    ),
    "2_hour": ("xigua", "jiazhai"),
    "3_hour": ("bear7", "cow2", "cow1", "bear14"),
    "5_hour": ("xiangjiao", "shanzha", "pingguo", "changbaipingguo", "lianou"),
    "6_hour": ("jianshui", "hexia1", "hexia2"),
    "daily": ("suancai",),
}

BASE_TASKS = dict(TASKS)
BASE_GROUPS = {name: tuple(values) for name, values in GROUPS.items()}
PROCEDURES = {}


ROUTES = {
    name: value
    for name, value in globals().items()
    if isinstance(value, list) and all(isinstance(item, tuple) and len(item) == 2 for item in value)
}


def parse_args():
    parser = argparse.ArgumentParser(description="Run 7x24 scheduled game automation.")
    parser.add_argument("--state-file", default="scheduler_state.json", help="Persistent scheduler state file.")
    parser.add_argument("--log-jsonl", default="scheduler_events.jsonl", help="Structured event log.")
    parser.add_argument("--log-actions", action="store_true", help="Include every click/drag status in the JSONL log.")
    parser.add_argument("--capture-dir", default="scheduler_captures", help="Directory for route/task screenshots.")
    parser.add_argument("--capture", choices=("none", "task", "route"), default="task", help="Screenshot capture level.")
    parser.add_argument("--procedure-dir", default="procedures", help="Directory containing installed smart procedures.")
    parser.add_argument("--timing-file", default="runtime_profiles.json", help="Learned per-action timing profile.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without clicking.")
    parser.add_argument("--list", action="store_true", help="List scheduler task and group names, then exit.")
    parser.add_argument("--once", action="store_true", help="Run due tasks once and exit.")
    parser.add_argument(
        "--wait-once",
        action="store_true",
        help="Wait for the next start window, run one due batch, then exit.",
    )
    parser.add_argument("--run-now", nargs="*", help="Run these task names immediately, then exit unless --loop is set.")
    parser.add_argument("--loop", action="store_true", help="Continue scheduling after --run-now.")
    parser.add_argument("--only-task", action="append", help="Only run this task or group. Can repeat.")
    parser.add_argument("--skip-task", action="append", help="Skip this task or group. Can repeat.")
    parser.add_argument("--poll-seconds", type=float, default=0.25, help="Scheduler polling interval.")
    parser.add_argument(
        "--precision-reserve-seconds",
        type=float,
        default=120.0,
        help="Do not start an overdue legacy task this close to a future precise-task start window.",
    )
    parser.add_argument("--retry-minutes", type=float, default=10.0, help="Delay before retrying a failed task.")
    parser.add_argument("--startup-delay", type=float, default=5.0, help="Seconds to wait before scheduler starts.")
    parser.add_argument(
        "--completion-padding-seconds",
        type=float,
        default=0.0,
        help="Extra time added to the refresh anchor before calculating the next due time.",
    )
    return parser.parse_args()


def register_smart_procedures(directory):
    global GROUPS, PROCEDURES, TASKS
    TASKS = dict(BASE_TASKS)
    GROUPS = {name: tuple(values) for name, values in BASE_GROUPS.items()}
    PROCEDURES = load_procedures(directory, builtins=SMART_ROUTES)
    smart_task_names = []
    for name, procedure in PROCEDURES.items():
        schedule = procedure.get("schedule") or {}
        if not procedure.get("enabled") or not procedure.get("actions") or not schedule:
            continue
        before_routes = tuple(schedule.get("before_routes", ()))
        after_routes = tuple(schedule.get("after_routes", ()))
        TASKS[name] = route_task(
            name,
            float(schedule["interval_minutes"]),
            float(schedule.get("initial_delay_minutes", 0.0)),
            *before_routes,
            name,
            *after_routes,
            lead_seconds=float(schedule.get("lead_seconds", 0.0)),
        )
        smart_task_names.append(name)
    if smart_task_names:
        GROUPS["smart"] = tuple(sorted(smart_task_names))


def active_tasks(args):
    names = set(expand_names(args.only_task) if args.only_task else TASKS)
    skipped = set(expand_names(args.skip_task))
    return {name: task for name, task in TASKS.items() if name in names and name not in skipped and task.enabled}


def expand_names(names):
    expanded = []
    for name in names or []:
        if name in GROUPS:
            expanded.extend(GROUPS[name])
        elif name in TASKS:
            expanded.append(name)
        else:
            raise SystemExit(f"Unknown scheduler task/group: {name}")
    return expanded


def load_state(path, tasks):
    now = datetime.now()
    state_path = Path(path)
    if state_path.exists():
        with open(state_path, "r", encoding="utf-8") as file:
            raw_state = json.load(file)
    else:
        raw_state = {}

    state = {}
    for name, task in tasks.items():
        task_state = raw_state.get(name, {})
        lead_seconds = max(0.0, float(task_state.get("lead_seconds", task.lead_seconds)))
        lead_samples = max(0, int(task_state.get("lead_samples", 0)))
        next_due = task_state.get("next_due")
        if next_due:
            try:
                due_time = datetime.fromisoformat(next_due)
            except ValueError:
                due_time = now + timedelta(
                    minutes=task.initial_delay_minutes,
                    seconds=lead_seconds,
                )
        else:
            due_time = now + timedelta(
                minutes=task.initial_delay_minutes,
                seconds=lead_seconds,
            )
        state[name] = {
            "next_due": due_time,
            "last_started": parse_optional_datetime(task_state.get("last_started")),
            "last_completed": parse_optional_datetime(task_state.get("last_completed")),
            "last_refresh_anchor": parse_optional_datetime(task_state.get("last_refresh_anchor")),
            "last_status": task_state.get("last_status", "new"),
            "failures": task_state.get("failures", 0),
            "lead_seconds": lead_seconds,
            "lead_samples": lead_samples,
        }
    return state


def save_state(path, state):
    serializable = {}
    for name, task_state in state.items():
        serializable[name] = {
            key: value.isoformat(timespec="milliseconds") if isinstance(value, datetime) else value
            for key, value in task_state.items()
        }
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(serializable, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(state_path)


def sync_external_procedure_result(path, procedure, result):
    """Persist a successful exact refresh anchor produced outside the scheduler loop."""
    schedule = procedure.get("schedule") or {}
    if not result.refresh_anchor or not schedule:
        return None
    interval_minutes = float(schedule.get("interval_minutes", 0.0))
    if interval_minutes <= 0:
        return None

    state_path = Path(path)
    try:
        raw_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        raw_state = {}
    task_state = raw_state.setdefault(str(procedure["name"]), {})
    next_due = next_due_from_anchor(result.refresh_anchor, interval_minutes)
    task_state.update(
        {
            "next_due": next_due.isoformat(timespec="milliseconds"),
            "last_started": result.started_at.isoformat(timespec="milliseconds"),
            "last_completed": result.completed_at.isoformat(timespec="milliseconds"),
            "last_refresh_anchor": result.refresh_anchor.isoformat(timespec="milliseconds"),
            "last_status": "external_ok",
            "failures": 0,
            "lead_seconds": max(
                0.0,
                float(task_state.get("lead_seconds", schedule.get("lead_seconds", 0.0))),
            ),
            "lead_samples": max(0, int(task_state.get("lead_samples", 0))),
        }
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(json.dumps(raw_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)
    return next_due


def parse_optional_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def log_event(path, event):
    event = {"time": datetime.now().isoformat(timespec="seconds"), **event}
    with open(path, "a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


def due_task_names(state, run_now, active_names=None, precision_reserve_seconds=0.0):
    if run_now is not None:
        return expand_names(run_now)
    now = datetime.now()
    names = active_names if active_names is not None else state
    due = [name for name in names if task_start_time(state[name]) <= now]
    precise_due = [name for name in due if float(state[name].get("lead_seconds", 0.0)) > 0]
    if due and not precise_due and precision_reserve_seconds > 0:
        future_precise_starts = [
            task_start_time(state[name])
            for name in names
            if float(state[name].get("lead_seconds", 0.0)) > 0 and task_start_time(state[name]) > now
        ]
        if future_precise_starts:
            seconds_to_precise = (min(future_precise_starts) - now).total_seconds()
            if seconds_to_precise <= precision_reserve_seconds:
                return []
    return sorted(
        due,
        key=lambda name: (
            state[name]["next_due"] <= now,
            state[name]["next_due"],
            name,
        ),
    )


def task_start_time(task_state):
    return task_state["next_due"] - timedelta(seconds=max(0.0, float(task_state.get("lead_seconds", 0.0))))


def seconds_until_next_start(state, active_names=None):
    names = list(active_names if active_names is not None else state)
    if not names:
        return None
    return max(0.0, min((task_start_time(state[name]) - datetime.now()).total_seconds() for name in names))


def next_due_from_anchor(anchor, interval_minutes, padding_seconds=0.0):
    return anchor + timedelta(minutes=interval_minutes, seconds=padding_seconds)


def run_task(task, automation, smart_runner, args, state, *, scheduled_for=None):
    started_at = datetime.now()
    original_next_due = state[task.name]["next_due"]
    target_label = f" target={scheduled_for.isoformat(timespec='milliseconds')}" if scheduled_for else ""
    print(f"{started_at:%Y-%m-%d %H:%M:%S} start {task.name}{target_label}: {', '.join(task.routes)}")
    state[task.name]["last_started"] = started_at
    state[task.name]["last_status"] = "running"
    save_state(args.state_file, state)
    log_event(
        args.log_jsonl,
        {
            "event": "task_started",
            "task": task.name,
            "routes": task.routes,
            "scheduled_for": scheduled_for.isoformat(timespec="milliseconds") if scheduled_for else None,
            "lead_seconds": state[task.name].get("lead_seconds", 0.0),
        },
    )

    refresh_anchor = None
    scheduled_wait_seconds = 0.0
    try:
        for route_name in task.routes:
            is_smart = route_name in PROCEDURES and PROCEDURES[route_name].get("enabled")
            if route_name not in ROUTES and not is_smart:
                raise KeyError(f"Route '{route_name}' is not defined in coordinates.py")

            print(f"route {route_name}")
            log_event(args.log_jsonl, {"event": "route_started", "task": task.name, "route": route_name})
            route_capture_dir = args.capture_dir if args.capture == "route" else None

            def log_action(status):
                if args.log_actions:
                    log_event(args.log_jsonl, {"event": "action", "task": task.name, **status})

            if is_smart:
                result = smart_runner.run(
                    PROCEDURES[route_name],
                    dry_run=args.dry_run,
                    action_logger=log_action,
                    not_before=scheduled_for,
                )
                scheduled_wait_seconds += result.scheduled_wait_seconds
                if result.refresh_anchor:
                    refresh_anchor = result.refresh_anchor
            else:
                automation.run_actions(
                    ROUTES[route_name],
                    route_name=route_name,
                    print_names=True,
                    dry_run=args.dry_run,
                    capture_dir=route_capture_dir,
                    action_logger=log_action,
                )
            log_event(args.log_jsonl, {"event": "route_completed", "task": task.name, "route": route_name})

        completed_at = datetime.now()
        if args.dry_run:
            state[task.name]["last_status"] = "dry_run"
            state[task.name]["next_due"] = original_next_due
            log_event(args.log_jsonl, {"event": "task_dry_run", "task": task.name})
            print(f"{completed_at:%Y-%m-%d %H:%M:%S} dry-run complete {task.name}; schedule unchanged")
            return

        anchor = refresh_anchor or completed_at
        state[task.name]["last_completed"] = completed_at
        state[task.name]["last_refresh_anchor"] = anchor
        state[task.name]["last_status"] = "ok"
        state[task.name]["failures"] = 0
        if refresh_anchor:
            update_lead_time(
                state[task.name],
                max(0.0, (refresh_anchor - started_at).total_seconds() - scheduled_wait_seconds),
            )
        state[task.name]["next_due"] = next_due_from_anchor(
            anchor,
            task.interval_minutes,
            args.completion_padding_seconds,
        )
        if args.capture == "task" and not args.dry_run:
            capture = automation.capture_screenshot(args.capture_dir, task.name)
            if capture:
                log_event(args.log_jsonl, {"event": "task_capture", "task": task.name, "path": str(capture)})
        print(
            f"{datetime.now():%Y-%m-%d %H:%M:%S} complete {task.name}; "
            f"next due {state[task.name]['next_due']:%Y-%m-%d %H:%M:%S}"
        )
        log_event(
            args.log_jsonl,
            {
                "event": "task_completed",
                "task": task.name,
                "refresh_anchor": anchor.isoformat(timespec="milliseconds"),
                "anchor_source": "marked_action" if refresh_anchor else "task_completion",
                "next_due": state[task.name]["next_due"].isoformat(timespec="milliseconds"),
            },
        )
    except Exception as exc:
        failed_at = datetime.now()
        state[task.name]["failures"] += 1
        failed_after_anchor = refresh_anchor or getattr(exc, "refresh_anchor", None)
        if failed_after_anchor:
            state[task.name]["last_status"] = "post_anchor_failed"
            state[task.name]["last_refresh_anchor"] = failed_after_anchor
            update_lead_time(
                state[task.name],
                max(
                    0.0,
                    (failed_after_anchor - started_at).total_seconds()
                    - float(getattr(exc, "scheduled_wait_seconds", 0.0)),
                ),
            )
            state[task.name]["next_due"] = next_due_from_anchor(
                failed_after_anchor,
                task.interval_minutes,
                args.completion_padding_seconds,
            )
        else:
            state[task.name]["last_status"] = "failed"
            # Retry later, but do not shift the game-refresh anchor as if the task succeeded.
            state[task.name]["next_due"] = failed_at + timedelta(minutes=args.retry_minutes)
        capture = None
        if not args.dry_run:
            capture = automation.capture_screenshot(args.capture_dir, f"{task.name}_failure")
        failure_label = "failed after refresh anchor" if failed_after_anchor else "failed"
        print(f"{failed_at:%Y-%m-%d %H:%M:%S} {failure_label} {task.name}: {exc}")
        log_event(
            args.log_jsonl,
            {
                "event": "task_failed_after_anchor" if failed_after_anchor else "task_failed",
                "task": task.name,
                "error": repr(exc),
                "action_index": getattr(exc, "action_index", None),
                "action_type": getattr(exc, "action_type", None),
                "action_label": getattr(exc, "action_label", None),
                "refresh_anchor": (
                    failed_after_anchor.isoformat(timespec="milliseconds") if failed_after_anchor else None
                ),
                "next_due": state[task.name]["next_due"].isoformat(timespec="milliseconds"),
                "capture": str(capture) if capture else None,
            },
        )
    finally:
        save_state(args.state_file, state)


def print_schedule(state):
    print("schedule")
    for name, task_state in sorted(state.items(), key=lambda item: item[1]["next_due"]):
        start_at = task_start_time(task_state)
        print(
            f"  {name}: {task_state['last_status']} start={start_at:%Y-%m-%d %H:%M:%S} "
            f"target={task_state['next_due']:%Y-%m-%d %H:%M:%S} "
            f"lead={float(task_state.get('lead_seconds', 0.0)):.1f}s"
        )


def update_lead_time(task_state, observed_seconds, margin_seconds=2.0):
    measured = max(0.0, float(observed_seconds)) + max(0.0, float(margin_seconds))
    samples = max(0, int(task_state.get("lead_samples", 0)))
    old = max(0.0, float(task_state.get("lead_seconds", measured)))
    task_state["lead_seconds"] = round(measured if samples == 0 else old * 0.7 + measured * 0.3, 3)
    task_state["lead_samples"] = samples + 1


def main():
    args = parse_args()
    register_smart_procedures(args.procedure_dir)
    if args.list:
        print("groups")
        for name, task_names in GROUPS.items():
            print(f"  {name}: {', '.join(task_names)}")
        print("tasks")
        for name, task in sorted(TASKS.items()):
            lead = f" lead={task.lead_seconds:g}s" if task.lead_seconds else ""
            print(f"  {name}: every {task.interval_minutes}m{lead} routes={', '.join(task.routes)}")
        drafts = [name for name, procedure in PROCEDURES.items() if not procedure.get("actions")]
        if drafts:
            print("smart procedure drafts")
            for name in sorted(drafts):
                procedure = PROCEDURES[name]
                print(
                    f"  {name}: {procedure.get('map')} "
                    f"{procedure.get('start_coordinate')} -> {procedure.get('target_coordinate')}"
                )
        return

    tasks = active_tasks(args)
    state = load_state(args.state_file, TASKS)
    save_state(args.state_file, state)

    from automation import GameAutomation

    automation = GameAutomation(position_names={value: key for key, value in pos.items()})
    smart_runner = SmartProcedureRunner(
        automation,
        named_points=pos,
        timing_file=args.timing_file,
    )
    print_schedule(state)
    print(f"Starting scheduler in {args.startup_delay:g}s")
    time.sleep(args.startup_delay)

    run_now = args.run_now
    completed_batch = False
    try:
        while True:
            forced = run_now is not None
            reserve_seconds = 0.0 if forced or args.once else max(0.0, args.precision_reserve_seconds)
            names = due_task_names(
                state,
                run_now,
                tasks,
                precision_reserve_seconds=reserve_seconds,
            )
            run_now = None
            batch_names = names if forced or args.once else names[:1]
            for name in batch_names:
                if name not in tasks:
                    print(f"Skipping unknown/inactive task {name}")
                    continue
                run_task(
                    tasks[name],
                    automation,
                    smart_runner,
                    args,
                    state,
                    scheduled_for=None if forced else state[name]["next_due"],
                )
            completed_batch = completed_batch or bool(batch_names)

            if args.once or (args.wait_once and completed_batch) or (args.run_now is not None and not args.loop):
                break
            until_next = seconds_until_next_start(state, tasks)
            if not names and until_next == 0:
                sleep_seconds = args.poll_seconds
            else:
                sleep_seconds = args.poll_seconds if until_next is None else min(args.poll_seconds, until_next)
            time.sleep(max(0.01, sleep_seconds))
    except KeyboardInterrupt:
        print("Scheduler stopped by user.")
    finally:
        save_state(args.state_file, state)
        print_schedule(state)


if __name__ == "__main__":
    main()
