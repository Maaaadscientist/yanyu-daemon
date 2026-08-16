import argparse
import json
import time

from coordinates import SMART_ROUTES, pos
from smart_automation import ProcedureExecutionError, SmartProcedureRunner, VisionGameStateReader, load_procedures


def parse_args():
    parser = argparse.ArgumentParser(description="Inspect or run coordinate-verified smart procedures.")
    parser.add_argument("procedure", nargs="?", help="Procedure name to run.")
    parser.add_argument("--list", action="store_true", help="List installed procedures and drafts.")
    parser.add_argument("--read-state", action="store_true", help="Read the current map and grid coordinate, then exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without clicking or checking state.")
    parser.add_argument("--step", action="store_true", help="Wait for Enter before each smart action.")
    parser.add_argument("--procedure-dir", default="procedures", help="Installed procedure directory.")
    parser.add_argument("--reference", default="game_screenshot.png", help="Reference screenshot for click scaling.")
    parser.add_argument("--timing-file", default="runtime_profiles.json", help="Learned timing profile.")
    parser.add_argument("--scheduler-state", default="scheduler_state.json", help="Scheduler state updated by a real refresh anchor.")
    parser.add_argument("--no-sync-scheduler", action="store_true", help="Do not sync a real refresh anchor.")
    parser.add_argument("--log-jsonl", help="Optional structured action log.")
    parser.add_argument("--startup-delay", type=float, default=3.0, help="Seconds before execution.")
    return parser.parse_args()


def main():
    args = parse_args()
    procedures = load_procedures(args.procedure_dir, builtins=SMART_ROUTES)
    if args.list:
        for name, procedure in sorted(procedures.items()):
            if procedure.get("library"):
                status = "library"
            else:
                status = "ready" if procedure.get("enabled") and procedure.get("actions") else "draft"
            source = procedure.get("source", "coordinates.py")
            print(f"{name}: {status} actions={len(procedure.get('actions', []))} source={source}")
        return

    if not args.read_state and not args.procedure:
        raise SystemExit("Provide a procedure name, --list, or --read-state.")

    from automation import GameAutomation

    automation = GameAutomation(reference_image=args.reference, position_names={value: key for key, value in pos.items()})
    if args.read_state:
        state = VisionGameStateReader(automation).read_state()
        print(json.dumps(state.as_dict(), ensure_ascii=False))
        return

    if args.procedure not in procedures:
        raise SystemExit(f"Unknown procedure: {args.procedure}. Use --list to inspect available names.")
    if procedures[args.procedure].get("library"):
        raise SystemExit(f"Procedure {args.procedure} is a reusable library segment, not a standalone route.")
    if not procedures[args.procedure].get("enabled"):
        raise SystemExit(f"Procedure {args.procedure} is disabled or still a draft.")

    log_file = open(args.log_jsonl, "a", encoding="utf-8") if args.log_jsonl else None

    def log_action(status):
        if log_file:
            log_file.write(json.dumps(status, ensure_ascii=False) + "\n")
            log_file.flush()

    print(f"Starting {args.procedure} in {args.startup_delay:g}s")
    time.sleep(max(0.0, args.startup_delay))
    try:
        try:
            result = SmartProcedureRunner(
                automation,
                named_points=pos,
                timing_file=args.timing_file,
            ).run(
                procedures[args.procedure],
                dry_run=args.dry_run,
                action_logger=log_action,
                step=args.step,
            )
        except ProcedureExecutionError as exc:
            anchor = exc.refresh_anchor.isoformat(timespec="milliseconds") if exc.refresh_anchor else "none"
            raise SystemExit(
                f"failed {args.procedure}: action={exc.action_index} type={exc.action_type} "
                f"label={exc.action_label or '-'} refresh_anchor={anchor}; {exc}"
            ) from None
        if not args.dry_run and not args.no_sync_scheduler and result.refresh_anchor:
            from tracking_click import sync_external_procedure_result

            next_due = sync_external_procedure_result(
                args.scheduler_state,
                procedures[args.procedure],
                result,
            )
            if next_due:
                print(f"synced scheduler target={next_due.isoformat(timespec='milliseconds')}")
        print(
            f"completed {result.name}: actions={result.actions_completed} "
            f"refresh_anchor={result.refresh_anchor.isoformat(timespec='milliseconds') if result.refresh_anchor else 'none'}"
        )
    finally:
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()
