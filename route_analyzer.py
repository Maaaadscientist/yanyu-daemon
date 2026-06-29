import argparse
from collections import Counter

from coordinates import *


def is_route(value):
    return isinstance(value, list) and all(isinstance(item, tuple) and len(item) == 2 for item in value)


ROUTES = {name: value for name, value in globals().items() if is_route(value) and not name.startswith("_")}
POINT_NAMES = {value: key for key, value in pos.items()}


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze coordinate routes without controlling the game.")
    parser.add_argument("routes", nargs="*", help="Route names. Defaults to all routes.")
    parser.add_argument("--width", type=int, default=2102, help="Reference screenshot width.")
    parser.add_argument("--height", type=int, default=1632, help="Reference screenshot height.")
    parser.add_argument("--top", type=int, default=10, help="Warn for points this close to the top edge.")
    parser.add_argument("--bottom", type=int, default=10, help="Warn for points this close to the bottom edge.")
    return parser.parse_args()


def target_points(target):
    if isinstance(target[0], int):
        return [target]
    return [target[0], target[1]]


def analyze_route(name, route, width, height, top_margin, bottom_margin):
    clicks = 0
    drags = 0
    total_delay = 0.0
    warnings = []
    point_counter = Counter()

    for index, (target, delay) in enumerate(route, start=1):
        total_delay += delay
        if isinstance(target[0], int):
            clicks += 1
        else:
            drags += 1

        for point in target_points(target):
            point_counter[POINT_NAMES.get(point, str(point))] += 1
            x, y = point
            if x < 0 or y < 0 or x > width or y > height:
                warnings.append(f"{index}: out of bounds {POINT_NAMES.get(point, point)}")
            elif y <= top_margin or y >= height - bottom_margin:
                warnings.append(f"{index}: near vertical edge {POINT_NAMES.get(point, point)}")

    repeated = [f"{name}x{count}" for name, count in point_counter.most_common(5) if count > 1]
    return {
        "name": name,
        "actions": len(route),
        "clicks": clicks,
        "drags": drags,
        "delay": total_delay,
        "estimated": total_delay + 3.5,
        "repeated": repeated,
        "warnings": warnings,
    }


def main():
    args = parse_args()
    route_names = args.routes or sorted(ROUTES)
    missing = [name for name in route_names if name not in ROUTES]
    if missing:
        raise SystemExit(f"Unknown route(s): {', '.join(missing)}")

    for route_name in route_names:
        result = analyze_route(route_name, ROUTES[route_name], args.width, args.height, args.top, args.bottom)
        print(
            f"{result['name']}: actions={result['actions']} clicks={result['clicks']} "
            f"drags={result['drags']} wait={result['delay']:.1f}s estimated={result['estimated']:.1f}s"
        )
        if result["repeated"]:
            print(f"  repeated: {', '.join(result['repeated'])}")
        for warning in result["warnings"][:8]:
            print(f"  warning: {warning}")
        if len(result["warnings"]) > 8:
            print(f"  warning: ... {len(result['warnings']) - 8} more")


if __name__ == "__main__":
    main()
