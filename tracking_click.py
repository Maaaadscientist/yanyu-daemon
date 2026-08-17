import argparse
import json
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from coordinates import *
from monitor_server import MonitorData, MonitoringServer
from resource_catalog import (
    ResourceLedger,
    infer_legacy_anchor_specs,
    next_due_for_points,
    policy_for_task,
    smart_anchor_specs,
)
from runtime_control import RuntimeControl
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
        "--stop-hotkey",
        default="<ctrl>+c",
        help="Global pynput hotkey that gracefully stops the scheduler (default: <ctrl>+c).",
    )
    parser.add_argument(
        "--no-stop-hotkey",
        action="store_true",
        help="Disable the global stop hotkey; terminal Ctrl-C still works in foreground mode.",
    )
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
    parser.add_argument(
        "--control-state-file",
        default="runtime_control.json",
        help="Persistent human-input pause and resume checkpoint file.",
    )
    parser.add_argument(
        "--resume-hotkey",
        default="<ctrl>+<alt>+r",
        help="Global hotkey that validates the checkpoint and resumes automation.",
    )
    parser.add_argument(
        "--resume-delay-seconds",
        type=float,
        default=3.0,
        help="Quiet countdown after a valid resume request.",
    )
    parser.add_argument(
        "--resume-coordinate-tolerance",
        type=int,
        default=0,
        help="Maximum map-coordinate difference accepted by normal resume.",
    )
    parser.add_argument(
        "--no-human-input-pause",
        action="store_true",
        help="Disable physical mouse/keyboard pause detection; web/manual pause remains available.",
    )
    parser.add_argument(
        "--resource-history",
        default="resource_history.jsonl",
        help="Append-only resource acquisition and adjustment ledger.",
    )
    parser.add_argument("--web-host", default="127.0.0.1", help="Monitoring service bind address.")
    parser.add_argument("--web-port", type=int, default=8765, help="Monitoring service port.")
    parser.add_argument("--no-web", action="store_true", help="Disable the monitoring web service.")
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
        configured_interval = float(schedule["interval_minutes"])
        interval_minutes = policy_for_task(name, configured_interval).interval_minutes
        TASKS[name] = route_task(
            name,
            interval_minutes,
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
        policy = policy_for_task(name, task.interval_minutes)
        lead_seconds = max(0.0, float(task_state.get("lead_seconds", task.lead_seconds)))
        lead_samples = max(0, int(task_state.get("lead_samples", 0)))
        last_refresh_anchor = parse_optional_datetime(task_state.get("last_refresh_anchor"))
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
        stored_interval = task_state.get("interval_minutes")
        inferred_interval = None
        if last_refresh_anchor and next_due:
            inferred_interval = max(0.0, (due_time - last_refresh_anchor).total_seconds() / 60.0)
        previous_interval = float(stored_interval) if stored_interval is not None else inferred_interval
        interval_changed = (
            previous_interval is not None
            and abs(previous_interval - task.interval_minutes) > (2.0 / 60.0)
            and task_state.get("last_status") in {"ok", "external_ok", "post_anchor_failed"}
        )
        if interval_changed and last_refresh_anchor:
            due_time = next_due_from_anchor(last_refresh_anchor, task.interval_minutes)

        resource_points = {}
        for point_id, point in (task_state.get("resource_points") or {}).items():
            if not isinstance(point, dict):
                continue
            point_state = dict(point)
            point_state["last_refresh_anchor"] = parse_optional_datetime(point.get("last_refresh_anchor"))
            point_state["next_due"] = parse_optional_datetime(point.get("next_due"))
            if interval_changed and point_state["last_refresh_anchor"]:
                point_state["next_due"] = next_due_from_anchor(
                    point_state["last_refresh_anchor"],
                    task.interval_minutes,
                )
            resource_points[str(point_id)] = point_state
        if resource_points:
            due_time = next_due_for_points(
                resource_points,
                fallback_anchor=last_refresh_anchor or now,
                interval_minutes=task.interval_minutes,
            )
        state[name] = {
            "next_due": due_time,
            "last_started": parse_optional_datetime(task_state.get("last_started")),
            "last_completed": parse_optional_datetime(task_state.get("last_completed")),
            "last_refresh_anchor": last_refresh_anchor,
            "last_status": task_state.get("last_status", "new"),
            "failures": task_state.get("failures", 0),
            "lead_seconds": lead_seconds,
            "lead_samples": lead_samples,
            "interval_minutes": float(task.interval_minutes),
            "resource_category": policy.category,
            "resource_name": policy.display_name,
            "anchor_mode": policy.anchor_mode,
            "resource_points": resource_points,
            "human_pause_seconds": max(0.0, float(task_state.get("human_pause_seconds", 0.0))),
        }
    return state


def save_state(path, state):
    def serialize(value):
        if isinstance(value, datetime):
            return value.isoformat(timespec="milliseconds")
        if isinstance(value, dict):
            return {key: serialize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [serialize(item) for item in value]
        return value

    serializable = serialize(state)
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
    configured_interval = float(schedule.get("interval_minutes", 0.0))
    policy = policy_for_task(str(procedure["name"]), configured_interval)
    interval_minutes = policy.interval_minutes
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
            "interval_minutes": interval_minutes,
            "resource_category": policy.category,
            "resource_name": policy.display_name,
            "anchor_mode": policy.anchor_mode,
        }
    )
    specs = smart_anchor_specs(
        str(procedure["name"]),
        procedure,
        default_interval_minutes=interval_minutes,
    )
    if len(specs) == 1:
        spec = specs[0]
        point_state = task_state.setdefault("resource_points", {}).setdefault(spec.point_id, {})
        point_state.update(
            {
                "label": spec.label,
                "category": spec.category,
                "last_refresh_anchor": result.refresh_anchor.isoformat(timespec="milliseconds"),
                "next_due": next_due.isoformat(timespec="milliseconds"),
                "estimated_quantity": spec.estimated_quantity,
                "unit": spec.unit,
                "anchor_source": "external_smart_run",
                "samples": max(0, int(point_state.get("samples", 0))) + 1,
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


class SchedulerStopHotkey:
    def __init__(self, hotkey, stop_event, log_path, *, listener_factory=None):
        self.hotkey = str(hotkey)
        self.stop_event = stop_event
        self.log_path = log_path
        self.listener_factory = listener_factory
        self.listener = None
        self.stop_reason = None
        self._request_lock = threading.Lock()

    def start(self):
        if self.listener_factory is None:
            from pynput.keyboard import GlobalHotKeys

            self.listener_factory = GlobalHotKeys
        self.listener = self.listener_factory({self.hotkey: self.request_stop})
        self.listener.start()

    def request_stop(self, *, reason="global_hotkey", event_name="scheduler_hotkey_stop_requested"):
        with self._request_lock:
            if self.stop_event.is_set():
                return
            source = {
                "global_hotkey": f"Global stop hotkey {self.hotkey}",
                "keyboard_interrupt": "Terminal Ctrl-C",
                "web_control": "Web stop control",
            }.get(reason, reason)
            print(f"{source} pressed; stopping safely.")
            try:
                log_event(
                    self.log_path,
                    {
                        "event": event_name,
                        "hotkey": self.hotkey,
                        "reason": reason,
                    },
                )
            except Exception as exc:
                print(f"Could not log stop request: {exc}")
            finally:
                self.stop_reason = reason
                self.stop_event.set()

    def stop(self):
        if self.listener is None:
            return
        self.listener.stop()
        if self.listener.is_alive():
            self.listener.join(timeout=1.0)


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
    def task_priority(name):
        task_state = state[name]
        precise = float(task_state.get("lead_seconds", 0.0)) > 0
        missed_target = task_state["next_due"] <= now
        return (
            not precise,
            missed_target if precise else False,
            task_state["next_due"],
            name,
        )

    return sorted(due, key=task_priority)


def task_start_time(task_state):
    return task_state["next_due"] - timedelta(seconds=max(0.0, float(task_state.get("lead_seconds", 0.0))))


def seconds_until_next_start(state, active_names=None):
    names = list(active_names if active_names is not None else state)
    if not names:
        return None
    return max(0.0, min((task_start_time(state[name]) - datetime.now()).total_seconds() for name in names))


def next_due_from_anchor(anchor, interval_minutes, padding_seconds=0.0):
    return anchor + timedelta(minutes=interval_minutes, seconds=padding_seconds)


def resource_specs_for_route(task, route_name):
    if route_name in PROCEDURES and PROCEDURES[route_name].get("enabled"):
        return smart_anchor_specs(
            task.name,
            PROCEDURES[route_name],
            default_interval_minutes=task.interval_minutes,
        )
    if route_name not in ROUTES:
        return ()
    return infer_legacy_anchor_specs(
        task.name,
        route_name,
        ROUTES[route_name],
        pos,
        default_interval_minutes=task.interval_minutes,
    )


def task_resource_specs(task):
    return tuple(spec for route_name in task.routes for spec in resource_specs_for_route(task, route_name))


def apply_resource_anchor(
    task,
    spec,
    anchored_at,
    *,
    source,
    args,
    state,
    ledger=None,
):
    task_state = state[task.name]
    next_due = next_due_from_anchor(
        anchored_at,
        task.interval_minutes,
        getattr(args, "completion_padding_seconds", 0.0),
    )
    point_state = task_state.setdefault("resource_points", {}).setdefault(spec.point_id, {})
    point_state.update(
        {
            "label": spec.label,
            "category": spec.category,
            "last_refresh_anchor": anchored_at,
            "next_due": next_due,
            "estimated_quantity": spec.estimated_quantity,
            "unit": spec.unit,
            "anchor_source": source,
            "samples": max(0, int(point_state.get("samples", 0))) + 1,
        }
    )
    previous_anchor = task_state.get("last_refresh_anchor")
    if previous_anchor is None or anchored_at >= previous_anchor:
        task_state["last_refresh_anchor"] = anchored_at
    save_state(args.state_file, state)
    acquisition = None
    if ledger is not None:
        acquisition = ledger.record_anchor(
            task_name=task.name,
            spec=spec,
            anchored_at=anchored_at,
            next_due=next_due,
            source=source,
        )
    log_event(
        args.log_jsonl,
        {
            "event": "resource_refresh_anchored",
            "task": task.name,
            "route": spec.route_name,
            "action_index": spec.action_index,
            "point_id": spec.point_id,
            "point_label": spec.label,
            "category": spec.category,
            "refresh_anchor": anchored_at.isoformat(timespec="milliseconds"),
            "next_due": next_due.isoformat(timespec="milliseconds"),
            "anchor_source": source,
            "acquisition_id": acquisition.get("id") if acquisition else None,
        },
    )
    return next_due


def run_task(
    task,
    automation,
    smart_runner,
    args,
    state,
    *,
    scheduled_for=None,
    resume_checkpoint=None,
    ledger=None,
):
    runtime_control = getattr(automation, "runtime_control", None)
    resume_checkpoint = resume_checkpoint if resume_checkpoint and resume_checkpoint.get("task") == task.name else None
    invoked_at = datetime.now()
    started_at = parse_optional_datetime((resume_checkpoint or {}).get("task_started_at")) or invoked_at
    default_pause_baseline = runtime_control.total_pause_seconds if runtime_control is not None else 0.0
    pause_at_start = max(
        0.0,
        float((resume_checkpoint or {}).get("task_pause_baseline", default_pause_baseline)),
    )
    original_next_due = state[task.name]["next_due"]
    resume_route_index = max(1, int((resume_checkpoint or {}).get("route_index", 1)))
    resume_action_index = max(1, int((resume_checkpoint or {}).get("next_action_index", 1)))
    scheduled_wait_seconds = max(0.0, float((resume_checkpoint or {}).get("scheduled_wait_seconds", 0.0)))
    target_label = f" target={scheduled_for.isoformat(timespec='milliseconds')}" if scheduled_for else ""
    resume_label = f" resume=route:{resume_route_index}/action:{resume_action_index}" if resume_checkpoint else ""
    print(
        f"{invoked_at:%Y-%m-%d %H:%M:%S} start {task.name}{target_label}{resume_label}: "
        f"{', '.join(task.routes)}"
    )
    state[task.name]["last_started"] = started_at
    state[task.name]["last_status"] = "resuming" if resume_checkpoint else "running"
    if runtime_control is not None:
        runtime_control.replace_context(
            task=task.name,
            route_index=resume_route_index,
            total_routes=len(task.routes),
            next_action_index=resume_action_index,
            task_started_at=started_at.isoformat(timespec="milliseconds"),
            task_pause_baseline=pause_at_start,
            scheduled_wait_seconds=scheduled_wait_seconds,
            phase="task_start",
        )
    save_state(args.state_file, state)
    log_event(
        args.log_jsonl,
        {
            "event": "task_started",
            "task": task.name,
            "routes": task.routes,
            "scheduled_for": scheduled_for.isoformat(timespec="milliseconds") if scheduled_for else None,
            "lead_seconds": state[task.name].get("lead_seconds", 0.0),
            "resume_checkpoint": resume_checkpoint,
        },
    )

    refresh_anchor = None
    refresh_anchor_pause_seconds = 0.0
    expected_specs = task_resource_specs(task)
    expected_ids = {spec.point_id for spec in expected_specs}
    anchors_this_run = {}
    skipped_point_ids = set()
    recorded_dispatches = set()
    try:
        for route_index, route_name in enumerate(task.routes, start=1):
            if resume_checkpoint and route_index < resume_route_index:
                continue
            is_smart = route_name in PROCEDURES and PROCEDURES[route_name].get("enabled")
            if route_name not in ROUTES and not is_smart:
                raise KeyError(f"Route '{route_name}' is not defined in coordinates.py")

            print(f"route {route_name}")
            log_event(args.log_jsonl, {"event": "route_started", "task": task.name, "route": route_name})
            route_capture_dir = args.capture_dir if args.capture == "route" else None
            route_specs = resource_specs_for_route(task, route_name)
            specs_by_index = {spec.action_index: spec for spec in route_specs}
            route_start_index = resume_action_index if resume_checkpoint and route_index == resume_route_index else 1
            skip_action_indexes = set()
            if not is_smart:
                now = datetime.now()
                for spec in route_specs:
                    point_state = state[task.name].get("resource_points", {}).get(spec.point_id, {})
                    point_due = point_state.get("next_due")
                    if isinstance(point_due, str):
                        point_due = parse_optional_datetime(point_due)
                    if (
                        spec.category in {"pen_livestock", "ranch_livestock", "wild_bear"}
                        and point_due
                        and point_due > now
                    ):
                        skipped_point_ids.add(spec.point_id)
                        skip_action_indexes.update(
                            index
                            for index in range(spec.action_index - 2, spec.action_index + 2)
                            if index >= 1
                        )
                        log_event(
                            args.log_jsonl,
                            {
                                "event": "resource_point_skipped_cooldown",
                                "task": task.name,
                                "route": route_name,
                                "point_id": spec.point_id,
                                "point_label": spec.label,
                                "next_due": point_due.isoformat(timespec="milliseconds"),
                            },
                        )

            def log_action(status):
                nonlocal refresh_anchor, refresh_anchor_pause_seconds
                if args.log_actions:
                    log_event(args.log_jsonl, {"event": "action", "task": task.name, **status})
                if status.get("event") not in {"smart_action_dispatched", "legacy_action_dispatched"}:
                    return
                spec = specs_by_index.get(int(status.get("index", 0)))
                timestamp = status.get("dispatched_at")
                if spec is None or not timestamp:
                    return
                try:
                    anchored_at = datetime.fromisoformat(str(timestamp))
                except ValueError:
                    return
                dispatch_key = (spec.point_id, anchored_at.isoformat(timespec="milliseconds"))
                if dispatch_key in recorded_dispatches:
                    return
                recorded_dispatches.add(dispatch_key)
                source = "smart_marked_action" if is_smart else "legacy_inferred_action"
                apply_resource_anchor(
                    task,
                    spec,
                    anchored_at,
                    source=source,
                    args=args,
                    state=state,
                    ledger=ledger,
                )
                anchors_this_run[spec.point_id] = anchored_at
                if refresh_anchor is None or anchored_at >= refresh_anchor:
                    refresh_anchor = anchored_at
                    refresh_anchor_pause_seconds = (
                        max(0.0, runtime_control.total_pause_seconds - pause_at_start)
                        if runtime_control is not None
                        else 0.0
                    )

            if is_smart:
                result = smart_runner.run(
                    PROCEDURES[route_name],
                    dry_run=args.dry_run,
                    action_logger=log_action,
                    not_before=scheduled_for,
                    start_index=route_start_index,
                    route_index=route_index,
                    total_routes=len(task.routes),
                )
                scheduled_wait_seconds += result.scheduled_wait_seconds
                if result.refresh_anchor and not any(spec.point_id in anchors_this_run for spec in route_specs):
                    if len(route_specs) == 1:
                        apply_resource_anchor(
                            task,
                            route_specs[0],
                            result.refresh_anchor,
                            source="smart_result_fallback",
                            args=args,
                            state=state,
                            ledger=ledger,
                        )
                        anchors_this_run[route_specs[0].point_id] = result.refresh_anchor
                    refresh_anchor = result.refresh_anchor
                    refresh_anchor_pause_seconds = result.refresh_anchor_pause_seconds
                elif result.refresh_anchor and (refresh_anchor is None or result.refresh_anchor >= refresh_anchor):
                    refresh_anchor = result.refresh_anchor
                    refresh_anchor_pause_seconds = result.refresh_anchor_pause_seconds
            else:
                automation.run_actions(
                    ROUTES[route_name],
                    route_name=route_name,
                    print_names=True,
                    dry_run=args.dry_run,
                    capture_dir=route_capture_dir,
                    action_logger=log_action,
                    start_index=route_start_index,
                    route_index=route_index,
                    total_routes=len(task.routes),
                    skip_action_indexes=skip_action_indexes,
                )
            log_event(args.log_jsonl, {"event": "route_completed", "task": task.name, "route": route_name})
            if runtime_control is not None:
                runtime_control.set_context(
                    task=task.name,
                    route_index=route_index + 1,
                    total_routes=len(task.routes),
                    next_action_index=1,
                    phase="between_routes",
                )

        completed_at = datetime.now()
        if args.dry_run:
            state[task.name]["last_status"] = "dry_run"
            state[task.name]["next_due"] = original_next_due
            log_event(args.log_jsonl, {"event": "task_dry_run", "task": task.name})
            print(f"{completed_at:%Y-%m-%d %H:%M:%S} dry-run complete {task.name}; schedule unchanged")
            return

        point_states = state[task.name].setdefault("resource_points", {})
        if expected_ids:
            missing_state = expected_ids - set(point_states)
            missing_run = expected_ids - set(anchors_this_run) - skipped_point_ids
            if missing_state or (missing_run and not resume_checkpoint):
                missing = sorted(missing_state or missing_run)
                raise RuntimeError(f"Resource anchors were not observed for: {', '.join(missing)}")
            relevant_points = {point_id: point_states[point_id] for point_id in expected_ids}
            anchor = max(point["last_refresh_anchor"] for point in relevant_points.values())
            next_due = next_due_for_points(
                relevant_points,
                fallback_anchor=anchor,
                interval_minutes=task.interval_minutes,
                padding_seconds=args.completion_padding_seconds,
            )
            anchor_source = "resource_actions"
        else:
            anchor = refresh_anchor or completed_at
            next_due = next_due_from_anchor(
                anchor,
                task.interval_minutes,
                args.completion_padding_seconds,
            )
            anchor_source = "marked_action" if refresh_anchor else "task_completion"
        state[task.name]["last_completed"] = completed_at
        state[task.name]["last_refresh_anchor"] = anchor
        state[task.name]["last_status"] = "ok"
        state[task.name]["failures"] = 0
        task_pause_seconds = (
            max(0.0, runtime_control.total_pause_seconds - pause_at_start)
            if runtime_control is not None
            else 0.0
        )
        state[task.name]["human_pause_seconds"] = round(
            float(state[task.name].get("human_pause_seconds", 0.0)) + task_pause_seconds,
            3,
        )
        if refresh_anchor:
            update_lead_time(
                state[task.name],
                max(
                    0.0,
                    (refresh_anchor - started_at).total_seconds()
                    - scheduled_wait_seconds
                    - refresh_anchor_pause_seconds,
                ),
            )
        state[task.name]["next_due"] = next_due
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
                "anchor_source": anchor_source,
                "next_due": state[task.name]["next_due"].isoformat(timespec="milliseconds"),
                "human_pause_seconds": round(task_pause_seconds, 3),
                "resource_points": sorted(expected_ids),
            },
        )
        if runtime_control is not None:
            runtime_control.set_context(task=task.name, phase="task_completed")
            runtime_control.clear_checkpoint()
    except Exception as exc:
        failed_at = datetime.now()
        state[task.name]["failures"] += 1
        failed_after_anchor = refresh_anchor or getattr(exc, "refresh_anchor", None)
        if failed_after_anchor:
            covered_ids = set(anchors_this_run) | skipped_point_ids
            if resume_checkpoint:
                covered_ids.update(state[task.name].get("resource_points", {}))
            incomplete = expected_ids - covered_ids if expected_ids else set()
            state[task.name]["last_status"] = "partial_failed" if incomplete else "post_anchor_failed"
            state[task.name]["last_refresh_anchor"] = failed_after_anchor
            update_lead_time(
                state[task.name],
                max(
                    0.0,
                    (failed_after_anchor - started_at).total_seconds()
                    - float(getattr(exc, "scheduled_wait_seconds", 0.0))
                    - refresh_anchor_pause_seconds,
                ),
            )
            if incomplete:
                state[task.name]["next_due"] = failed_at + timedelta(minutes=args.retry_minutes)
            else:
                relevant_points = {
                    point_id: state[task.name].get("resource_points", {})[point_id]
                    for point_id in expected_ids
                    if point_id in state[task.name].get("resource_points", {})
                }
                state[task.name]["next_due"] = (
                    next_due_for_points(
                        relevant_points,
                        fallback_anchor=failed_after_anchor,
                        interval_minutes=task.interval_minutes,
                        padding_seconds=args.completion_padding_seconds,
                    )
                    if relevant_points
                    else next_due_from_anchor(
                        failed_after_anchor,
                        task.interval_minutes,
                        args.completion_padding_seconds,
                    )
                )
        else:
            state[task.name]["last_status"] = "failed"
            # Retry later, but do not shift the game-refresh anchor as if the task succeeded.
            state[task.name]["next_due"] = failed_at + timedelta(minutes=args.retry_minutes)
        task_pause_seconds = (
            max(0.0, runtime_control.total_pause_seconds - pause_at_start)
            if runtime_control is not None
            else float(getattr(exc, "human_pause_seconds", 0.0))
        )
        state[task.name]["human_pause_seconds"] = round(
            float(state[task.name].get("human_pause_seconds", 0.0)) + task_pause_seconds,
            3,
        )
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
                "route": getattr(exc, "route_name", None),
                "last_status": state[task.name]["last_status"],
                "refresh_anchor": (
                    failed_after_anchor.isoformat(timespec="milliseconds") if failed_after_anchor else None
                ),
                "next_due": state[task.name]["next_due"].isoformat(timespec="milliseconds"),
                "capture": str(capture) if capture else None,
                "human_pause_seconds": round(task_pause_seconds, 3),
                "anchored_points": sorted(anchors_this_run),
                "skipped_cooldown_points": sorted(skipped_point_ids),
            },
        )
    finally:
        save_state(args.state_file, state)
        if runtime_control is not None and not runtime_control.is_paused:
            runtime_control.replace_context(phase="idle")


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

    stop_event = threading.Event()
    runtime_control = RuntimeControl(
        stop_event=stop_event,
        state_file=args.control_state_file,
        resume_hotkey=args.resume_hotkey,
        resume_delay_seconds=args.resume_delay_seconds,
        coordinate_tolerance=args.resume_coordinate_tolerance,
        detect_human_input=not args.no_human_input_pause,
        event_logger=lambda event: log_event(args.log_jsonl, event),
    )
    automation = GameAutomation(
        position_names={value: key for key, value in pos.items()},
        stop_event=stop_event,
        runtime_control=runtime_control,
    )
    smart_runner = SmartProcedureRunner(
        automation,
        named_points=pos,
        timing_file=args.timing_file,
    )
    runtime_control.set_state_provider(smart_runner.state_reader.read_state)
    ledger = ResourceLedger(args.resource_history)
    hotkey = SchedulerStopHotkey(args.stop_hotkey, stop_event, args.log_jsonl)
    monitor = None
    if not args.no_web:
        monitor_data = MonitorData(
            state_file=args.state_file,
            event_file=args.log_jsonl,
            resource_history=args.resource_history,
            ledger=ledger,
            runtime_control=runtime_control,
            stop_callback=lambda: hotkey.request_stop(
                reason="web_control",
                event_name="scheduler_web_stop_requested",
            ),
        )
        try:
            monitor = MonitoringServer(host=args.web_host, port=args.web_port, data=monitor_data)
        except OSError as exc:
            print(f"Cannot bind monitoring service on {args.web_host}:{args.web_port}: {exc}")
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    def request_sigint_stop(_signum, _frame):
        hotkey.request_stop(
            reason="keyboard_interrupt",
            event_name="scheduler_interrupt_stop_requested",
        )

    signal.signal(signal.SIGINT, request_sigint_stop)
    run_now = args.run_now
    completed_batch = False
    try:
        runtime_hotkeys = {} if args.no_stop_hotkey else {args.stop_hotkey: hotkey.request_stop}
        runtime_control.start_listeners(extra_hotkeys=runtime_hotkeys)
        if not args.no_stop_hotkey:
            print(f"Global stop hotkey enabled: {args.stop_hotkey}")
        if not args.no_human_input_pause:
            print(f"Human input pauses automation; resume hotkey: {args.resume_hotkey}")
        if monitor is not None:
            monitor.start()
            host, port = monitor.address
            print(f"Resource monitor: http://{host}:{port}")
        print_schedule(state)
        print(f"Starting scheduler in {args.startup_delay:g}s")
        automation.wait_seconds(args.startup_delay)

        while True:
            automation.wait_seconds(0.0)
            resume_checkpoint = runtime_control.peek_resume_checkpoint()
            resumable_phases = {"task_start", "route_start", "before_action", "after_action", "between_routes"}
            resume_task = (
                resume_checkpoint.get("task")
                if resume_checkpoint and resume_checkpoint.get("phase") in resumable_phases
                else None
            )
            if resume_task in tasks:
                resume_checkpoint = runtime_control.consume_resume_checkpoint()
            elif resume_task:
                runtime_control.consume_resume_checkpoint()
                print(f"Discarding checkpoint for inactive task {resume_task}.")
                log_event(
                    args.log_jsonl,
                    {
                        "event": "automation_resume_checkpoint_discarded",
                        "task": resume_task,
                        "reason": "task_inactive",
                    },
                )
                resume_checkpoint = None
                resume_task = None
            elif resume_checkpoint and not resume_task:
                runtime_control.consume_resume_checkpoint()
                resume_checkpoint = None
            forced = run_now is not None or resume_task is not None
            reserve_seconds = 0.0 if forced or args.once else max(0.0, args.precision_reserve_seconds)
            if resume_task:
                names = [resume_task]
            else:
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
                    scheduled_for=(
                        state[name]["next_due"]
                        if name == resume_task
                        else (None if forced else state[name]["next_due"])
                    ),
                    resume_checkpoint=resume_checkpoint if name == resume_task else None,
                    ledger=ledger,
                )
            completed_batch = completed_batch or bool(batch_names)

            if args.once or (args.wait_once and completed_batch) or (args.run_now is not None and not args.loop):
                break
            until_next = seconds_until_next_start(state, tasks)
            if not names and until_next == 0:
                sleep_seconds = args.poll_seconds
            else:
                sleep_seconds = args.poll_seconds if until_next is None else min(args.poll_seconds, until_next)
            automation.wait_seconds(max(0.01, sleep_seconds))
    except KeyboardInterrupt:
        print("Scheduler stopped by user.")
        log_event(
            args.log_jsonl,
            {
                "event": "scheduler_stopped",
                "reason": hotkey.stop_reason or "keyboard_interrupt",
            },
        )
    finally:
        if not args.no_stop_hotkey:
            hotkey.stop()
        runtime_control.stop_listeners()
        if monitor is not None:
            monitor.stop()
        signal.signal(signal.SIGINT, previous_sigint_handler)
        save_state(args.state_file, state)
        print_schedule(state)


if __name__ == "__main__":
    main()
