import unittest
from unittest import mock

from input_takeover import DesktopTakeoverHUD, MouseTakeoverDetector


class MouseTakeoverDetectorTests(unittest.TestCase):
    def test_sustained_movement_triggers_inside_the_rolling_window(self):
        detector = MouseTakeoverDetector(
            window_seconds=3.0,
            required_seconds=2.0,
            max_gap_seconds=0.2,
            min_distance_pixels=1,
        )
        progress = detector.feed(0, 0, now=10.0)
        for index in range(1, 22):
            progress = detector.feed(index * 3, 0, now=10.0 + index * 0.1)

        self.assertTrue(progress.triggered)
        self.assertGreaterEqual(progress.active_seconds, 2.0)

    def test_short_movement_expires_without_triggering(self):
        detector = MouseTakeoverDetector(
            window_seconds=3.0,
            required_seconds=2.0,
            max_gap_seconds=0.2,
        )
        detector.feed(0, 0, now=5.0)
        progress = detector.feed(10, 0, now=5.1)

        self.assertTrue(progress.active)
        self.assertFalse(progress.triggered)
        expired = detector.poll(now=8.2)
        self.assertFalse(expired.active)
        self.assertEqual(expired.progress, 0.0)

    def test_a_long_idle_gap_is_not_counted_as_active_movement(self):
        detector = MouseTakeoverDetector(
            window_seconds=3.0,
            required_seconds=2.0,
            max_gap_seconds=0.2,
        )
        detector.feed(0, 0, now=1.0)
        progress = detector.feed(100, 100, now=2.0)

        self.assertFalse(progress.active)
        self.assertFalse(progress.triggered)

    def test_hiding_an_unstarted_hud_does_not_spawn_a_process(self):
        hud = DesktopTakeoverHUD(enabled=True)
        with mock.patch.object(hud, "start") as start:
            hud.hide()

        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
