import argparse
import json
import re
from pathlib import Path

import cv2
import numpy as np


CAPTURE_RE = re.compile(r"(.+)_(\d{3})_(\d{8})_(\d{6})\.png$")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Review --log-jsonl and --capture-each-action output to find route-control patterns."
    )
    parser.add_argument("--log-jsonl", required=True, help="JSONL status log written by test.py.")
    parser.add_argument("--capture-dir", required=True, help="Directory containing per-action screenshots.")
    parser.add_argument("--low-change", type=float, default=0.003, help="Warn below this changed-pixel ratio.")
    parser.add_argument("--high-change", type=float, default=0.25, help="Mark above this changed-pixel ratio.")
    return parser.parse_args()


def read_log(path):
    records = []
    with open(path, "r", encoding="utf-8") as log_file:
        for line in log_file:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def capture_key(path):
    match = CAPTURE_RE.match(path.name)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def load_captures(directory):
    captures = {}
    for path in Path(directory).glob("*.png"):
        key = capture_key(path)
        if key:
            captures[key] = path
    return captures


def changed_ratio(before_path, after_path):
    before = cv2.imread(str(before_path), cv2.IMREAD_GRAYSCALE)
    after = cv2.imread(str(after_path), cv2.IMREAD_GRAYSCALE)
    if before is None or after is None:
        return None
    if before.shape != after.shape:
        after = cv2.resize(after, (before.shape[1], before.shape[0]))
    diff = cv2.absdiff(before, after)
    return float(np.count_nonzero(diff > 8) / diff.size)


def main():
    args = parse_args()
    records = read_log(args.log_jsonl)
    captures = load_captures(args.capture_dir)

    previous_capture = None
    previous_record = None
    route_stats = {}

    for record in records:
        key = (record["route"], record["index"])
        capture = captures.get(key)
        if not capture:
            continue

        ratio = changed_ratio(previous_capture, capture) if previous_capture else None
        stats = route_stats.setdefault(record["route"], {"actions": 0, "low": 0, "high": 0, "changes": []})
        stats["actions"] += 1

        if ratio is not None:
            stats["changes"].append(ratio)
            label = "ok"
            if ratio < args.low_change:
                label = "low-change"
                stats["low"] += 1
            elif ratio > args.high_change:
                label = "high-change"
                stats["high"] += 1
            print(
                f"{record['route']} {record['index']:03d}/{record['total']:03d} "
                f"{record['type']} {record['name']} change={ratio:.4f} {label}"
            )

        previous_capture = capture
        previous_record = record

    print("summary")
    for route, stats in route_stats.items():
        mean_change = sum(stats["changes"]) / len(stats["changes"]) if stats["changes"] else 0.0
        print(
            f"{route}: captures={stats['actions']} mean_change={mean_change:.4f} "
            f"low_change={stats['low']} high_change={stats['high']}"
        )


if __name__ == "__main__":
    main()
