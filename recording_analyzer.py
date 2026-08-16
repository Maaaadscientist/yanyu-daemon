import argparse
import json
from collections import Counter
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Quantify a manual control recording.")
    parser.add_argument("recording", help="JSONL recording or .procedure.json file from control_recorder.py.")
    parser.add_argument("--slow-delay", type=float, default=3.0, help="Report actions with delay above this value.")
    return parser.parse_args()


def read_events(path):
    events = []
    annotations = []
    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("record_type") == "annotation":
                annotations.append(record)
            else:
                events.append(record)
    for annotation in annotations:
        field = annotation.get("field")
        index = int(annotation.get("action_index", 0)) - 1
        if not field or not 0 <= index < len(events):
            continue
        if annotation.get("unique"):
            for event in events:
                event.pop(field, None)
        events[index][field] = annotation.get("value", True)
    return events


def analyze_procedure(path):
    procedure = json.loads(Path(path).read_text(encoding="utf-8"))
    if any(action.get("type") == "include" for action in procedure.get("actions", [])):
        from smart_automation import load_procedures

        procedure = load_procedures(Path(path).parent)[procedure["name"]]
    actions = procedure.get("actions", [])
    action_counts = Counter(action.get("type", "unknown") for action in actions)
    click_count = action_counts["click"] + action_counts["click_text"] + action_counts["aligned_click"]
    click_count += sum(len(action.get("points", ())) for action in actions if action.get("type") == "rapid_clicks")
    verified = [action for action in actions if action.get("expect") or action.get("expect_text")]
    semantic = [
        action
        for action in actions
        if action.get("target_text") or action.get("expect_text") or action.get("type") == "click_text"
    ]
    anchors = [index for index, action in enumerate(actions, start=1) if action.get("refresh_anchor")]
    observed = [float(action["observed_seconds"]) for action in verified if "observed_seconds" in action]
    rapid_actions = [action for action in actions if action.get("type") == "rapid_clicks"]

    print(f"file: {Path(path).name}")
    print(f"procedure: {procedure.get('name')} enabled={procedure.get('enabled', False)}")
    print(
        f"actions: {len(actions)} clicks={click_count} drags={action_counts['drag']} "
        f"verified={len(verified)} text_located={len(semantic)}"
    )
    if rapid_actions:
        rapid_clicks = sum(len(action.get("points", ())) for action in rapid_actions)
        rapid_gaps = [float(gap) for action in rapid_actions for gap in action.get("intervals", ())]
        print(
            f"rapid_sequences: {len(rapid_actions)} clicks={rapid_clicks} "
            f"max_recorded_gap={max(rapid_gaps, default=0.0):.3f}s"
        )
    print(
        f"map_path: {procedure.get('map') or '?'} "
        f"{procedure.get('start_coordinate') or '?'} -> {procedure.get('target_coordinate') or '?'}"
    )
    print(f"refresh_anchor_actions: {anchors or 'none'}")
    if observed:
        print(
            f"verified_wait: total={sum(observed):.2f}s "
            f"mean={sum(observed) / len(observed):.2f}s max={max(observed):.2f}s"
        )
    schedule = procedure.get("schedule") or {}
    if schedule:
        print(f"schedule_interval: {schedule.get('interval_minutes')}m")
        if not anchors:
            print("warning: scheduled procedure has no exact refresh anchor")


def main():
    args = parse_args()
    if args.recording.endswith(".json"):
        analyze_procedure(args.recording)
        return
    events = read_events(args.recording)
    if not events:
        raise SystemExit("No events found.")

    action_counts = Counter(event["type"] for event in events)
    total_delay = sum(event["delay"] for event in events)
    total_duration = sum(event.get("duration", 0) for event in events)
    slow = [(index, event) for index, event in enumerate(events, start=1) if event["delay"] >= args.slow_delay]
    drags = [event for event in events if event["type"] == "drag"]
    rapid_groups = {}
    for event in events:
        if event.get("rapid_group") is not None:
            rapid_groups.setdefault(event["rapid_group"], []).append(event)
    checkpoints = [index for index, event in enumerate(events, start=1) if event.get("checkpoint")]
    anchors = [index for index, event in enumerate(events, start=1) if event.get("refresh_anchor")]

    print(f"file: {Path(args.recording).name}")
    print(f"actions: {len(events)} clicks={action_counts['click']} drags={action_counts['drag']}")
    print(f"wait_time: {total_delay:.2f}s")
    print(f"press_hold_time: {total_duration:.2f}s")
    print(f"estimated_route_time: {total_delay + 2.5:.2f}s")
    print(f"checkpoint_actions: {checkpoints or 'none'}")
    print(f"refresh_anchor_actions: {anchors or 'none'}")

    if drags:
        drag_distances = [event["distance"] for event in drags]
        print(f"drag_distance: mean={sum(drag_distances) / len(drag_distances):.1f} max={max(drag_distances):.1f}")

    if rapid_groups:
        gaps = []
        for group in rapid_groups.values():
            times = [float(event["monotonic"]) for event in group]
            gaps.extend(later - earlier for earlier, later in zip(times, times[1:]))
        print(
            f"rapid_groups: {len(rapid_groups)} clicks={sum(len(group) for group in rapid_groups.values())} "
            f"mean_gap={sum(gaps) / len(gaps) if gaps else 0.0:.3f}s "
            f"max_gap={max(gaps, default=0.0):.3f}s"
        )

    if slow:
        print("slow waits:")
        for index, event in slow[:20]:
            target = event.get("reference") or f"{event.get('start_reference')}->{event.get('end_reference')}"
            print(f"  {index}: {event['type']} target={target} delay={event['delay']}")
        if len(slow) > 20:
            print(f"  ... {len(slow) - 20} more")


if __name__ == "__main__":
    main()
