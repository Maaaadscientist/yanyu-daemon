import argparse
import re
from pathlib import Path

import cv2


def find_latest_screenshot(directory):
    pattern = re.compile(r"screenshot_high_res_(\d{8})_(\d{6})\.png")
    screenshots = [path for path in Path(directory).iterdir() if path.is_file() and pattern.match(path.name)]
    if not screenshots:
        return None
    return max(screenshots, key=lambda path: pattern.match(path.name).groups())


def parse_args():
    parser = argparse.ArgumentParser(description="Inspect a screenshot and print coordinates for coordinates.py.")
    parser.add_argument("image", nargs="?", help="Screenshot path. Defaults to latest screenshot_high_res_*.png.")
    parser.add_argument("--dir", default=".", help="Directory to scan when no image is provided.")
    parser.add_argument("--name", default="新坐标", help="Coordinate name printed when clicking.")
    return parser.parse_args()


def main():
    args = parse_args()
    image_path = Path(args.image) if args.image else find_latest_screenshot(args.dir)
    if not image_path:
        raise SystemExit("No screenshot found.")

    img = cv2.imread(str(image_path))
    if img is None:
        raise SystemExit(f"Cannot open {image_path}")

    window_name = "Image Analyzer"
    height, width = img.shape[:2]
    print(f"Analyzing {image_path} ({width}x{height})")
    print("Move mouse to inspect. Left click prints a coordinates.py line. Press any key to exit.")

    def show_pixel_value(event, x, y, flags, param):
        pixel_value = img[y, x]
        text = f"X: {x}, Y: {y}, BGR: {pixel_value.tolist()}"
        img_copy = img.copy()
        text_size, _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 3)
        text_w, text_h = text_size
        cv2.rectangle(img_copy, (10, 20), (10 + text_w + 8, 20 + text_h + 12), (0, 0, 0), -1)
        cv2.putText(img_copy, text, (14, 26 + text_h), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 3)
        cv2.drawMarker(img_copy, (x, y), (0, 255, 255), markerType=cv2.MARKER_CROSS, markerSize=28, thickness=2)
        cv2.imshow(window_name, img_copy)

        if event == cv2.EVENT_LBUTTONDOWN:
            print(f"'{args.name}':({x},{y}),")

    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(window_name, show_pixel_value)
    cv2.imshow(window_name, img)
    cv2.waitKey(0)
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
