import argparse
import json
from collections import Counter
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Quantify a manual control recording.")
    parser.add_argument("recording", help="JSONL file from control_recorder.py.")
    parser.add_argument("--slow-delay", type=float, default=3.0, help="Report actions with delay above this value.")
    return parser.parse_args()


def read_events(path):
    events = []
    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def main():
    args = parse_args()
    events = read_events(args.recording)
    if not events:
        raise SystemExit("No events found.")

    action_counts = Counter(event["type"] for event in events)
    total_delay = sum(event["delay"] for event in events)
    total_duration = sum(event.get("duration", 0) for event in events)
    slow = [(index, event) for index, event in enumerate(events, start=1) if event["delay"] >= args.slow_delay]
    drags = [event for event in events if event["type"] == "drag"]

    print(f"file: {Path(args.recording).name}")
    print(f"actions: {len(events)} clicks={action_counts['click']} drags={action_counts['drag']}")
    print(f"wait_time: {total_delay:.2f}s")
    print(f"press_hold_time: {total_duration:.2f}s")
    print(f"estimated_route_time: {total_delay + 2.5:.2f}s")

    if drags:
        drag_distances = [event["distance"] for event in drags]
        print(f"drag_distance: mean={sum(drag_distances) / len(drag_distances):.1f} max={max(drag_distances):.1f}")

    if slow:
        print("slow waits:")
        for index, event in slow[:20]:
            target = event.get("reference") or f"{event.get('start_reference')}->{event.get('end_reference')}"
            print(f"  {index}: {event['type']} target={target} delay={event['delay']}")
        if len(slow) > 20:
            print(f"  ... {len(slow) - 20} more")


if __name__ == "__main__":
    main()
