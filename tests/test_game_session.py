import unittest
from collections import deque

import numpy as np
from PIL import Image

from game_session import (
    AGREEMENT_CHECKBOX_POINT,
    GamePeriodReader,
    GameSessionManager,
    SessionSnapshot,
    _ZI_GLYPH_ROWS,
)
from smart_automation import TextObservation


def observation(text, x=0.4, top=0.4, width=0.2, height=0.03):
    return TextObservation(
        text=text,
        confidence=1.0,
        x=x,
        y=1.0 - top - height,
        width=width,
        height=height,
    )


class FakeStateReader:
    def __init__(self, observations):
        self._observations = list(observations)

    def observations(self):
        return list(self._observations)


class FakeAutomation:
    reference_width = 2102
    reference_height = 1632

    def __init__(self, image=None):
        self.image = image
        self.clicked = []

    def capture_image(self):
        return self.image

    def click_reference(self, point):
        self.clicked.append(tuple(point))

    def focus_window(self, **_kwargs):
        return True

    def capture_screenshot(self, *_args, **_kwargs):
        return None


class SequenceSessionManager(GameSessionManager):
    def __init__(self, snapshots, agreement_checked=False):
        super().__init__(FakeAutomation(), state_reader=FakeStateReader([]))
        self.snapshots = deque(snapshots)
        self.actions = []
        self._agreement_checked = agreement_checked

    def detect(self):
        return self.snapshots.popleft()

    def agreement_checked(self):
        return self._agreement_checked

    def _click_text(self, text, **_kwargs):
        self.actions.append(("text", text))

    def _click_point(self, point):
        self.actions.append(("point", tuple(point)))

    def _sleep(self, _seconds):
        return None


class GameSessionTests(unittest.TestCase):
    def test_detects_login_provider_and_server_download_without_exposing_account_text(self):
        provider = GameSessionManager(
            FakeAutomation(),
            state_reader=FakeStateReader(
                [
                    observation("⑨账号登录"),
                    observation("QQ登录"),
                    observation("通过 Apple 登录"),
                ]
            ),
        ).detect()
        download = GameSessionManager(
            FakeAutomation(),
            state_reader=FakeStateReader(
                [
                    observation("服务器存档：沉淀［100级］"),
                    observation("其他设备上传过更新版本存档"),
                    observation("即将从服务器强制下载存档"),
                ]
            ),
        ).detect()

        self.assertEqual(provider.state, "login_provider")
        self.assertEqual(download.state, "server_download_prompt")
        self.assertEqual(download.marker, "server_save_newer")

    def test_detects_remote_logout_variants(self):
        snapshot = GameSessionManager(
            FakeAutomation(),
            state_reader=FakeStateReader(
                [observation("当前账号已在其他设备登录，请重新登录")]
            ),
        ).detect()

        self.assertEqual(snapshot.state, "remote_logout")

    def test_recovery_uses_the_verified_login_chain_and_only_confirms_server_download(self):
        manager = SequenceSessionManager(
            [
                SessionSnapshot("login_provider"),
                SessionSnapshot("account_form"),
                SessionSnapshot("announcement"),
                SessionSnapshot("game_home"),
                SessionSnapshot("server_download_prompt"),
                SessionSnapshot("in_game", "天顶峰", (25, 26)),
            ]
        )

        result = manager.recover_latest_server_save()

        self.assertEqual(result["map"], "天顶峰")
        self.assertEqual(
            manager.actions,
            [
                ("text", "账号登录"),
                ("text", "登录"),
                ("point", (1783, 455)),
                ("point", AGREEMENT_CHECKBOX_POINT),
                ("text", "开始游戏"),
                ("text", "确认"),
            ],
        )

    def test_agreement_checkbox_is_detected_from_the_orange_check_mark(self):
        image = np.zeros((1714, 2210, 3), dtype=np.uint8)
        image[1450:1490, 500:550] = (210, 95, 25)
        manager = GameSessionManager(
            FakeAutomation(Image.fromarray(image, mode="RGB")),
            state_reader=FakeStateReader([]),
        )

        self.assertTrue(manager.agreement_checked())

    def test_period_reader_accepts_the_zi_glyph_and_rejects_daylight_without_it(self):
        rgb = np.zeros((1714, 2210, 3), dtype=np.uint8)
        glyph = np.array([[value == "#" for value in row] for row in _ZI_GLYPH_ROWS])
        y, x = 522, 2051
        rgb[y : y + glyph.shape[0], x : x + glyph.shape[1]][glyph] = (40, 170, 205)
        reader = GamePeriodReader(FakeAutomation(Image.fromarray(rgb, mode="RGB")))

        period = reader.read()
        daylight = GamePeriodReader(
            FakeAutomation(Image.fromarray(np.zeros_like(rgb), mode="RGB"))
        ).read()

        self.assertEqual(period.period, "子")
        self.assertGreater(period.similarity, 0.95)
        self.assertIsNone(daylight.period)


if __name__ == "__main__":
    unittest.main()
