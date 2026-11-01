import json
import sys
import types
import unittest
from unittest import mock

# The parser tests do not need a native tray backend or a display. Stub what is missing.
try:
    import pystray  # noqa: F401
except ModuleNotFoundError:
    sys.modules["pystray"] = types.ModuleType("pystray")

try:
    import tkinter  # noqa: F401
except ModuleNotFoundError:
    _tk = types.ModuleType("tkinter")
    for _name in ("Tk", "Toplevel", "Canvas", "TclError"):
        setattr(_tk, _name, type(_name, (Exception,), {}))
    sys.modules["tkinter"] = _tk

try:
    from PIL import ImageTk  # noqa: F401
except Exception:
    sys.modules["PIL.ImageTk"] = types.ModuleType("PIL.ImageTk")

import app
import visuals
from app import (
    BatteryReading, build_battery_args, build_window_state, current_to_amps, discover_devices,
    extract_reading, voltage_to_volts,
)
from device_models import lookup_model


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


class ModelLookupTests(unittest.TestCase):
    def test_known_models(self):
        self.assertEqual(lookup_model("iPhone15,2").name, "iPhone 14 Pro")
        self.assertTrue(lookup_model("iPhone15,2").pro)
        self.assertEqual(lookup_model("iPhone17,5").name, "iPhone 16e")
        self.assertEqual(lookup_model("iPhone18,4").name, "iPhone Air")
        self.assertEqual(lookup_model("iPhone18,4").layout, "bar")
        self.assertEqual(lookup_model("iPhone12,8").layout, "corner")   # SE 2nd generation: one camera
        self.assertEqual(lookup_model("iPhone10,2").layout, "pill")     # 8 Plus: older dual camera
        self.assertEqual(lookup_model("iPhone16,1").lenses, 3)

    def test_unknown_identifier_is_not_invented(self):
        info = lookup_model("iPhone99,9")
        self.assertFalse(info.known)
        self.assertEqual(info.name, "iPhone")
        self.assertEqual(info.identifier, "iPhone99,9")

    def test_empty_identifier(self):
        self.assertFalse(lookup_model("").known)
        self.assertFalse(lookup_model(None).known)


class DiscoveryTests(unittest.TestCase):
    def _discover(self, payload):
        completed = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch.object(app, "run_cli", return_value=completed):
            return discover_devices()

    def test_model_and_ios_version_are_collected(self):
        devices = self._discover([{
            "Identifier": "00008110-001A2B3C4D5E801E",
            "ConnectionType": "USB",
            "DeviceName": "Alex's iPhone",
            "ProductType": "iPhone15,2",
            "ProductVersion": "17.5.1",
        }])
        self.assertEqual(devices, [{
            "udid": "00008110-001A2B3C4D5E801E",
            "name": "Alex's iPhone",
            "product_type": "iPhone15,2",
            "ios_version": "17.5.1",
        }])

    def test_no_devices(self):
        self.assertEqual(self._discover([]), [])

    def test_unrelated_records_are_ignored(self):
        self.assertIsNone(self._discover([{"Identifier": "com.example.app"}]))


class ExtraStatsTests(unittest.TestCase):
    def test_cycles_temperature_and_health(self):
        result = extract_reading({
            "CurrentCapacity": 80, "CycleCount": 412, "Temperature": 3120,
            "NominalChargeCapacity": 2950, "DesignCapacity": 3200,
        }, "Test")
        self.assertEqual(result.cycle_count, 412)
        self.assertAlmostEqual(result.temperature_c, 31.2)
        self.assertEqual(result.health_percent, 92)

    def test_implausible_values_are_dropped(self):
        result = extract_reading({
            "CurrentCapacity": 80, "CycleCount": 10 ** 7, "Temperature": 999999,
            "NominalChargeCapacity": 100, "DesignCapacity": 3200,
        }, "Test")
        self.assertIsNone(result.cycle_count)
        self.assertIsNone(result.temperature_c)
        self.assertIsNone(result.health_percent)

    def test_health_never_exceeds_100(self):
        result = extract_reading({"CurrentCapacity": 80, "NominalChargeCapacity": 3300, "DesignCapacity": 3200}, "Test")
        self.assertEqual(result.health_percent, 100)

    def test_missing_extras_stay_empty(self):
        result = extract_reading({"CurrentCapacity": 80}, "Test")
        self.assertIsNone(result.cycle_count)
        self.assertIsNone(result.temperature_c)
        self.assertIsNone(result.health_percent)


class WindowStateTests(unittest.TestCase):
    def test_connected_state(self):
        reading = extract_reading({
            "CurrentCapacity": 73, "IsCharging": True, "ExternalConnected": True,
            "Voltage": 3980, "InstantAmperage": 850, "CycleCount": 12,
        }, "Alex's iPhone")
        app.attach_model(reading, "iPhone15,2", "17.5.1")
        state = build_window_state(reading)
        self.assertTrue(state.connected)
        self.assertTrue(state.charging)
        self.assertEqual(state.percent, 73)
        self.assertEqual(state.model_name, "iPhone 14 Pro")
        self.assertEqual(state.owner_name, "Alex's iPhone")
        self.assertEqual(state.os_line, "iPhone15,2 on iOS 17.5.1")
        self.assertEqual(state.watts_text, "3.4 W")
        self.assertEqual(state.cycles_text, "12")
        self.assertEqual(state.health_text, "")   # not reported -> blank, never invented

    def test_disconnected_state(self):
        state = build_window_state(BatteryReading(connected=False, status="No iPhone detected"))
        self.assertFalse(state.connected)
        self.assertIsNone(state.model)
        self.assertEqual(state.model_name, "No iPhone connected")

    def test_tooltip_uses_model_name(self):
        reading = extract_reading({"CurrentCapacity": 50, "IsCharging": True, "ExternalConnected": True}, "x")
        app.attach_model(reading, "iPhone17,3", "")
        self.assertTrue(reading.tooltip().startswith("iPhone 16:"))


class RenderTests(unittest.TestCase):
    def test_window_renders_at_expected_size(self):
        for scale in (1.0, 1.5):
            state = build_window_state(extract_reading({"CurrentCapacity": 64, "IsCharging": True,
                                                        "ExternalConnected": True}, "x"))
            image, hit = visuals.render_window(state, scale)
            self.assertEqual(image.size, visuals.window_size(scale))
            self.assertIn("refresh", hit)

    def test_empty_state_renders(self):
        image, _ = visuals.render_window(visuals.WindowState(), 1.0)
        self.assertEqual(image.size, visuals.window_size(1.0))

    def test_ring_edge_values(self):
        for pct in (None, 0, 1, 50, 99, 100):
            ring = visuals.render_ring(pct, visuals.GREEN, 1.0)
            self.assertEqual(ring.width, ring.height)

    def test_every_icon_layout_renders(self):
        for ident in ("iPhone15,2", "iPhone10,2", "iPhone12,8", "iPhone18,4", "iPhone99,1"):
            icon = visuals.render_device_icon(lookup_model(ident), 1.0)
            self.assertEqual(icon.size, (visuals.ICON_W, visuals.ICON_H))


if __name__ == "__main__":
    unittest.main()
