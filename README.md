# iPhone Charge Tray for Windows

Version 1.0.1 — fixes the battery command by omitting an invalid bare `--usbmux` flag.

A small Windows notification-area app that displays a USB-connected iPhone's battery percentage and charging-power reading.

## What appears in the tray

- **Green/amber/red number icon:** iPhone battery percentage.
- **Blue number icon:** charging watts when telemetry is available.
- Hover over either icon for a tooltip. Right-click either icon to open details, refresh, set Windows startup, or exit.
- The detail window reports how the wattage was obtained.

Windows tray icons are small, so keep both icons visible in the notification area if you want to see them at all times. Windows may put new icons in the overflow menu; drag them to the visible tray area from Taskbar settings or the overflow panel.

## Install

1. Extract this folder to a normal location, such as `C:\Users\YourName\Apps\iPhoneChargeTray`.
2. Install 64-bit Python 3.12 for Windows. This version is deliberate: the setup pins `pylzss==0.3.4`, which has a CPython 3.12 Windows wheel and matches pyimg4’s exact dependency requirement. Python 3.14 may try to compile a native extension and fail if its C headers/build environment are not available. You can keep Python 3.14 installed for other projects; this app uses its own `.venv`. Download Python 3.12 from https://www.python.org/downloads/release/python-31210/.
3. Double-click **`setup.bat`** and let it install the required packages. Internet access is needed for the initial setup.
4. Connect your iPhone with a data-capable USB cable, unlock it, and tap **Trust This Computer** on the phone if prompted.
5. Double-click **`start.bat`**. It runs quietly in the background; look in the Windows notification area near the clock.

Use `run_console.bat` if you need to see troubleshooting output. Logs are written to `%APPDATA%\iPhoneChargeTray\app.log`.

## Notes about charging watts

Apple does not provide one stable public Windows API for live charging watts across every iPhone model and iOS version. The app reads the iPhone's diagnostics through the open-source `pymobiledevice3` library.

- When explicit charging/input wattage is present, it uses that value.
- When input voltage/current is present, it estimates input power as volts × amps.
- Otherwise, if only battery voltage/current is available, it shows a **battery-side power estimate**. This is not the same as power drawn from the wall charger or USB port, because conversion losses and the phone's own power use are not included.
- If iOS does not expose usable current/voltage, the watt icon shows `--` and the details window explains that watts are unavailable. It never makes up a wattage number.
- iOS battery diagnostics can update more slowly than the tray refresh interval, so the number may not change every 15 seconds.

The app is intended for a trusted, USB-connected phone. Device visibility can depend on the Apple USB device driver being installed (Apple Devices or iTunes may provide it), the cable, the trust pairing, and iOS version.

## Troubleshooting

1. Unlock the phone, reconnect the cable, and accept **Trust This Computer** if asked.
2. Try another USB data cable/port. A charge-only cable will not work.
3. Install/update Apple's device support software if Windows does not recognize the phone.
4. Run `run_console.bat` and inspect `%APPDATA%\iPhoneChargeTray\app.log` for the error.
5. Right-click a tray icon and choose **Refresh now**.
6. If you previously installed the earlier package, extract this updated ZIP to a new folder (or replace the old `app.py`) and run `start.bat` again; you do not need to reinstall the Python packages for this fix.

## Development

- `app.py` — tray icons, dashboard, device diagnostics polling.
- `requirements.txt` — Python packages, including a pinned `pylzss==0.3.4` to match `pyimg4` and avoid a source build on Python 3.12.
- `setup.bat` / `start.bat` — setup and quiet launchers.

The current download is the source package, not a signed installer or prebuilt `.exe`.
