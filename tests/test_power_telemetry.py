import os
import unittest
from unittest.mock import AsyncMock, patch

with patch.dict(os.environ, MELCLOUD_EMAIL="test@example.com", MELCLOUD_PASSWORD="test"):
    from app.melcloud import (
        DRIFT_STREAK_DEFAULT,
        DRIFT_STREAK_POWER_TELEMETRY,
        MelCloudBridge,
    )
    from app.models import Device, Setting


# Real settings captured from a unit that Loxone had switched off but which kept
# running: MELCloud reported Power=False while the fan was still turning.
COMMANDED_OFF_BUT_RUNNING = {
    "RoomTemperature": "23.5", "Power": "False", "OperationMode": "Automatic",
    "ActualFanSpeed": "Two", "SetFanSpeed": "Auto", "VaneHorizontalDirection": "Centre",
    "VaneVerticalDirection": "Auto", "InStandbyMode": "False", "SetTemperature": "20",
    "IsInError": "False", "ErrorCode": "",
}
GENUINELY_OFF = {
    "RoomTemperature": "21.0", "Power": "False", "OperationMode": "Heat",
    "ActualFanSpeed": "Off", "SetFanSpeed": "One", "VaneHorizontalDirection": "Centre",
    "VaneVerticalDirection": "Auto", "InStandbyMode": "False", "SetTemperature": "23",
    "IsInError": "False", "ErrorCode": "",
}
OFF_DESIRED = {"power": False, "temperature": 20.0, "mode": "auto",
               "fan_speed": "auto", "vane_vertical": "auto"}


def make_bridge(settings_dict, desired):
    with patch.object(MelCloudBridge, "_load_desired"):
        bridge = MelCloudBridge()
    bridge._devices = [Device(
        id="test-unit", device_type="ataunit", givenDisplayName="Test unit",
        settings=[Setting(name=k, value=v) for k, v in settings_dict.items()],
        isConnected=True, isInError=False,
    )]
    bridge._desired = {"test-unit": dict(desired)}
    bridge.set_device_state = AsyncMock(return_value={"status": "ok"})
    return bridge


class FanTelemetryTests(unittest.TestCase):
    def setUp(self):
        with patch.object(MelCloudBridge, "_load_desired"):
            self.bridge = MelCloudBridge()

    def test_fan_telemetry_reading(self):
        for reported, expected in [("Two", True), ("One", True), ("Off", False), ("", False)]:
            with self.subTest(reported=reported):
                self.assertIs(self.bridge._fan_is_running({"ActualFanSpeed": reported}), expected)

    def test_missing_telemetry_is_unknown_not_idle(self):
        # Devices that never report ActualFanSpeed (e.g. ATW) must be left alone.
        self.assertIsNone(self.bridge._fan_is_running({"Power": "False"}))

    def test_commanded_off_but_spinning_is_drift(self):
        self.assertEqual(
            self.bridge._drifted_fields(OFF_DESIRED, COMMANDED_OFF_BUT_RUNNING),
            {"power": False},
        )

    def test_genuinely_off_is_not_drift(self):
        self.assertNotIn("power", self.bridge._drifted_fields({"power": False}, GENUINELY_OFF))

    def test_no_telemetry_is_not_drift(self):
        self.assertEqual(self.bridge._drifted_fields({"power": False}, {"Power": "False"}), {})

    def test_existing_power_check_still_fires(self):
        self.assertEqual(self.bridge._drifted_fields({"power": True}, GENUINELY_OFF), {"power": True})

    def test_cloud_disagreement_is_drift(self):
        self.assertEqual(
            self.bridge._drifted_fields({"power": False}, {**GENUINELY_OFF, "Power": "True"}),
            {"power": False},
        )

    def test_telemetry_path_gets_the_longer_grace(self):
        self.assertEqual(
            self.bridge._required_streak("power", {"power": False}, COMMANDED_OFF_BUT_RUNNING),
            DRIFT_STREAK_POWER_TELEMETRY,
        )

    def test_cloud_disagreement_keeps_default_grace(self):
        self.assertEqual(
            self.bridge._required_streak("power", {"power": False}, {**GENUINELY_OFF, "Power": "True"}),
            DRIFT_STREAK_DEFAULT,
        )

    def test_other_fields_keep_default_grace(self):
        self.assertEqual(
            self.bridge._required_streak("fan_speed", {"fan_speed": "auto"}, COMMANDED_OFF_BUT_RUNNING),
            DRIFT_STREAK_DEFAULT,
        )


class ReconcileGraceWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_resent_only_after_the_long_grace_window(self):
        bridge = make_bridge(COMMANDED_OFF_BUT_RUNNING, OFF_DESIRED)
        writes = []
        for _ in range(10):
            await bridge._reconcile()
            writes.append(bridge.set_device_state.await_count)

        self.assertEqual(writes[:4], [0, 0, 0, 0], "must not act inside the grace window")
        self.assertEqual(writes[4], 1, "should re-assert on the fifth drifted poll")
        bridge.set_device_state.assert_awaited_with(device_id="test-unit", power=False)
        # Streak resets after a write, so the next retry is a full window later.
        self.assertEqual(writes[5:], [1, 1, 1, 1, 2])

    async def test_normal_field_keeps_two_poll_behaviour(self):
        bridge = make_bridge(GENUINELY_OFF, {"temperature": 19.0})
        writes = []
        for _ in range(3):
            await bridge._reconcile()
            writes.append(bridge.set_device_state.await_count)
        self.assertEqual(writes, [0, 1, 1])

    async def test_genuinely_off_device_is_never_touched(self):
        bridge = make_bridge(GENUINELY_OFF, {"power": False})
        for _ in range(6):
            await bridge._reconcile()
        bridge.set_device_state.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
