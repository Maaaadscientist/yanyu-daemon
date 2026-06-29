import argparse
import json
import time

from coordinates import *

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
    return parser.parse_args()


def main():
    args = parse_args()

    if args.list:
        for name in sorted(ROUTES):
            print(name)
        return

    route_names = args.routes or DEFAULT_SEQUENCE
    missing = [name for name in route_names if name not in ROUTES]
    if missing:
        raise SystemExit(f"Unknown route(s): {', '.join(missing)}. Use --list to see route names.")

    if args.analyze:
        total_actions = 0
        total_seconds = 0.0
        for route_name in route_names:
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
        print(f"total: routes={len(route_names)} actions={total_actions} estimated={total_seconds:.1f}s")
        return

    from automation import GameAutomation

    automation = GameAutomation(
        reference_image=args.reference,
        position_names={v: k for k, v in pos.items()},
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
