import sys
import types
import unittest

# The parser tests do not need a native tray backend. Stub it when pystray has not been installed yet.
try:
    import pystray  # noqa: F401
except ModuleNotFoundError:
    sys.modules["pystray"] = types.ModuleType("pystray")

from app import extract_reading, current_to_amps, voltage_to_volts, build_battery_args


class BatteryParserTests(unittest.TestCase):
    def test_battery_command_does_not_pass_usbmux_as_bare_flag(self):
        args = build_battery_args({"udid": "device-udid", "name": "iPhone"})
        self.assertEqual(args, ["diagnostics", "battery", "single", "--udid", "device-udid"])
        self.assertNotIn("--usbmux", args)

    def test_battery_command_uses_default_device_when_udid_missing(self):
        args = build_battery_args({"udid": "", "name": "iPhone"})
        self.assertEqual(args, ["diagnostics", "battery", "single"])

    def test_percent_and_battery_power_estimate(self):
        sample = {
            "CurrentCapacity": 73,
            "IsCharging": True,
            "ExternalConnected": True,
            "Voltage": 3980,
            "InstantAmperage": 850,
        }
        result = extract_reading(sample, "Test iPhone")
        self.assertEqual(result.percent, 73)
        self.assertAlmostEqual(result.watts, 3.383, places=2)
        self.assertIn("battery-side", result.watts_source)
        self.assertEqual(result.status, "Charging")

    def test_watts_unavailable_when_current_missing(self):
        result = extract_reading({"CurrentCapacity": 99, "IsCharging": True}, "Test")
        self.assertEqual(result.percent, 99)
        self.assertIsNone(result.watts)

    def test_explicit_input_power_takes_priority(self):
        result = extract_reading({
            "CurrentCapacity": 50,
            "IsCharging": True,
            "ExternalConnected": True,
            "ChargingPowerWatts": 8.2,
        }, "Test")
        self.assertAlmostEqual(result.watts, 8.2)
        self.assertEqual(result.watts_source, "reported power")

    def test_units(self):
        self.assertAlmostEqual(voltage_to_volts(4000), 4.0)
        self.assertAlmostEqual(voltage_to_volts(9.0), 9.0)
        self.assertAlmostEqual(current_to_amps(950), 0.95)
        self.assertAlmostEqual(current_to_amps(250000), 0.25)

    def test_no_fake_power_if_not_charging(self):
        result = extract_reading({
            "CurrentCapacity": 100,
            "IsCharging": False,
            "ExternalConnected": True,
            "Voltage": 4000,
            "Amperage": 250,
        }, "Test")
        self.assertIsNone(result.watts)
        self.assertEqual(result.status, "Connected, not charging")


if __name__ == "__main__":
    unittest.main()
