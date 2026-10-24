import os
import unittest
from unittest.mock import AsyncMock, patch

with patch.dict(os.environ, MELCLOUD_EMAIL="test@example.com", MELCLOUD_PASSWORD="test"):
    from app.main import control_fan, get_status_loxone
    from app.melcloud import MelCloudBridge
    from app.models import Device, Setting


class FanReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.object(MelCloudBridge, "_load_desired"):
            self.bridge = MelCloudBridge()
        self.device = Device(
            id="test-unit", givenDisplayName="Test unit", isConnected=True,
            isInError=False, settings=[Setting(name="SetFanSpeed", value="Four")],
        )
        self.bridge._devices = [self.device]

    async def test_reported_four_satisfies_commanded_speed_three(self):
        self.bridge._api_put = AsyncMock(return_value={"status": "ok"})
        with patch.object(self.bridge, "_save_desired"), patch("app.main.bridge", self.bridge):
            await control_fan(self.device.id, "5")
            status = await get_status_loxone(self.device.id, fresh=False)

        payload = self.bridge._api_put.call_args.args[1]
        self.assertEqual(payload["setFanSpeed"], 4)
        self.assertEqual(self.bridge.get_desired(self.device.id), {"fan_speed": "3"})
        self.assertEqual(status["set_fan_speed"], 5)
        self.bridge._api_put.reset_mock()

        for _ in range(4):
            await self.bridge._reconcile()
        self.bridge._api_put.assert_not_awaited()

    async def test_real_drift_corrected_once_and_stops_when_reported(self):
        self.bridge._desired = {self.device.id: {"fan_speed": "3"}}
        self.device.settings[0].value = "Two"
        self.bridge._api_put = AsyncMock(return_value={"status": "ok"})

        await self.bridge._reconcile()
        self.bridge._api_put.assert_not_awaited()
        with patch.object(self.bridge, "_save_desired"):
            await self.bridge._reconcile()
        self.bridge._api_put.assert_awaited_once()
        self.assertEqual(self.bridge._api_put.call_args.args[1]["setFanSpeed"], 4)

        self.device.settings[0].value = "Four"
        for _ in range(4):
            await self.bridge._reconcile()
        self.bridge._api_put.assert_awaited_once()

    async def test_fan_status_and_reconciliation_agree(self):
        for reported, canonical, loxone in [
            ("Auto", "auto", 1), ("Silent", "silent", 2),
            ("One", "silent", 2), ("Two", "1", 3),
            ("Three", "2", 4), ("Four", "3", 5), ("Five", "4", 6),
        ]:
            with self.subTest(reported=reported):
                self.assertEqual(self.bridge._drifted_fields(
                    {"fan_speed": canonical}, {"SetFanSpeed": reported},
                ), {})
                self.device.settings = [
                    Setting(name="SetFanSpeed", value=reported),
                    Setting(name="ActualFanSpeed", value=reported),
                ]
                with patch("app.main.bridge", self.bridge):
                    status = await get_status_loxone(self.device.id, fresh=False)
                self.assertEqual(status["set_fan_speed"], loxone)
                self.assertEqual(status["fan_speed"], loxone)
