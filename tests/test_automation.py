import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

import automation
from automation import GameAutomation, RapidClickTimingError, get_window_info


class AutomationTests(unittest.TestCase):
    def make_automation(self):
        instance = object.__new__(GameAutomation)
        instance.window_left = 0
        instance.window_top = 0
        instance.scale_x = 1
        instance.scale_y = 1
        instance.reference_width = 400
        instance.reference_height = 300
        return instance

    def test_window_lookup_keeps_owner_pid_for_focus_activation(self):
        window = {
            "kCGWindowOwnerName": "烟雨江湖",
            "kCGWindowName": "烟雨江湖",
            "kCGWindowOwnerPID": 1234,
            "kCGWindowNumber": 99,
            "kCGWindowBounds": {"X": 1, "Y": 2, "Width": 3, "Height": 4},
        }

        with patch("automation.CGWindowListCopyWindowInfo", return_value=[window]):
            result = get_window_info("烟雨江湖")

        self.assertEqual(result.owner_pid, 1234)

    def test_window_lookup_prefers_largest_normal_layer_window(self):
        popup = {
            "kCGWindowOwnerName": "烟雨江湖",
            "kCGWindowName": "popup",
            "kCGWindowOwnerPID": 1234,
            "kCGWindowNumber": 1,
            "kCGWindowLayer": 3,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1400, "Height": 900},
        }
        main = {
            "kCGWindowOwnerName": "烟雨江湖",
            "kCGWindowName": "烟雨江湖",
            "kCGWindowOwnerPID": 1234,
            "kCGWindowNumber": 2,
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 10, "Y": 20, "Width": 1100, "Height": 850},
        }

        with patch("automation.CGWindowListCopyWindowInfo", return_value=[popup, main]):
            result = get_window_info("烟雨江湖")

        self.assertEqual(result.number, 2)

    def test_rapid_clicks_follow_absolute_deadlines_without_drift(self):
        instance = self.make_automation()
        clock = [10.0]
        clicks = []

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        def click(**kwargs):
            clicks.append((clock[0], kwargs["x"], kwargs["y"]))
            clock[0] += 0.02

        with (
            patch("automation.time.monotonic", side_effect=monotonic),
            patch("automation.time.sleep", side_effect=sleep),
            patch("automation.pyautogui.click", side_effect=click),
        ):
            result = instance.rapid_click_reference(
                [(10, 20), (30, 40), (50, 60)],
                intervals=[0.2, 0.2],
                max_gap_seconds=0.3,
            )

        self.assertEqual([(x, y) for _, x, y in clicks], [(10, 20), (30, 40), (50, 60)])
        self.assertAlmostEqual(result.gaps[0], 0.2)
        self.assertAlmostEqual(result.gaps[1], 0.2)

    def test_rapid_clicks_finish_all_points_before_reporting_an_overrun(self):
        instance = self.make_automation()
        clock = [20.0]
        clicks = []

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        def click(**kwargs):
            clicks.append((kwargs["x"], kwargs["y"]))
            clock[0] += 0.4

        with (
            patch("automation.time.monotonic", side_effect=monotonic),
            patch("automation.time.sleep", side_effect=sleep),
            patch("automation.pyautogui.click", side_effect=click),
        ):
            with self.assertRaises(RapidClickTimingError):
                instance.rapid_click_reference(
                    [(10, 20), (30, 40), (50, 60)],
                    intervals=[0.1, 0.1],
                    max_gap_seconds=0.3,
                )

        self.assertEqual(clicks, [(10, 20), (30, 40), (50, 60)])

    def test_feature_alignment_maps_a_canonical_point_after_map_pan(self):
        instance = self.make_automation()
        rng = np.random.default_rng(7)
        canonical = rng.integers(0, 256, size=(300, 400, 3), dtype=np.uint8)
        translation = np.float32([[1, 0, 30], [0, 1, -12]])
        current = cv2.warpAffine(canonical, translation, (400, 300))
        instance.capture_image = lambda: Image.fromarray(current)

        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory, "map.png")
            Image.fromarray(canonical).save(reference)
            result = instance.align_reference_point(reference, (150, 150), min_matches=20)

        self.assertAlmostEqual(result.point[0], 180, delta=2)
        self.assertAlmostEqual(result.point[1], 138, delta=2)
        self.assertGreater(result.inlier_ratio, 0.8)


if __name__ == "__main__":
    unittest.main()
