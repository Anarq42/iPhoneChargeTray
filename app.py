from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pystray
from PIL import Image, ImageDraw, ImageFont
import tkinter as tk
from tkinter import ttk

APP_NAME = "iPhone Charge Tray"
APP_VERSION = "1.0.1"
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
        return f"iPhone: {pct} | {watts} | {status}"


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
        udid = ""
        name = ""
        for source in sources:
            for key in ("UDID", "udid", "SerialNumber", "serial_number", "serialNumber"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    udid = value.strip()
                    break
            for key in ("DeviceName", "device_name", "Name", "name", "ProductType"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
            if udid:
                break
        # A UDID is usually 25-40 characters; don't treat unrelated records as devices.
        if udid and udid not in seen and len(udid) >= 16:
            seen.add(udid)
            devices.append({"udid": udid, "name": name or "iPhone"})
    return devices if devices else None


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


def read_iphone() -> BatteryReading:
    devices = discover_devices()
    if devices == []:
        return BatteryReading(
            connected=False,
            status="No iPhone detected",
            error="Connect the iPhone by USB, unlock it, and tap Trust if prompted.",
        )
    # If this version of the CLI returns an unfamiliar device-list structure, try its default device.
    device = devices[0] if devices else {"udid": "", "name": "iPhone"}
    args = build_battery_args(device)
    try:
        result = run_cli(args, timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return BatteryReading(
            connected=True,
            device_name=device["name"],
            status="iPhone found; battery query timed out",
            error="The iPhone did not respond to the battery diagnostics request in time.",
        )
    except Exception as exc:
        logging.exception("Battery query failed")
        return BatteryReading(
            connected=True,
            device_name=device["name"],
            status="Battery query failed",
            error=str(exc),
        )

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
        return BatteryReading(
            connected=True,
            device_name=device["name"],
            status="Could not read battery telemetry",
            error=help_text,
        )

    reading = extract_reading(payload, device["name"])
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


class IPhoneChargeTray:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.root.resizable(False, False)
        self.window: tk.Toplevel | None = None
        self.latest = BatteryReading()
        self.updates: queue.Queue[BatteryReading] = queue.Queue()
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

    def _build_window(self) -> None:
        self.window = tk.Toplevel(self.root)
        self.window.title(APP_NAME)
        self.window.geometry("420x315")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.hide_window)

        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="iPhone Charge Tray", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(outer, text="USB battery status · updates about every 15 seconds", foreground="#666666").pack(anchor="w", pady=(2, 14))

        stats = ttk.Frame(outer)
        stats.pack(fill="x", pady=(0, 12))
        self.percent_var = tk.StringVar(value="--%")
        self.watts_var = tk.StringVar(value="—")
        self.status_var = tk.StringVar(value="Waiting for iPhone")
        self.device_var = tk.StringVar(value="Device: none")
        self.updated_var = tk.StringVar(value="Last update: —")
        ttk.Label(stats, text="Battery", font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w", padx=(0, 24))
        ttk.Label(stats, text="Charging power", font=("Segoe UI", 10)).grid(row=0, column=1, sticky="w")
        ttk.Label(stats, textvariable=self.percent_var, font=("Segoe UI", 25, "bold")).grid(row=1, column=0, sticky="w", padx=(0, 24))
        ttk.Label(stats, textvariable=self.watts_var, font=("Segoe UI", 25, "bold")).grid(row=1, column=1, sticky="w")
        ttk.Label(outer, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(2, 2))
        ttk.Label(outer, textvariable=self.device_var).pack(anchor="w")
        ttk.Label(outer, textvariable=self.updated_var, foreground="#666666").pack(anchor="w", pady=(2, 8))
        self.detail_var = tk.StringVar(value="Connect your iPhone via USB and tap Trust if prompted.")
        ttk.Label(outer, textvariable=self.detail_var, wraplength=375, justify="left").pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", side="bottom")
        ttk.Button(buttons, text="Refresh now", command=self.refresh_now).pack(side="left")
        ttk.Button(buttons, text="Hide", command=self.hide_window).pack(side="right")
        self.window.withdraw()

    def make_menu(self, icon_kind: str) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Open details", lambda icon, item: self.root.after(0, self.show_window), default=True),
            pystray.MenuItem("Refresh now", lambda icon, item: self.refresh_now()),
            pystray.MenuItem("Start with Windows", self.toggle_startup, checked=lambda item: is_startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda icon, item: self.root.after(0, self.quit)),
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
        pct_text = reading.tray_percent() if reading.connected else "--%"
        watts_text = f"{reading.watts:.1f} W" if reading.watts is not None else "—"
        self.percent_var.set(pct_text)
        self.watts_var.set(watts_text)
        self.status_var.set(reading.status)
        self.device_var.set(f"Device: {reading.device_name}" if reading.connected else "Device: none")
        self.updated_var.set(f"Last update: {reading.updated_at.strftime('%H:%M:%S') if reading.updated_at else '—'}")
        if reading.error:
            self.detail_var.set(reading.error)
        elif reading.watts_source:
            self.detail_var.set(f"Power source: {reading.watts_source}. iOS may refresh its battery telemetry only periodically.")
        elif reading.connected:
            self.detail_var.set("Battery percentage is available; charging watts were not exposed by this device's diagnostics.")
        else:
            self.detail_var.set("Connect your iPhone via USB, unlock it, and tap Trust This Computer if prompted.")

        p = reading.percent if reading.percent is not None else None
        percent_label = str(p) if p is not None else "--"
        self.percent_icon.icon = make_label_icon(percent_label, pct_color(p))
        self.percent_icon.title = reading.tooltip("battery")[:127]
        wlabel = watt_icon_label(reading.watts)
        wcolor = (56, 94, 160) if reading.watts is not None else (92, 98, 108)
        self.watts_icon.icon = make_label_icon(wlabel, wcolor)
        self.watts_icon.title = reading.tooltip("watts")[:127]

    def show_window(self) -> None:
        if self.window is not None:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()

    def hide_window(self) -> None:
        if self.window is not None:
            self.window.withdraw()

    def refresh_now(self) -> None:
        self.root.after(0, lambda: self.status_var.set("Refreshing battery data…"))
        self.refresh_event.set()

    def toggle_startup(self, icon=None, item=None) -> None:
        try:
            set_startup_enabled(not is_startup_enabled())
            logging.info("Start with Windows set to %s", is_startup_enabled())
        except Exception as exc:
            logging.exception("Could not change startup setting")
            self.root.after(0, lambda error=str(exc): self.detail_var.set(f"Could not change Windows startup setting: {error}"))

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
