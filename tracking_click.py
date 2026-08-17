import argparse
import hashlib
import json
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from coordinates import *
from game_session import LOGIN_STATES, GamePeriodReader, GameSessionManager, SessionWatchdog
from input_takeover import DesktopTakeoverHUD
from monitor_server import MonitorData, MonitoringServer, read_auth_token_file
from resource_catalog import (
    ResourceLedger,
    infer_legacy_anchor_specs,
    next_due_for_points,
    policy_for_task,
    smart_anchor_specs,
)
from runtime_control import (
    CONTINUE_STEP,
    RESTART_TASK,
    AutomationRecoveryHandoff,
    RuntimeControl,
)
from smart_automation import StateReadError, SmartProcedureRunner, load_procedures


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

NIGHT_GATED_TASKS = frozenset({"bear7", "cow1", "bear14"})
NEUTRAL_TRAVEL_ROUTES = ("bear5", "bear6")
CARRIAGE_DESTINATION_MAP_ALIASES = {
    "乌思雪原": frozenset({"乌思雪原", "逻邪河谷", "逻娑河谷"}),
}


ROUTES = {
    name: value
    for name, value in globals().items()
    if isinstance(value, list) and all(isinstance(item, tuple) and len(item) == 2 for item in value)
}

POSITION_NAMES = {}
for _name, _point in pos.items():
    POSITION_NAMES.setdefault(_point, []).append(_name)


def legacy_carriage_destination(route_name):
    for target, _delay in ROUTES.get(route_name, ()):
        if not (isinstance(target, tuple) and len(target) == 2 and isinstance(target[0], int)):
            continue
        departure = next(
            (name for name in POSITION_NAMES.get(target, ()) if name.endswith("出发")),
            None,
        )
        if not departure:
            continue
        destination = departure[: -len("出发")]
        for prefix in ("右上", "右下", "左上", "左下"):
            if destination.startswith(prefix):
                destination = destination[len(prefix) :]
                break
        return destination
    return None


def legacy_carriage_prefix(route_name):
    actions = ROUTES.get(route_name, ())
    for index, (target, _delay) in enumerate(actions, start=1):
        names = POSITION_NAMES.get(target, ()) if isinstance(target, tuple) else ()
        if any(name.endswith("出发") for name in names):
            return list(actions[:index])
    return []


def carriage_destination_matches(map_name, destination):
    return map_name in CARRIAGE_DESTINATION_MAP_ALIASES.get(destination, {destination})


def ensure_non_current_carriage_origin(
    route_name,
    *,
    task_name,
    automation,
    state_reader,
    args,
):
    """Move to a neutral map if the route's carriage icon is hidden by the player marker."""
    destination = legacy_carriage_destination(route_name)
    if not destination or args.dry_run:
        return None
    current = state_reader.read_state()
    if not current.map_name or not current.coordinate:
        raise RuntimeError(
            f"Cannot start carriage route {route_name}: the in-game map/coordinate is unreadable."
        )
    if not carriage_destination_matches(current.map_name, destination):
        return current

    choice = next(
        (
            (neutral_route, legacy_carriage_destination(neutral_route))
            for neutral_route in NEUTRAL_TRAVEL_ROUTES
            if legacy_carriage_destination(neutral_route) not in {None, destination, current.map_name}
        ),
        None,
    )
    if choice is None:
        raise RuntimeError(f"Cannot find a neutral carriage map while already in {destination}.")
    neutral_route, neutral_destination = choice
    prefix = legacy_carriage_prefix(neutral_route)
    if not prefix:
        raise RuntimeError(f"Neutral route {neutral_route} has no carriage departure prefix.")

    log_event(
        args.log_jsonl,
        {
            "event": "carriage_origin_reposition_started",
            "task": task_name,
            "route": route_name,
            "current_map": current.map_name,
            "route_destination": destination,
            "neutral_route": neutral_route,
            "neutral_destination": neutral_destination,
        },
    )
    automation.run_actions(
        prefix,
        route_name=f"reposition_{neutral_destination}",
        start_delay=0.5,
        end_delay=7.0,
        print_names=True,
        dry_run=False,
    )
    reached, _elapsed = state_reader.wait_for_state(
        {"map": neutral_destination},
        timeout=15.0,
        stable_samples=1,
    )
    log_event(
        args.log_jsonl,
        {
            "event": "carriage_origin_reposition_completed",
            "task": task_name,
            "route": route_name,
            "map": reached.map_name,
            "coordinate": list(reached.coordinate) if reached.coordinate else None,
        },
    )
    return reached


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
        "--start-paused",
        action="store_true",
        help="Start the web service and scheduler in a paused state without dispatching game actions.",
    )
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
        help="Global hotkey that validates state and continues at the current checkpoint step.",
    )
    parser.add_argument(
        "--restart-hotkey",
        default="<ctrl>+<alt>+<shift>+r",
        help="Global hotkey that abandons the middle checkpoint and restarts its task.",
    )
    parser.add_argument(
        "--pause-hotkey",
        default="<ctrl>+<alt>+p",
        help="Global hotkey that immediately interrupts automation and records a checkpoint.",
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
        help="Legacy compatibility option; restart recovery no longer resumes at a saved coordinate.",
    )
    parser.add_argument(
        "--no-human-input-pause",
        action="store_true",
        help="Disable physical mouse/keyboard pause detection; web/manual pause remains available.",
    )
    parser.add_argument(
        "--input-pause-policy",
        choices=("gesture", "immediate"),
        default="gesture",
        help="Require a sustained mouse gesture or pause immediately on ordinary physical input.",
    )
    parser.add_argument("--takeover-window-seconds", type=float, default=3.0)
    parser.add_argument("--takeover-required-seconds", type=float, default=2.0)
    parser.add_argument("--takeover-max-gap-seconds", type=float, default=0.30)
    parser.add_argument("--takeover-min-distance-pixels", type=float, default=1.0)
    parser.add_argument(
        "--no-takeover-hud",
        action="store_true",
        help="Disable the click-through desktop takeover progress HUD.",
    )
    parser.add_argument(
        "--resource-history",
        default="resource_history.jsonl",
        help="Append-only resource acquisition and adjustment ledger.",
    )
    parser.add_argument(
        "--resource-gate-max-wait-seconds",
        type=float,
        default=300.0,
        help="Wait this long for a resource point due soon; farther points are skipped for this pass.",
    )
    parser.add_argument(
        "--night-period-min-score",
        type=float,
        default=0.65,
        help="Minimum visual similarity required to confirm 子时 before protected livestock kills.",
    )
    parser.add_argument(
        "--no-intermediate-saves",
        dest="intermediate_saves",
        action="store_false",
        help="Disable in-game saves between resource points on multi-point routes.",
    )
    parser.set_defaults(intermediate_saves=True)
    parser.add_argument(
        "--session-check-seconds",
        type=float,
        default=15.0,
        help="Read-only interval for detecting login screens or remote-device logout.",
    )
    parser.add_argument(
        "--no-session-watchdog",
        action="store_true",
        help="Disable periodic login/remote-logout detection.",
    )
    parser.add_argument("--web-host", default="127.0.0.1", help="Monitoring service bind address.")
    parser.add_argument("--web-port", type=int, default=8765, help="Monitoring service port.")
    parser.add_argument("--web-auth-user", default="yanyu", help="HTTP Basic username for the dashboard.")
    parser.add_argument(
        "--web-auth-token-file",
        help="Mode-600 file containing the HTTP Basic password. Required for LAN control.",
    )
    parser.add_argument("--web-tls-cert-file", help="TLS certificate file for authenticated LAN control.")
    parser.add_argument("--web-tls-key-file", help="TLS private-key file for authenticated LAN control.")
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
        last_status = task_state.get("last_status", "new")
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
        retry_not_before = parse_optional_datetime(task_state.get("retry_not_before"))
        if retry_not_before is None and last_status in {"failed", "partial_failed"}:
            # Schema migration: these statuses historically stored the retry gate in next_due.
            retry_not_before = due_time
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
        expected_specs = {spec.point_id: spec for spec in task_resource_specs(task)}
        for point_id, point_state in resource_points.items():
            spec = expected_specs.get(point_id)
            if spec is None:
                continue
            point_state.update(
                {
                    "label": spec.label,
                    "category": spec.category,
                    "estimated_quantity": spec.estimated_quantity,
                    "unit": spec.unit,
                    "record_acquisition": spec.record_acquisition,
                }
            )
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
            "last_status": last_status,
            "failures": task_state.get("failures", 0),
            "lead_seconds": lead_seconds,
            "lead_samples": lead_samples,
            "interval_minutes": float(task.interval_minutes),
            "resource_category": policy.category,
            "resource_name": policy.display_name,
            "anchor_mode": policy.anchor_mode,
            "resource_points": resource_points,
            "human_pause_seconds": max(0.0, float(task_state.get("human_pause_seconds", 0.0))),
            "last_intermediate_save": parse_optional_datetime(task_state.get("last_intermediate_save")),
            "retry_not_before": retry_not_before,
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
    precise_due = [name for name in due if task_has_precision(state[name])]
    if due and not precise_due and precision_reserve_seconds > 0:
        future_precise_starts = [
            task_start_time(state[name])
            for name in names
            if task_has_precision(state[name]) and task_start_time(state[name]) > now
        ]
        if future_precise_starts:
            seconds_to_precise = (min(future_precise_starts) - now).total_seconds()
            if seconds_to_precise <= precision_reserve_seconds:
                return []
    def task_priority(name):
        task_state = state[name]
        precise = task_has_precision(task_state)
        missed_target = task_state["next_due"] <= now
        return (
            not precise,
            missed_target if precise else False,
            task_state["next_due"],
            name,
        )

    return sorted(due, key=task_priority)


def task_start_time(task_state):
    point_starts = []
    for point in (task_state.get("resource_points") or {}).values():
        point_due = point.get("next_due")
        if isinstance(point_due, str):
            point_due = parse_optional_datetime(point_due)
        if not point_due:
            continue
        offset = max(
            0.0,
            float(point.get("anchor_offset_seconds", task_state.get("lead_seconds", 0.0))),
        )
        point_starts.append(point_due - timedelta(seconds=offset))
    if point_starts:
        start_at = min(point_starts)
    else:
        start_at = task_state["next_due"] - timedelta(
            seconds=max(0.0, float(task_state.get("lead_seconds", 0.0)))
        )
    retry_not_before = task_state.get("retry_not_before")
    if isinstance(retry_not_before, str):
        retry_not_before = parse_optional_datetime(retry_not_before)
    return max(start_at, retry_not_before) if retry_not_before else start_at


def task_has_precision(task_state):
    if float(task_state.get("lead_seconds", 0.0)) > 0:
        return True
    return any(
        float(point.get("anchor_offset_seconds", 0.0)) > 0
        for point in (task_state.get("resource_points") or {}).values()
    )


def all_resources_ready(state, active_names, *, now=None):
    now = now or datetime.now()
    due_times = []
    for name in active_names:
        task_state = state[name]
        points = task_state.get("resource_points") or {}
        if points:
            for point in points.values():
                due = point.get("next_due")
                if isinstance(due, str):
                    due = parse_optional_datetime(due)
                if due:
                    due_times.append(due)
        elif task_state.get("next_due"):
            due_times.append(task_state["next_due"])
    return bool(due_times) and all(due <= now for due in due_times)


def seconds_until_next_start(state, active_names=None):
    names = list(active_names if active_names is not None else state)
    if not names:
        return None
    return max(0.0, min((task_start_time(state[name]) - datetime.now()).total_seconds() for name in names))


def next_due_from_anchor(anchor, interval_minutes, padding_seconds=0.0):
    return anchor + timedelta(minutes=interval_minutes, seconds=padding_seconds)


def sleep_until(moment):
    started = time.monotonic()
    while True:
        remaining = (moment - datetime.now()).total_seconds()
        if remaining <= 0:
            return max(0.0, time.monotonic() - started)
        time.sleep(min(0.1, remaining))


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
            "record_acquisition": spec.record_acquisition,
            "anchor_source": source,
            "samples": max(0, int(point_state.get("samples", 0))) + 1,
        }
    )
    previous_anchor = task_state.get("last_refresh_anchor")
    if previous_anchor is None or anchored_at >= previous_anchor:
        task_state["last_refresh_anchor"] = anchored_at
    save_state(args.state_file, state)
    acquisition = None
    if ledger is not None and spec.record_acquisition:
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
            "acquisition_recorded": bool(acquisition),
        },
    )
    return next_due


def update_point_anchor_timing(point_state, observed_offset_seconds, observed_segment_seconds=None):
    measured = max(0.0, float(observed_offset_seconds))
    samples = max(0, int(point_state.get("anchor_offset_samples", 0)))
    previous = max(0.0, float(point_state.get("anchor_offset_seconds", measured)))
    point_state["anchor_offset_seconds"] = round(
        measured if samples == 0 else previous * 0.7 + measured * 0.3,
        3,
    )
    point_state["anchor_offset_samples"] = samples + 1
    if observed_segment_seconds is not None:
        segment = max(0.0, float(observed_segment_seconds))
        segment_samples = max(0, int(point_state.get("segment_samples", 0)))
        old_segment = max(0.0, float(point_state.get("segment_seconds", segment)))
        point_state["segment_seconds"] = round(
            segment if segment_samples == 0 else old_segment * 0.7 + segment * 0.3,
            3,
        )
        point_state["segment_samples"] = segment_samples + 1


def run_intermediate_save(task, route_name, spec, *, automation, args, state):
    log_event(
        args.log_jsonl,
        {
            "event": "intermediate_save_started",
            "task": task.name,
            "route": route_name,
            "point_id": spec.point_id,
        },
    )
    automation.run_actions(
        ROUTES["save"],
        route_name=f"{route_name}_checkpoint_save",
        start_delay=0.2,
        end_delay=1.0,
        print_names=True,
        dry_run=False,
    )
    saved_at = datetime.now()
    state[task.name]["last_intermediate_save"] = saved_at
    save_state(args.state_file, state)
    log_event(
        args.log_jsonl,
        {
            "event": "intermediate_save_completed",
            "task": task.name,
            "route": route_name,
            "point_id": spec.point_id,
            "saved_at": saved_at.isoformat(timespec="milliseconds"),
        },
    )


def run_task(
    task,
    automation,
    smart_runner,
    args,
    state,
    *,
    scheduled_for=None,
    resume_checkpoint=None,
    recovery_ticket=None,
    ledger=None,
    period_reader=None,
    session_manager=None,
):
    runtime_control = getattr(automation, "runtime_control", None)
    resume_checkpoint = (
        resume_checkpoint
        if resume_checkpoint
        and resume_checkpoint.get("task") == task.name
        and resume_checkpoint.get("recovery_mode") in {None, CONTINUE_STEP}
        else None
    )
    recovery_ticket = recovery_ticket if recovery_ticket and recovery_ticket.get("task") == task.name else None
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
    if resume_route_index > len(task.routes):
        raise ValueError(
            f"Resume route index {resume_route_index} exceeds task route count {len(task.routes)}."
        )
    scheduled_wait_seconds = max(
        0.0,
        float((resume_checkpoint or {}).get("scheduled_wait_seconds", 0.0)),
    )
    target_label = f" target={scheduled_for.isoformat(timespec='milliseconds')}" if scheduled_for else ""
    if resume_checkpoint:
        resume_label = f" recovery=continue_step route:{resume_route_index}/action:{resume_action_index}"
    else:
        resume_label = " recovery=restart_task" if recovery_ticket else ""
    print(
        f"{invoked_at:%Y-%m-%d %H:%M:%S} start {task.name}{target_label}{resume_label}: "
        f"{', '.join(task.routes)}"
    )
    state[task.name]["last_started"] = started_at
    state[task.name]["last_status"] = (
        "resuming" if resume_checkpoint else ("recovering" if recovery_ticket else "running")
    )
    if not args.dry_run:
        state[task.name]["retry_not_before"] = None
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
            "recovery_ticket": recovery_ticket,
        },
    )

    refresh_anchor = None
    refresh_anchor_pause_seconds = 0.0
    expected_specs = task_resource_specs(task)
    expected_ids = {spec.point_id for spec in expected_specs}
    anchors_this_run = {}
    anchor_wait_totals = {}
    anchor_pause_totals = {}
    skipped_point_ids = set()
    recorded_dispatches = set()
    try:
        for route_index, route_name in enumerate(task.routes, start=1):
            if resume_checkpoint and route_index < resume_route_index:
                continue
            is_smart = route_name in PROCEDURES and PROCEDURES[route_name].get("enabled")
            if route_name not in ROUTES and not is_smart:
                raise KeyError(f"Route '{route_name}' is not defined in coordinates.py")

            continuing_inside_route = (
                resume_checkpoint
                and route_index == resume_route_index
                and resume_action_index > 1
            )
            if (
                not is_smart
                and not continuing_inside_route
                and getattr(smart_runner, "state_reader", None) is not None
            ):
                ensure_non_current_carriage_origin(
                    route_name,
                    task_name=task.name,
                    automation=automation,
                    state_reader=smart_runner.state_reader,
                    args=args,
                )

            print(f"route {route_name}")
            if runtime_control is not None:
                runtime_control.set_context(
                    route=route_name,
                    route_index=route_index,
                    route_revision=route_revision(route_name),
                )
            log_event(args.log_jsonl, {"event": "route_started", "task": task.name, "route": route_name})
            route_capture_dir = args.capture_dir if args.capture == "route" else None
            route_specs = resource_specs_for_route(task, route_name)
            specs_by_index = {spec.action_index: spec for spec in route_specs}
            gates_by_index = {max(1, spec.action_index - 2): spec for spec in route_specs}
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
                        spec.category in {
                            "pen_livestock",
                            "ranch_livestock",
                            "wild_bear",
                            "map_cow",
                        }
                        and point_due
                        and point_due > now
                    ):
                        seconds_until_due = (point_due - now).total_seconds()
                        if seconds_until_due <= max(
                            0.0,
                            float(getattr(args, "resource_gate_max_wait_seconds", 0.0)),
                        ):
                            continue
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
                                "seconds_until_due": round(seconds_until_due, 3),
                            },
                        )

            def before_legacy_action(status):
                nonlocal scheduled_wait_seconds
                spec = gates_by_index.get(int(status.get("index", 0)))
                if spec is None:
                    return
                point_state = state[task.name].get("resource_points", {}).get(spec.point_id, {})
                point_due = point_state.get("next_due")
                if isinstance(point_due, str):
                    point_due = parse_optional_datetime(point_due)
                if point_due and point_due > datetime.now():
                    waited = (
                        runtime_control.wait_until(point_due)
                        if runtime_control is not None
                        else sleep_until(point_due)
                    )
                    scheduled_wait_seconds += waited
                    if runtime_control is not None:
                        runtime_control.set_context(scheduled_wait_seconds=scheduled_wait_seconds)
                    log_event(
                        args.log_jsonl,
                        {
                            "event": "resource_point_gate_reached",
                            "task": task.name,
                            "route": route_name,
                            "point_id": spec.point_id,
                            "scheduled_for": point_due.isoformat(timespec="milliseconds"),
                            "waited_seconds": round(waited, 3),
                        },
                    )
                if task.name in NIGHT_GATED_TASKS:
                    if period_reader is None:
                        raise RuntimeError("Protected livestock route has no game-period reader.")
                    period = period_reader.wait_for_zi(timeout=8.0, stable_samples=2)
                    log_event(
                        args.log_jsonl,
                        {
                            "event": "night_period_verified",
                            "task": task.name,
                            "route": route_name,
                            "point_id": spec.point_id,
                            **period.as_dict(),
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
                pause_elapsed = (
                    max(0.0, runtime_control.total_pause_seconds - pause_at_start)
                    if runtime_control is not None
                    else 0.0
                )
                observed_offset = max(
                    0.0,
                    (anchored_at - started_at).total_seconds()
                    - scheduled_wait_seconds
                    - pause_elapsed,
                )
                prior_anchors = [
                    (point_id, value)
                    for point_id, value in anchors_this_run.items()
                    if point_id != spec.point_id and value <= anchored_at
                ]
                if prior_anchors:
                    prior_point_id, prior_anchor = max(prior_anchors, key=lambda item: item[1])
                    observed_segment = max(
                        0.0,
                        (anchored_at - prior_anchor).total_seconds()
                        - (scheduled_wait_seconds - anchor_wait_totals.get(prior_point_id, 0.0))
                        - (pause_elapsed - anchor_pause_totals.get(prior_point_id, 0.0)),
                    )
                else:
                    observed_segment = None
                anchor_wait_totals[spec.point_id] = scheduled_wait_seconds
                anchor_pause_totals[spec.point_id] = pause_elapsed
                point_state = state[task.name].setdefault("resource_points", {}).setdefault(spec.point_id, {})
                update_point_anchor_timing(point_state, observed_offset, observed_segment)
                save_state(args.state_file, state)
                log_event(
                    args.log_jsonl,
                    {
                        "event": "resource_point_timing_observed",
                        "task": task.name,
                        "route": route_name,
                        "point_id": spec.point_id,
                        "anchor_offset_seconds": point_state["anchor_offset_seconds"],
                        "anchor_offset_samples": point_state["anchor_offset_samples"],
                        "observed_offset_seconds": round(observed_offset, 3),
                        "observed_segment_seconds": (
                            round(observed_segment, 3) if observed_segment is not None else None
                        ),
                    },
                )
                if refresh_anchor is None or anchored_at >= refresh_anchor:
                    refresh_anchor = anchored_at
                    refresh_anchor_pause_seconds = (
                        max(0.0, runtime_control.total_pause_seconds - pause_at_start)
                        if runtime_control is not None
                        else 0.0
                    )
                if (
                    bool(getattr(args, "intermediate_saves", False))
                    and len(route_specs) > 1
                    and spec != route_specs[-1]
                ):
                    run_intermediate_save(
                        task,
                        route_name,
                        spec,
                        automation=automation,
                        args=args,
                        state=state,
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
                    before_action_hook=before_legacy_action,
                )
            if route_name == "sleep1" and not args.dry_run:
                try:
                    smart_runner.state_reader.find_text("休息中", exact=False)
                except StateReadError:
                    pass
                else:
                    smart_runner.state_reader.wait_for_text(
                        "休息中",
                        present=False,
                        timeout=20.0,
                        stable_samples=2,
                    )
                if task.name in NIGHT_GATED_TASKS:
                    if period_reader is None:
                        raise RuntimeError("Protected livestock route has no game-period reader.")
                    period = period_reader.wait_for_zi(timeout=8.0, stable_samples=2)
                    log_event(
                        args.log_jsonl,
                        {
                            "event": "night_rest_verified",
                            "task": task.name,
                            "route": route_name,
                            **period.as_dict(),
                        },
                    )
            log_event(args.log_jsonl, {"event": "route_completed", "task": task.name, "route": route_name})
            if runtime_control is not None:
                if route_index < len(task.routes):
                    runtime_control.replace_context(
                        task=task.name,
                        route=task.routes[route_index],
                        route_index=route_index + 1,
                        route_revision=route_revision(task.routes[route_index]),
                        total_routes=len(task.routes),
                        next_action_index=1,
                        task_started_at=started_at.isoformat(timespec="milliseconds"),
                        task_pause_baseline=pause_at_start,
                        scheduled_wait_seconds=scheduled_wait_seconds,
                        phase="between_routes",
                    )
                else:
                    runtime_control.replace_context(
                        task=task.name,
                        task_started_at=started_at.isoformat(timespec="milliseconds"),
                        task_pause_baseline=pause_at_start,
                        scheduled_wait_seconds=scheduled_wait_seconds,
                        phase="routes_completed",
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
        state[task.name]["retry_not_before"] = None
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
        if runtime_control is not None and session_manager is not None and not runtime_control.is_paused:
            try:
                session = session_manager.detect()
                if session.state in LOGIN_STATES:
                    runtime_control.request_pause(
                        reason=f"session_{session.state}",
                        source="task_failure_session_check",
                        details=session.as_dict(),
                    )
            except Exception as session_exc:
                log_event(
                    args.log_jsonl,
                    {
                        "event": "task_failure_session_check_failed",
                        "task": task.name,
                        "error": repr(session_exc),
                    },
                )
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
                retry_at = failed_at + timedelta(minutes=args.retry_minutes)
                state[task.name]["next_due"] = retry_at
                state[task.name]["retry_not_before"] = retry_at
            else:
                state[task.name]["retry_not_before"] = None
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
            retry_at = failed_at + timedelta(minutes=args.retry_minutes)
            state[task.name]["next_due"] = retry_at
            state[task.name]["retry_not_before"] = retry_at
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


def validate_continue_checkpoint(checkpoint, tasks):
    task_name = checkpoint.get("task")
    task = tasks.get(task_name)
    if task is None:
        raise ValueError(f"checkpoint task {task_name!r} is not active")
    route_index = int(checkpoint.get("route_index", 0))
    if route_index < 1 or route_index > len(task.routes):
        raise ValueError("checkpoint route index is outside the current task")
    route_name = task.routes[route_index - 1]
    if checkpoint.get("route") != route_name:
        raise ValueError(
            f"checkpoint route changed: expected {route_name}, got {checkpoint.get('route')}"
        )
    actions = route_actions(route_name)
    total_actions = len(actions)
    saved_revision = checkpoint.get("route_revision")
    current_revision = route_revision(route_name)
    if not saved_revision:
        raise ValueError("checkpoint has no route revision; use restart from the beginning")
    if saved_revision != current_revision:
        raise ValueError("checkpoint route definition changed; use restart from the beginning")
    next_action_index = int(checkpoint.get("next_action_index", 0))
    if next_action_index < 1 or next_action_index > total_actions + 1:
        raise ValueError("checkpoint action index is outside the current route")


def route_actions(route_name):
    if route_name in PROCEDURES and PROCEDURES[route_name].get("enabled"):
        return PROCEDURES[route_name]["actions"]
    if route_name in ROUTES:
        return ROUTES[route_name]
    raise ValueError(f"checkpoint route {route_name!r} is no longer available")


def route_revision(route_name):
    actions = route_actions(route_name)
    point_names = set()

    def collect(value):
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)
        elif isinstance(value, str) and value in pos:
            point_names.add(value)

    collect(actions)
    payload = {
        "route": route_name,
        "actions": actions,
        "named_points": {name: pos[name] for name in sorted(point_names)},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


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
    takeover_hud = DesktopTakeoverHUD(enabled=not args.no_takeover_hud)
    runtime_control = RuntimeControl(
        stop_event=stop_event,
        state_file=args.control_state_file,
        resume_hotkey=args.resume_hotkey,
        restart_hotkey=args.restart_hotkey,
        pause_hotkey=args.pause_hotkey,
        resume_delay_seconds=args.resume_delay_seconds,
        coordinate_tolerance=args.resume_coordinate_tolerance,
        detect_human_input=not args.no_human_input_pause,
        input_pause_policy=args.input_pause_policy,
        takeover_window_seconds=args.takeover_window_seconds,
        takeover_required_seconds=args.takeover_required_seconds,
        takeover_max_gap_seconds=args.takeover_max_gap_seconds,
        takeover_min_distance_pixels=args.takeover_min_distance_pixels,
        takeover_hud=takeover_hud,
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
    runtime_control.set_continue_validator(
        lambda checkpoint: validate_continue_checkpoint(checkpoint, tasks)
    )
    event_logger = lambda event: log_event(args.log_jsonl, event)
    session_manager = GameSessionManager(
        automation,
        state_reader=smart_runner.state_reader,
        event_logger=event_logger,
        capture_dir=args.capture_dir,
    )
    runtime_control.set_resume_preparer(
        lambda **request: session_manager.recover_latest_server_save(
            force=bool(request.get("force")),
        )
    )
    if args.start_paused and not runtime_control.is_paused:
        runtime_control.request_pause(
            reason="startup_pause",
            source="command_line",
            details={"start_paused": True},
        )
    period_reader = GamePeriodReader(
        automation,
        min_similarity=args.night_period_min_score,
    )
    session_watchdog = SessionWatchdog(
        session_manager,
        runtime_control,
        interval_seconds=args.session_check_seconds,
        event_logger=event_logger,
    )
    ledger = ResourceLedger(args.resource_history)
    hotkey = SchedulerStopHotkey(args.stop_hotkey, stop_event, args.log_jsonl)
    monitor = None
    if not args.no_web:
        try:
            web_auth_token = (
                read_auth_token_file(args.web_auth_token_file) if args.web_auth_token_file else None
            )
        except ValueError as exc:
            raise SystemExit(f"Cannot start monitoring service: {exc}") from exc
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
            monitor = MonitoringServer(
                host=args.web_host,
                port=args.web_port,
                data=monitor_data,
                auth_username=args.web_auth_user,
                auth_token=web_auth_token,
                tls_cert_file=args.web_tls_cert_file,
                tls_key_file=args.web_tls_key_file,
            )
        except (OSError, ValueError) as exc:
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
        if not args.no_session_watchdog:
            session_watchdog.start()
        if not args.no_stop_hotkey:
            print(f"Global stop hotkey enabled: {args.stop_hotkey}")
        if not args.no_human_input_pause:
            print(
                f"Human takeover policy: {args.input_pause_policy}; pause={args.pause_hotkey}, "
                f"continue={args.resume_hotkey}, restart={args.restart_hotkey}"
            )
        if monitor is not None:
            monitor.start()
            host, port = monitor.address
            print(f"Resource monitor: {monitor.scheme}://{host}:{port}")
        print_schedule(state)
        print(f"Starting scheduler in {args.startup_delay:g}s")
        try:
            automation.wait_seconds(args.startup_delay)
        except AutomationRecoveryHandoff:
            pass

        while True:
            try:
                automation.wait_seconds(0.0)
            except AutomationRecoveryHandoff:
                continue
            resume_ticket = runtime_control.peek_resume_checkpoint()
            resume_mode = (resume_ticket or {}).get("recovery_mode")
            expected_phase = resume_mode if resume_mode in {CONTINUE_STEP, RESTART_TASK} else None
            resume_task = (
                resume_ticket.get("task")
                if resume_ticket and expected_phase and resume_ticket.get("phase") == expected_phase
                else None
            )
            if resume_task in tasks:
                resume_ticket = runtime_control.consume_resume_checkpoint()
            elif resume_task:
                runtime_control.consume_resume_checkpoint()
                print(f"Discarding recovery ticket for inactive task {resume_task}.")
                log_event(
                    args.log_jsonl,
                    {
                        "event": "automation_resume_checkpoint_discarded",
                        "task": resume_task,
                        "mode": resume_mode,
                        "reason": "task_inactive",
                    },
                )
                resume_ticket = None
                resume_task = None
            elif resume_ticket and not resume_task:
                runtime_control.consume_resume_checkpoint()
                resume_ticket = None

            if (
                resume_ticket
                and resume_mode == RESTART_TASK
                and all_resources_ready(state, tasks)
            ):
                log_event(
                    args.log_jsonl,
                    {
                        "event": "recovery_full_cycle_selected",
                        "abandoned_task": resume_task,
                        "reason": "all_active_resources_ready",
                    },
                )
                resume_ticket = None
                resume_task = None

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
            recovery_handoff = False
            for name in batch_names:
                if name not in tasks:
                    print(f"Skipping unknown/inactive task {name}")
                    continue
                try:
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
                        resume_checkpoint=(
                            resume_ticket
                            if name == resume_task and resume_mode == CONTINUE_STEP
                            else None
                        ),
                        recovery_ticket=(
                            resume_ticket
                            if name == resume_task and resume_mode == RESTART_TASK
                            else None
                        ),
                        ledger=ledger,
                        period_reader=period_reader,
                        session_manager=session_manager,
                    )
                except AutomationRecoveryHandoff:
                    recovery_handoff = True
                    break
            if recovery_handoff:
                continue
            completed_batch = completed_batch or bool(batch_names)

            if args.once or (args.wait_once and completed_batch) or (args.run_now is not None and not args.loop):
                break
            until_next = seconds_until_next_start(state, tasks)
            if not names and until_next == 0:
                sleep_seconds = args.poll_seconds
            else:
                sleep_seconds = args.poll_seconds if until_next is None else min(args.poll_seconds, until_next)
            try:
                automation.wait_seconds(max(0.01, sleep_seconds))
            except AutomationRecoveryHandoff:
                continue
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
        if not args.no_session_watchdog:
            session_watchdog.stop()
        if monitor is not None:
            monitor.stop()
        signal.signal(signal.SIGINT, previous_sigint_handler)
        save_state(args.state_file, state)
        print_schedule(state)


if __name__ == "__main__":
    main()
