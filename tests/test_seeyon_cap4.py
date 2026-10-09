import unittest
from unittest.mock import patch

from agentbridge.adapters.seeyon_cap4 import wait_for_cap4_interactive


class Cap4InteractiveWaitTests(unittest.TestCase):
    def test_dismisses_message_and_waits_for_loading_overlay(self):
        page = FakePage()
        frame = FakeRoot(button=True, mask=True, loading=True)
        page.frame = frame

        wait_for_cap4_interactive(
            page,
            frame,
            timeout_seconds=1,
            settle_polls=1,
        )

        self.assertEqual(frame.click_count, 1)
        self.assertEqual(page.wait_count, 1)

    def test_raises_requested_contract_error_when_overlay_stays_visible(self):
        page = FakePage()
        frame = FakeRoot(mask=True)

        with (
            patch(
                "agentbridge.adapters.seeyon_cap4.time.monotonic",
                side_effect=[0, 0, 2],
            ),
            self.assertRaisesRegex(FakeContractError, "business-trip form"),
        ):
            wait_for_cap4_interactive(
                page,
                frame,
                error_type=FakeContractError,
                context="The OA business-trip form",
                timeout_seconds=1,
                settle_polls=1,
            )


class FakeContractError(RuntimeError):
    pass


class FakePage:
    def __init__(self):
        self.frame = None
        self.wait_count = 0

    def locator(self, selector):
        return FakeLocator(self, selector)

    def wait_for_timeout(self, _milliseconds):
        self.wait_count += 1
        if self.frame is not None:
            self.frame.loading = False


class FakeRoot:
    def __init__(self, *, button=False, mask=False, loading=False):
        self.button = button
        self.mask = mask
        self.loading = loading
        self.click_count = 0

    def locator(self, selector):
        return FakeLocator(self, selector)


class FakeLocator:
    def __init__(self, root, selector):
        self.root = root
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        if self.selector.endswith('ok_msg_btn_first"]:visible'):
            return int(getattr(self.root, "button", False))
        if self.selector == ".mask.mask_msg:visible":
            return int(getattr(self.root, "mask", False))
        if self.selector == ".cap4-loading:visible":
            return int(getattr(self.root, "loading", False))
        return 0

    def click(self, **_kwargs):
        self.root.click_count += 1
        self.root.button = False
        self.root.mask = False


if __name__ == "__main__":
    unittest.main()
