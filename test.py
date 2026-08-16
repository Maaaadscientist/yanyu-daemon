import argparse
import json
import time
from collections import Counter

from coordinates import *
from smart_automation import SmartProcedureRunner, load_procedures

DEFAULT_SEQUENCE = [
    "luoyangrichang1",
    "hangzhouxiuwei",
    "xiaotili",
    "luoyangrichang2",
    "wenxiangjiao",
    "jujingyunbiao",
    "wudao",
    "wushendian",
    "diling1",
    "save",
]

def is_route(value):
    return isinstance(value, list) and all(isinstance(item, tuple) and len(item) == 2 for item in value)


ROUTES = {name: value for name, value in globals().items() if is_route(value) and not name.startswith("_")}


def parse_args():
    parser = argparse.ArgumentParser(description="Run recorded game automation routes.")
    parser.add_argument(
        "routes",
        nargs="*",
        help="Route names from coordinates.py. Defaults to the final daily sequence.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print mapped screen coordinates without moving or clicking.",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Print route timing/action summary without requiring the game window.",
    )
    parser.add_argument(
        "--step",
        action="store_true",
        help="Wait for Enter before each action.",
    )
    parser.add_argument(
        "--log-jsonl",
        help="Write per-action status records to this JSONL file.",
    )
    parser.add_argument(
        "--capture-dir",
        help="Capture a game-window screenshot after each route, or after each action with --capture-each-action.",
    )
    parser.add_argument(
        "--capture-each-action",
        action="store_true",
        help="Capture a screenshot after every executed action.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available route names and exit.",
    )
    parser.add_argument(
        "--reference",
        default="game_screenshot.png",
        help="Reference screenshot used for coordinate scaling.",
    )
    parser.add_argument(
        "--startup-delay",
        type=float,
        default=5.0,
        help="Seconds to wait before starting routes.",
    )
    parser.add_argument("--procedure-dir", default="procedures", help="Installed smart procedure directory.")
    parser.add_argument("--timing-file", default="runtime_profiles.json", help="Learned smart-route timing data.")
    parser.add_argument("--scheduler-state", default="scheduler_state.json", help="Scheduler state updated by real smart runs.")
    parser.add_argument("--no-sync-scheduler", action="store_true", help="Do not sync a real smart refresh anchor.")
    return parser.parse_args()


def main():
    args = parse_args()
    procedures = load_procedures(args.procedure_dir, builtins=SMART_ROUTES)
    ready_procedures = {
        name: procedure
        for name, procedure in procedures.items()
        if procedure.get("enabled") and procedure.get("actions") and not procedure.get("library")
    }

    if args.list:
        for name in sorted(ROUTES):
            if name not in ready_procedures:
                print(f"{name}: legacy")
        for name, procedure in sorted(procedures.items()):
            if procedure.get("library"):
                status = "library"
            elif name in ready_procedures:
                status = "smart"
            else:
                status = "draft"
            print(f"{name}: {status}")
        return

    route_names = args.routes or DEFAULT_SEQUENCE
    missing = [name for name in route_names if name not in ROUTES and name not in ready_procedures]
    if missing:
        raise SystemExit(f"Unknown route(s): {', '.join(missing)}. Use --list to see route names.")

    if args.analyze:
        total_actions = 0
        total_seconds = 0.0
        for route_name in route_names:
            if route_name in ready_procedures:
                actions = ready_procedures[route_name]["actions"]
                counts = Counter(action["type"] for action in actions)
                duration = sum(float(action.get("before_delay", 0.0)) for action in actions)
                clicks = counts["click"] + counts["click_text"] + counts["aligned_click"]
                clicks += sum(len(action.get("points", ())) for action in actions if action["type"] == "rapid_clicks")
                verified = sum(1 for action in actions if action.get("expect") or action.get("expect_text"))
                total_actions += len(actions)
                print(
                    f"{route_name}: smart actions={len(actions)} clicks={clicks} "
                    f"drags={counts['drag']} verified={verified} explicit_wait={duration:.1f}s"
                )
                total_seconds += duration
                continue

            route = ROUTES[route_name]
            clicks = sum(1 for target, _ in route if isinstance(target[0], int))
            drags = len(route) - clicks
            duration = 1.0 + sum(delay for _, delay in route) + 2.5
            total_actions += len(route)
            total_seconds += duration
            print(
                f"{route_name}: actions={len(route)} clicks={clicks} drags={drags} "
                f"estimated={duration:.1f}s"
            )
        print(
            f"total: routes={len(route_names)} actions={total_actions} "
            f"stored_wait_floor={total_seconds:.1f}s (state-driven waits excluded)"
        )
        return

    from automation import GameAutomation

    automation = GameAutomation(
        reference_image=args.reference,
        position_names={v: k for k, v in pos.items()},
    )
    smart_runner = SmartProcedureRunner(
        automation,
        named_points=pos,
        timing_file=args.timing_file,
    )

    log_file = open(args.log_jsonl, "a", encoding="utf-8") if args.log_jsonl else None

    def log_status(status):
        if log_file:
            log_file.write(json.dumps(status, ensure_ascii=False) + "\n")
            log_file.flush()

    print(f"Starting in {args.startup_delay:g}s. Routes: {', '.join(route_names)}")
    if args.dry_run:
        print("Dry run enabled: no mouse movement or clicks will be performed.")
    if args.step:
        print("Step mode enabled: press Enter before each action.")
    time.sleep(args.startup_delay)

    try:
        for route_name in route_names:
            print(f"route {route_name}")
            if route_name in ready_procedures:
                result = smart_runner.run(
                    ready_procedures[route_name],
                    dry_run=args.dry_run,
                    action_logger=log_status,
                    step=args.step,
                )
                if not args.dry_run and not args.no_sync_scheduler and result.refresh_anchor:
                    from tracking_click import sync_external_procedure_result

                    next_due = sync_external_procedure_result(
                        args.scheduler_state,
                        ready_procedures[route_name],
                        result,
                    )
                    if next_due:
                        print(f"synced {route_name} scheduler target={next_due.isoformat(timespec='milliseconds')}")
                if args.capture_dir and not args.dry_run:
                    automation.capture_screenshot(args.capture_dir, route_name)
                print(
                    f"completed {route_name}: actions={result.actions_completed} "
                    f"refresh_anchor={result.refresh_anchor.isoformat(timespec='milliseconds') if result.refresh_anchor else 'none'}"
                )
                continue
            automation.run_actions(
                ROUTES[route_name],
                route_name=route_name,
                print_names=True,
                dry_run=args.dry_run,
                step=args.step,
                action_logger=log_status,
                capture_dir=args.capture_dir,
                capture_each_action=args.capture_each_action,
            )
    finally:
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()
