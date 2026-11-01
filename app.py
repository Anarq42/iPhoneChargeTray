from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pystray
from PIL import Image, ImageDraw, ImageFont, ImageTk
import tkinter as tk

import visuals
from device_models import ModelInfo, lookup_model

APP_NAME = "iPhone Charge Tray"
APP_VERSION = "1.1.0"
POLL_SECONDS = 15
COMMAND_TIMEOUT_SECONDS = 12
REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REGISTRY_VALUE = "iPhoneChargeTray"

APPDATA = Path(os.environ.get("APPDATA", str(Path.home())))
LOG_DIR = APPDATA / "iPhoneChargeTray"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "app.log"
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)


@dataclass
class BatteryReading:
    connected: bool = False
    device_name: str = "iPhone"
    percent: int | None = None
    charging: bool | None = None
    external_power: bool | None = None
    watts: float | None = None
    watts_source: str = ""
    voltage_v: float | None = None
    current_a: float | None = None
    updated_at: datetime | None = None
    status: str = "Waiting for iPhone"
    error: str = ""
    product_type: str = ""
    model_name: str = ""
    ios_version: str = ""
    cycle_count: int | None = None
    temperature_c: float | None = None
    health_percent: int | None = None

    def tray_percent(self) -> str:
        return f"{self.percent}%" if self.percent is not None else "--%"

    def tray_watts(self) -> str:
        return f"{self.watts:.1f} W" if self.watts is not None else "Watts unavailable"

    def tooltip(self, which: str = "battery") -> str:
        pct = self.tray_percent()
        watts = self.tray_watts()
        if not self.connected:
            return "iPhone Charge Tray - no iPhone detected"
        if which == "watts":
            return f"iPhone charging power: {watts} ({self.watts_source or 'not reported'})"
        status = self.status
        return f"{self.model_name or 'iPhone'}: {pct} | {watts} | {status}"


# ---------- subprocess / JSON helpers ----------

def cli_prefix() -> list[str]:
    """Prefer the installed console script in this virtual environment."""
    scripts_dir = Path(sys.executable).resolve().parent
    for name in ("pymobiledevice3.exe", "pymobiledevice3"):
        candidate = scripts_dir / name
        if candidate.exists():
            return [str(candidate)]
    return [sys.executable, "-m", "pymobiledevice3"]


def run_cli(args: list[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    command = cli_prefix() + args
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    logging.debug("Running command: %s", " ".join(command))
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creationflags,
        check=False,
    )


def parse_json_output(text: str) -> Any | None:
    """Parse a JSON object/list, including if a CLI version adds leading text."""
    text = text.strip().lstrip("\ufeff")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Query output should be JSON; this fallback tolerates a warning before it.
    for start, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except json.JSONDecodeError:
            continue
    return None


def walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


_UDID_KEYS = ("UDID", "udid", "UniqueDeviceID", "Identifier", "SerialNumber", "serial_number", "serialNumber")
_NAME_KEYS = ("DeviceName", "device_name", "Name", "name")
_TYPE_KEYS = ("ProductType", "product_type", "productType")
_VERSION_KEYS = ("ProductVersion", "product_version", "productVersion")
_UDID_RE = re.compile(r"[0-9A-Fa-f-]{16,}")


def _first_str(sources: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def discover_devices() -> list[dict[str, str]] | None:
    """Return device records, [] when none are connected, or None if discovery output is unknown."""
    try:
        result = run_cli(["usbmux", "list"], timeout=8)
    except Exception as exc:
        logging.info("Device discovery failed: %s", exc)
        return None
    payload = parse_json_output(result.stdout)
    if result.returncode != 0 or payload is None:
        logging.info("Could not parse usbmux list JSON: %s", (result.stderr or result.stdout)[-500:])
        return None
    if payload == [] or payload == {} or payload is None:
        return []

    devices: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in walk_dicts(payload):
        props = item.get("Properties") if isinstance(item.get("Properties"), dict) else {}
        sources = [item, props]
        udid = _first_str(sources, _UDID_KEYS)
        # A UDID is hex digits and dashes, 16+ characters; don't treat unrelated records as devices.
        if udid and udid not in seen and _UDID_RE.fullmatch(udid):
            seen.add(udid)
            devices.append({
                "udid": udid,
                "name": _first_str(sources, _NAME_KEYS) or "iPhone",
                "product_type": _first_str(sources, _TYPE_KEYS),
                "ios_version": _first_str(sources, _VERSION_KEYS),
            })
    return devices if devices else None


_device_info_cache: dict[str, dict[str, str]] = {}
_device_info_retry_at: dict[str, float] = {}


def fetch_device_info(udid: str) -> dict[str, str]:
    """Ask the phone for its model identifier once (cached); used when usbmux list lacks it."""
    key = udid or "default"
    if key in _device_info_cache:
        return _device_info_cache[key]
    if time.monotonic() < _device_info_retry_at.get(key, 0.0):
        return {}
    args = ["lockdown", "info"] + (["--udid", udid] if udid else [])
    info: dict[str, str] = {}
    try:
        result = run_cli(args, timeout=10)
        payload = parse_json_output(result.stdout)
        if result.returncode == 0 and payload is not None:
            values = flatten_values(payload)
            for out_key, norm in (("product_type", "producttype"), ("ios_version", "productversion"), ("name", "devicename")):
                value = values.get(norm)
                if isinstance(value, str) and value.strip():
                    info[out_key] = value.strip()
        else:
            logging.info("lockdown info returned rc=%s: %s", result.returncode, (result.stderr or result.stdout)[-300:])
    except Exception as exc:
        logging.info("lockdown info failed: %s", exc)
    if info.get("product_type"):
        _device_info_cache[key] = info
    else:
        _device_info_retry_at[key] = time.monotonic() + 120  # don't retry on every poll
    return info


def attach_model(reading: BatteryReading, product_type: str, ios_version: str) -> None:
    reading.product_type = product_type or ""
    reading.ios_version = ios_version or ""
    reading.model_name = lookup_model(product_type).name if product_type else ""


def flatten_values(value: Any) -> dict[str, Any]:
    """Flatten nested diagnostic dictionaries by normalized key name."""
    result: dict[str, Any] = {}
    for item in walk_dicts(value):
        for key, val in item.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized and normalized not in result:
                result[normalized] = val
    return result


def first_value(values: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        norm = re.sub(r"[^a-z0-9]", "", key.lower())
        if norm in values and values[norm] is not None:
            return values[norm]
    return None


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value.replace(",", "."))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "charging", "connected", "on"}:
        return True
    if text in {"false", "no", "0", "not charging", "disconnected", "off"}:
        return False
    return None


def voltage_to_volts(value: Any) -> float | None:
    number = as_number(value)
    if number is None or number <= 0:
        return None
    # iOS diagnostic reports commonly use millivolts (e.g. 3980); some sources use V.
    return number / 1000.0 if number > 100 else number


def current_to_amps(value: Any) -> float | None:
    number = as_number(value)
    if number is None:
        return None
    # Common iOS Amperage / InstantAmperage fields are in mA.
    if abs(number) > 10000:
        number /= 1_000_000.0  # likely microamps
    else:
        number /= 1000.0
    return number


def extract_reading(payload: Any, device_name: str = "iPhone") -> BatteryReading:
    values = flatten_values(payload)
    reading = BatteryReading(connected=True, device_name=device_name, updated_at=datetime.now())

    pct = as_number(first_value(
        values,
        "CurrentCapacity", "BatteryCurrentCapacity", "BatteryLevel", "BatteryPercent",
        "ChargePercent", "CurrentCharge", "PercentCharged",
    ))
    if pct is not None and -0.1 <= pct <= 100.1:
        reading.percent = max(0, min(100, round(pct)))

    reading.charging = as_bool(first_value(values, "IsCharging", "Charging", "BatteryIsCharging"))
    reading.external_power = as_bool(first_value(
        values, "ExternalConnected", "ExternalPowerConnected", "IsExternalConnected",
        "PowerSourceConnected",
    ))

    # Prefer explicitly-reported input/charging wattage if a device provides it.
    direct_power = as_number(first_value(
        values, "InputPowerWatts", "ChargingPowerWatts", "ChargerPowerWatts",
        "USBPowerWatts", "InstantaneousPowerWatts", "WattageWatts",
    ))
    if direct_power is not None and 0 <= direct_power <= 100:
        reading.watts = direct_power
        reading.watts_source = "reported power"
    else:
        # First prefer charger/USB-side voltage and current when present.
        input_voltage_raw = first_value(values, "InputVoltage", "USBVoltage", "AdapterVoltage", "ChargerVoltage")
        input_current_raw = first_value(values, "InputCurrent", "USBCurrent", "AdapterCurrent", "ChargerCurrent")
        input_v = voltage_to_volts(input_voltage_raw)
        input_a = current_to_amps(input_current_raw)
        if input_v is not None and input_a is not None:
            watts = abs(input_v * input_a)
            if 0 <= watts <= 100:
                reading.watts = watts
                reading.watts_source = "estimated from USB/input voltage × current"
                reading.voltage_v, reading.current_a = input_v, input_a
        if reading.watts is None:
            # Battery voltage × battery current gives battery-side power, not charger input power.
            voltage_raw = first_value(values, "BatteryVoltage", "Voltage", "BatteryVoltageMV")
            current_raw = first_value(values, "InstantAmperage", "BatteryAmperage", "Amperage", "BatteryCurrent", "Current")
            voltage_v = voltage_to_volts(voltage_raw)
            current_a = current_to_amps(current_raw)
            if voltage_v is not None and current_a is not None:
                watts = abs(voltage_v * current_a)
                if 0 <= watts <= 100:
                    reading.watts = watts
                    reading.watts_source = "battery-side estimate (voltage × current)"
                    reading.voltage_v, reading.current_a = voltage_v, current_a

    # Optional extras. Only shown when the phone reports a plausible value.
    cycles = as_number(first_value(values, "CycleCount", "BatteryCycleCount"))
    if cycles is not None and 0 <= cycles <= 20000:
        reading.cycle_count = int(round(cycles))
    temp_raw = as_number(first_value(values, "Temperature", "BatteryTemperature"))
    if temp_raw is not None:
        temp = temp_raw / 100.0 if abs(temp_raw) > 150 else temp_raw  # iOS reports 0.01 degC units
        if -20 <= temp <= 90:
            reading.temperature_c = round(temp, 1)
    nominal = as_number(first_value(values, "NominalChargeCapacity", "AppleRawMaxCapacity"))
    design = as_number(first_value(values, "DesignCapacity"))
    if nominal and design and nominal > 500 and design > 500:  # both in mAh
        ratio = nominal / design * 100.0
        if 40 <= ratio <= 130:
            reading.health_percent = int(min(100, round(ratio)))

    if reading.external_power is True:
        reading.status = "Charging" if reading.charging is not False else "Connected, not charging"
    elif reading.charging is True:
        reading.status = "Charging"
    elif reading.external_power is False:
        reading.status = "Connected by USB, not drawing charge"
    else:
        reading.status = "Connected"

    # Some iOS reports may not include a separate IsCharging field.
    if reading.watts is not None and reading.external_power is not False and reading.charging is not False:
        reading.watts = round(reading.watts, 2)
    elif reading.charging is False:
        reading.watts = None
        reading.watts_source = ""
    return reading


def build_battery_args(device: dict[str, str]) -> list[str]:
    """Build diagnostics arguments using pymobiledevice3's platform default usbmux.

    --usbmux is an address option (HOST:PORT or socket path), not a boolean flag.
    Leaving it out lets pymobiledevice3 select the platform default usbmux daemon.
    """
    args = ["diagnostics", "battery", "single"]
    if device.get("udid"):
        args.extend(["--udid", device["udid"]])
    return args


_logged_fields = False


def read_iphone() -> BatteryReading:
    global _logged_fields
    devices = discover_devices()
    if devices == []:
        return BatteryReading(
            connected=False,
            status="No iPhone detected",
            error="Connect the iPhone by USB, unlock it, and tap Trust if prompted.",
        )
    # If this version of the CLI returns an unfamiliar device-list structure, try its default device.
    device = devices[0] if devices else {"udid": "", "name": "iPhone"}
    known_type = device.get("product_type", "")
    known_ios = device.get("ios_version", "")
    args = build_battery_args(device)

    def connected_reading(**kwargs: Any) -> BatteryReading:
        reading = BatteryReading(connected=True, device_name=device["name"], updated_at=datetime.now(), **kwargs)
        attach_model(reading, known_type, known_ios)
        return reading

    try:
        result = run_cli(args, timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return connected_reading(
            status="iPhone found; battery query timed out",
            error="The iPhone did not respond to the battery diagnostics request in time.",
        )
    except Exception as exc:
        logging.exception("Battery query failed")
        return connected_reading(status="Battery query failed", error=str(exc))

    payload = parse_json_output(result.stdout)
    if result.returncode != 0 or payload is None:
        message = (result.stderr or result.stdout or "No diagnostic data returned").strip()
        logging.info("Battery query returned rc=%s: %s", result.returncode, message[-1000:])
        if "trust" in message.lower() or "pair" in message.lower():
            help_text = "Unlock your iPhone and accept the Trust This Computer prompt, then refresh."
        else:
            help_text = message[-500:] or "Check the Apple USB driver and try reconnecting the cable."
        if not device["udid"] and ("no device" in message.lower() or "not found" in message.lower()):
            return BatteryReading(
                connected=False,
                status="No iPhone detected",
                error="Connect the iPhone by USB, unlock it, and tap Trust if prompted.",
            )
        return connected_reading(status="Could not read battery telemetry", error=help_text)

    reading = extract_reading(payload, device["name"])

    # Work out the exact model. usbmux list usually has it; otherwise ask the phone once.
    if not known_type:
        fetched = fetch_device_info(device["udid"])
        known_type = fetched.get("product_type", "")
        known_ios = known_ios or fetched.get("ios_version", "")
        if device["name"] == "iPhone" and fetched.get("name"):
            reading.device_name = fetched["name"]
    attach_model(reading, known_type, known_ios)

    if not _logged_fields:
        _logged_fields = True
        logging.info("Battery fields reported by this iPhone: %s", ", ".join(sorted(flatten_values(payload)))[:3000])
    if reading.percent is None:
        reading.status = "Connected; battery fields unavailable"
        reading.error = "The diagnostics response did not contain a recognized battery percentage field."
        logging.info("Unknown battery payload: %s", json.dumps(payload, ensure_ascii=False)[:2000])
    return reading


# ---------- Windows tray icons ----------

def _font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_label_icon(label: str, background: tuple[int, int, int], foreground=(255, 255, 255)) -> Image.Image:
    """Render a simple bold label which remains legible when Windows scales it to tray size."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 61, 61), radius=13, fill=(*background, 255), outline=(255, 255, 255, 220), width=2)
    # Fit font to the label width.
    size = 37 if len(label) <= 2 else (29 if len(label) == 3 else 23)
    font = _font(size)
    while size > 15 and draw.textbbox((0, 0), label, font=font, stroke_width=0)[2] > 55:
        size -= 2
        font = _font(size)
    bbox = draw.textbbox((0, 0), label, font=font, stroke_width=0)
    x = (64 - (bbox[2] - bbox[0])) / 2 - bbox[0]
    y = (64 - (bbox[3] - bbox[1])) / 2 - bbox[1] - 1
    draw.text((x, y), label, font=font, fill=(*foreground, 255), stroke_width=0)
    return image


def pct_color(percent: int | None) -> tuple[int, int, int]:
    if percent is None:
        return (92, 98, 108)
    if percent <= 15:
        return (205, 55, 55)
    if percent <= 35:
        return (218, 143, 35)
    return (38, 145, 88)


def watt_icon_label(watts: float | None) -> str:
    if watts is None:
        return "--"
    # The tray icon is tiny; show rounded watts in the icon and precise watts in its tooltip.
    return f"{round(watts)}W"


def is_startup_enabled() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, REGISTRY_VALUE)
            return bool(value)
    except (OSError, ImportError):
        return False


def set_startup_enabled(enabled: bool) -> None:
    if os.name != "nt":
        raise OSError("Windows startup settings are only available on Windows.")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            executable = Path(sys.executable)
            # If started with python.exe, use pythonw.exe so a console does not appear at login.
            if executable.name.lower() == "python.exe":
                pythonw = executable.with_name("pythonw.exe")
                if pythonw.exists():
                    executable = pythonw
            app_path = Path(__file__).resolve()
            command = f'"{executable}" "{app_path}"'
            winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, REGISTRY_VALUE)
            except FileNotFoundError:
                pass


def enable_dpi_awareness() -> None:
    """Ask Windows for crisp rendering on scaled displays instead of a blurry stretched window."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        logging.info("Could not enable DPI awareness", exc_info=True)


def compute_ui_scale(root: tk.Tk) -> float:
    """Pixels per design pixel: follows Windows scaling but never makes the window taller than the screen."""
    try:
        dpi_scale = max(1.0, min(root.winfo_fpixels("1i") / 96.0, 3.0))
        fit = (root.winfo_screenheight() - 110 * dpi_scale) / visuals.H
    except tk.TclError:
        return 1.0
    return round(max(0.75, min(dpi_scale, fit)), 3)


def build_window_state(reading: BatteryReading, refreshing: bool = False) -> visuals.WindowState:
    """Turn a BatteryReading into the plain values the window draws."""
    if not reading.connected:
        return visuals.WindowState(
            connected=False,
            status=reading.status or "No iPhone detected",
            detail=reading.error or "Connect your iPhone via USB, unlock it, and tap Trust This Computer if prompted.",
            model=None,
            model_name="No iPhone connected",
            os_line="Connect with a USB data cable",
            updated_text=f"Updated {reading.updated_at:%H:%M:%S}" if reading.updated_at else "",
            refreshing=refreshing,
        )

    model: ModelInfo = lookup_model(reading.product_type)
    model_name = reading.model_name or (model.name if reading.product_type else "iPhone")
    owner = reading.device_name if reading.device_name not in ("", "iPhone", model_name, reading.product_type) else ""

    if reading.product_type and reading.ios_version:
        os_line = f"{reading.product_type} on iOS {reading.ios_version}"
    elif reading.product_type:
        os_line = reading.product_type
    elif reading.ios_version:
        os_line = f"iOS {reading.ios_version}"
    else:
        os_line = ""

    if reading.error:
        detail = reading.error
    elif reading.watts_source:
        detail = f"Power source: {reading.watts_source}. iOS may refresh its battery telemetry only periodically."
    else:
        detail = "Battery percentage is available; charging watts were not exposed by this device's diagnostics."

    return visuals.WindowState(
        connected=True,
        percent=reading.percent,
        charging=reading.status == "Charging",
        status=reading.status,
        watts_text=f"{reading.watts:.1f} W" if reading.watts is not None else "",
        health_text=f"{reading.health_percent}%" if reading.health_percent is not None else "",
        cycles_text=f"{reading.cycle_count:,}" if reading.cycle_count is not None else "",
        temp_text=f"{reading.temperature_c:.1f} °C" if reading.temperature_c is not None else "",
        detail=detail,
        model=model,
        model_name=model_name,
        owner_name=owner,
        os_line=os_line,
        updated_text=f"Updated {reading.updated_at:%H:%M:%S}" if reading.updated_at else "",
        refreshing=refreshing,
    )


class IPhoneChargeTray:
    def __init__(self) -> None:
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.root.resizable(False, False)
        self.scale = compute_ui_scale(self.root)
        self.window: tk.Toplevel | None = None
        self.canvas: tk.Canvas | None = None
        self._canvas_image: int | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._icon_photo: ImageTk.PhotoImage | None = None
        self._hit: dict[str, tuple[int, int, int, int]] = {}
        self._cursor = "arrow"
        self._last_key: Any = None
        self._titlebar_styled = False
        self.refreshing = False
        self.latest = BatteryReading()
        self.updates: queue.Queue[BatteryReading] = queue.Queue()
        self.ui_calls: queue.Queue[Any] = queue.Queue()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.refresh_event.set()
        self.worker = threading.Thread(target=self.poll_worker, name="iPhoneBatteryPoller", daemon=True)
        self._build_window()

        self.percent_icon = pystray.Icon(
            "iPhoneBatteryPercent",
            make_label_icon("--", (92, 98, 108)),
            "iPhone battery: not detected",
            menu=self.make_menu("battery"),
        )
        self.watts_icon = pystray.Icon(
            "iPhoneChargingWatts",
            make_label_icon("--", (56, 94, 160)),
            "iPhone charging watts: unavailable",
            menu=self.make_menu("watts"),
        )

    # ----- thread hand-off: tray menu callbacks run on pystray's thread, Tk must stay on the main thread
    def post_ui(self, call) -> None:
        self.ui_calls.put(call)

    # ----- window
    def _build_window(self) -> None:
        width, height = visuals.window_size(self.scale)
        self.window = tk.Toplevel(self.root)
        self.window.title(APP_NAME)
        self.window.geometry(f"{width}x{height}")
        self.window.resizable(False, False)
        self.window.configure(bg=visuals.BG_HEX)
        self.window.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.window.bind("<Escape>", lambda event: self.hide_window())
        self.window.bind("<F5>", lambda event: self.refresh_now())
        self.window.bind("<Control-r>", lambda event: self.refresh_now())

        self.canvas = tk.Canvas(self.window, width=width, height=height, bg=visuals.BG_HEX,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self._canvas_image = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Motion>", self.on_motion)

        try:
            self._icon_photo = ImageTk.PhotoImage(visuals.render_app_icon(64))
            self.window.iconphoto(False, self._icon_photo)
        except Exception:
            logging.info("Could not set window icon", exc_info=True)

        self.redraw()
        self.style_titlebar()
        self.window.withdraw()

    def style_titlebar(self) -> None:
        """Dark title bar on Windows 10/11 so the window looks like one piece. Silently skipped if unsupported."""
        if os.name != "nt" or self.window is None or self._titlebar_styled:
            return
        try:
            import ctypes
            self.window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.window.winfo_id())
            dwm = ctypes.windll.dwmapi

            def set_attr(attribute: int, value: int) -> int:
                data = ctypes.c_int(value)
                return dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))

            def colorref(rgb: tuple[int, int, int]) -> int:
                return rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)

            if set_attr(20, 1) != 0:   # DWMWA_USE_IMMERSIVE_DARK_MODE (older builds use 19)
                set_attr(19, 1)
            set_attr(35, colorref(visuals.BG))     # caption colour (Windows 11)
            set_attr(36, colorref(visuals.TEXT))   # caption text colour (Windows 11)
            self._titlebar_styled = True
        except Exception:
            logging.info("Could not style the title bar", exc_info=True)

    def redraw(self) -> None:
        if self.canvas is None or self._canvas_image is None:
            return
        state = build_window_state(self.latest, self.refreshing)
        key = (state, self.scale)
        if key == self._last_key:
            return
        try:
            image, hit = visuals.render_window(state, self.scale)
            self._photo = ImageTk.PhotoImage(image)
        except Exception:
            logging.exception("Could not draw the details window")
            return
        self.canvas.itemconfigure(self._canvas_image, image=self._photo)
        self._hit = hit
        self._last_key = key

    def _over_button(self, x: int, y: int) -> str | None:
        for name, (x0, y0, x1, y1) in self._hit.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return None

    def on_click(self, event) -> None:
        if self._over_button(event.x, event.y) == "refresh":
            self.refresh_now()

    def on_motion(self, event) -> None:
        cursor = "hand2" if self._over_button(event.x, event.y) else "arrow"
        if cursor != self._cursor and self.canvas is not None:
            self.canvas.configure(cursor=cursor)
            self._cursor = cursor

    def make_menu(self, icon_kind: str) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Open details", lambda icon, item: self.post_ui(self.show_window), default=True),
            pystray.MenuItem("Refresh now", lambda icon, item: self.refresh_now()),
            pystray.MenuItem("Start with Windows", self.toggle_startup, checked=lambda item: is_startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda icon, item: self.post_ui(self.quit)),
        )

    def poll_worker(self) -> None:
        while not self.stop_event.is_set():
            self.refresh_event.wait(POLL_SECONDS)
            self.refresh_event.clear()
            if self.stop_event.is_set():
                break
            try:
                reading = read_iphone()
            except Exception as exc:
                logging.exception("Unexpected update failure")
                reading = BatteryReading(status="Update failed", error=str(exc))
            self.updates.put(reading)
            # If the poll was triggered manually, do the next normal poll in 15 seconds.

    def drain_updates(self) -> None:
        while True:
            try:
                call = self.ui_calls.get_nowait()
            except queue.Empty:
                break
            try:
                call()
            except Exception:
                logging.exception("UI action failed")
        if self.stop_event.is_set():
            return
        newest: BatteryReading | None = None
        while True:
            try:
                newest = self.updates.get_nowait()
            except queue.Empty:
                break
        if newest is not None:
            self.latest = newest
            self.apply_reading(newest)
        self.root.after(250, self.drain_updates)

    def apply_reading(self, reading: BatteryReading) -> None:
        self.refreshing = False
        self.redraw()

        p = reading.percent if reading.percent is not None else None
        percent_label = str(p) if p is not None else "--"
        self.percent_icon.icon = make_label_icon(percent_label, pct_color(p))
        self.percent_icon.title = reading.tooltip("battery")[:127]
        wlabel = watt_icon_label(reading.watts)
        wcolor = (56, 94, 160) if reading.watts is not None else (92, 98, 108)
        self.watts_icon.icon = make_label_icon(wlabel, wcolor)
        self.watts_icon.title = reading.tooltip("watts")[:127]

    def show_window(self) -> None:
        if self.window is None:
            return
        if self.window.state() != "normal":
            width, height = visuals.window_size(self.scale)
            x = max(0, self.window.winfo_screenwidth() - width - int(16 * self.scale))
            y = max(0, self.window.winfo_screenheight() - height - int(100 * self.scale))
            self.window.geometry(f"+{x}+{y}")  # open near the tray, like a flyout
        self.style_titlebar()
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def hide_window(self) -> None:
        if self.window is not None:
            self.window.withdraw()

    def _show_refreshing(self) -> None:
        self.refreshing = True
        self.redraw()

    def refresh_now(self) -> None:
        self.post_ui(self._show_refreshing)
        self.refresh_event.set()

    def toggle_startup(self, icon=None, item=None) -> None:
        try:
            set_startup_enabled(not is_startup_enabled())
            logging.info("Start with Windows set to %s", is_startup_enabled())
        except Exception as exc:
            logging.exception("Could not change startup setting")
            error = str(exc)

            def show_error() -> None:
                self.latest.error = f"Could not change Windows startup setting: {error}"
                self._last_key = None
                self.redraw()
            self.post_ui(show_error)

    def quit(self) -> None:
        self.stop_event.set()
        self.refresh_event.set()
        for icon in (self.percent_icon, self.watts_icon):
            try:
                icon.stop()
            except Exception:
                pass
        if self.window is not None:
            self.window.destroy()
        self.root.quit()

    def run(self) -> None:
        self.percent_icon.run_detached()
        self.watts_icon.run_detached()
        self.worker.start()
        self.root.after(250, self.drain_updates)
        self.root.mainloop()
        self.stop_event.set()


def main() -> None:
    logging.info("Starting %s %s", APP_NAME, APP_VERSION)
    if os.name != "nt":
        print("This application is intended for Windows.")
    app = IPhoneChargeTray()
    app.run()


if __name__ == "__main__":
    main()
