import argparse

from ApplicationServices import (
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    kAXChildrenAttribute,
    kAXDescriptionAttribute,
    kAXHelpAttribute,
    kAXIdentifierAttribute,
    kAXRoleAttribute,
    kAXTitleAttribute,
    kAXValueAttribute,
    kAXWindowsAttribute,
)

from automation import get_window_info


ATTRIBUTES = (
    ("role", kAXRoleAttribute),
    ("title", kAXTitleAttribute),
    ("description", kAXDescriptionAttribute),
    ("identifier", kAXIdentifierAttribute),
    ("help", kAXHelpAttribute),
    ("value", kAXValueAttribute),
)


def parse_args():
    parser = argparse.ArgumentParser(description="Inspect the game's read-only Accessibility tree.")
    parser.add_argument("--game-title", default="烟雨江湖")
    parser.add_argument("--max-depth", type=int, default=12)
    parser.add_argument("--max-nodes", type=int, default=500)
    return parser.parse_args()


def attribute(element, name):
    error, value = AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


def action_names(element):
    error, names = AXUIElementCopyActionNames(element, None)
    return tuple(str(name) for name in names) if error == 0 and names else ()


def describe(element):
    details = []
    for label, attribute_name in ATTRIBUTES:
        value = attribute(element, attribute_name)
        if value not in (None, "", (), []):
            details.append(f"{label}={value!s}")
    actions = action_names(element)
    if actions:
        details.append(f"actions={','.join(actions)}")
    return " ".join(details) or "(no exposed attributes)"


def walk(element, *, max_depth, max_nodes, depth=0, counter=None):
    if counter is None:
        counter = [0]
    if counter[0] >= max_nodes:
        return
    counter[0] += 1
    print(f"{'  ' * depth}{counter[0]:03d} {describe(element)}")
    if depth >= max_depth:
        return
    children = attribute(element, kAXChildrenAttribute) or ()
    for child in children:
        if counter[0] >= max_nodes:
            break
        walk(
            child,
            max_depth=max_depth,
            max_nodes=max_nodes,
            depth=depth + 1,
            counter=counter,
        )


def main():
    args = parse_args()
    window = get_window_info(args.game_title)
    if window is None or not window.owner_pid:
        raise RuntimeError(f"No game process found for {args.game_title!r}.")
    print(f"game_pid={window.owner_pid} window_number={window.number}")
    application = AXUIElementCreateApplication(window.owner_pid)
    windows = attribute(application, kAXWindowsAttribute) or ()
    root = windows[0] if windows else application
    walk(
        root,
        max_depth=max(0, args.max_depth),
        max_nodes=max(1, args.max_nodes),
    )


if __name__ == "__main__":
    main()
