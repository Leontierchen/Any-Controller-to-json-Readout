# 🎮 Universal Multi-Controller JSON Bridge

[![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%2F%2011-0078D6?logo=windows&logoColor=white)](https://github.com/Leontierchen/Any-Controller-to-json-Readout)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![UI](https://img.shields.io/badge/GUI-PyQt6-41CD52?logo=qt&logoColor=white)](https://pypi.org/project/PyQt6/)
[![SDL_GameControllerDB](https://img.shields.io/badge/Mappings-SDL__GameControllerDB-00D2FF)](https://github.com/mdqinc/SDL_GameControllerDB)
[![Releases](https://img.shields.io/github/v/release/Leontierchen/Any-Controller-to-json-Readout?color=00a8cc&label=Release)](https://github.com/Leontierchen/Any-Controller-to-json-Readout/releases)

A lightweight, dark-mode Windows desktop utility that reads live inputs from virtually **any connected controller, joystick, 6-DOF SpaceMouse, or simulation panel** simultaneously and streams normalized, low-latency state data into a clean JSON file (`controller_data.json`).

Built for game mods, external telemetry scripts, custom robotics/CAD integrations, and any application that needs unified controller input without dealing with low-level HID or SDL APIs.

---

## ✨ Key Features

* **Multi-Device Simultaneous Readout & Hot-Plug Support**
  * Reads multiple controllers at the same time and automatically detects newly connected or disconnected devices every 2 seconds.
  * Per-device **Active / Ignored** toggle to exclude unwanted virtual or secondary devices from the JSON output.
* **Dedicated Raw HID Support for Specialized Hardware**
  * **3Dconnexion SpaceMouse:** Native 6-DOF readout (`x`, `y`, `z`, `pitch`, `roll`, `yaw`) plus buttons via `hidapi`—bypassing standard Windows joystick axis limitations.
  * **Logitech / Saitek Heavy Equipment Side Panel:** Full native support for all **28 buttons** and **6 axes** (covering both Red and Blue LED stick modes).
  * **Standard Gamepads, HOTAS & Wheels:** Broad support via SDL2 / `pygame` (Xbox, PlayStation, generic DirectInput/XInput devices, flight sticks, pedals).
* **Automatic Community Controller Mapping (`SDL_GameControllerDB`)**
  * Automatically syncs with the community-maintained [mdqinc/SDL_GameControllerDB](https://github.com/mdqinc/SDL_GameControllerDB) repository using HTTP `ETag` caching (`If-None-Match`) and local offline fallback (`gamecontrollerdb.txt`).
  * Automatically translates raw button/axis indices into standardized names (`a`, `b`, `leftx`, `lefttrigger`, `dpup`, etc.).
* **Interactive Calibration & Custom Remapping Wizard**
  * Automatic rest-position (baseline) calibration to eliminate hardware offset drift.
  * Smart detection of **digital buttons**, **2-way centered axes**, **1-way axes** (pedals/triggers), and **multi-step analog switches** mapped to virtual buttons at specific target values.
  * Rename any axis or button to custom JSON keys tailored to your target application.
* **Per-Controller Deadzone & Live Input Visualizer**
  * Individual deadzone slider (`0.00` – `0.50`) per device with smooth linear rescaling outside the deadzone.
  * Real-time bipolar axis bars (`-1.00` to `+1.00`) and live button indicators for instant visual verification.
* **High-Speed, Safe JSON Output**
  * Configurable output file path and write interval (`0.01s` – `5.00s`, default `0.045s`).
  * Writes only when input states change, using in-place file truncation to minimize disk wear and avoid empty-file race conditions for external readers.
* **9 Languages & Built-in Auto-Updater**
  * Automatic system language detection with instant switching between **English**, **Deutsch**, **Русский**, **日本語**, **中文 (简体)**, **Українська**, **Français**, **Español**, and **Italiano**.
  * Built-in GitHub release checker with one-click in-app updates for standalone `.exe` builds.

---

## 📄 JSON Output Format

The bridge writes a compact JSON structure to `controller_data.json` (or your chosen output path):

```json
{
  "controller_count": 2,
  "controllers": {
    "joy_0": {
      "id": "joy_0",
      "name": "Xbox One Controller",
      "axes": {
        "leftx": 0.0,
        "lefty": -0.4521,
        "rightx": 0.0,
        "righty": 0.0,
        "lefttrigger": 0.0,
        "righttrigger": 0.8125
      },
      "buttons": {
        "a": 1,
        "b": 0,
        "x": 0,
        "y": 0,
        "leftshoulder": 0,
        "rightshoulder": 1
      }
    },
    "spacemouse_0": {
      "id": "spacemouse_0",
      "name": "3Dconnexion SpaceMouse Compact",
      "axes": {
        "x": 0.1429,
        "y": 0.0,
        "z": -0.3105,
        "pitch": 0.0,
        "roll": 0.0,
        "yaw": 0.2218
      },
      "buttons": {
        "btn_1": 0,
        "btn_2": 1
      }
    }
  }
}
```

---

## 🚀 Getting Started

### Option 1: Standalone Windows Executable (Recommended)
1. Download the latest release from the [**Releases Page**](https://github.com/Leontierchen/Any-Controller-to-json-Readout/releases).
2. Extract the archive into a folder of your choice.
3. Run the executable—`controller_data.json` and `bridge_config.json` will be created automatically in the same directory.

### Option 2: Run from Source (Python 3.11+)

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/Leontierchen/Any-Controller-to-json-Readout.git](https://github.com/Leontierchen/Any-Controller-to-json-Readout.git)
   cd Any-Controller-to-json-Readout
   ```

2. **Install dependencies:**
   ```bash
   pip install PyQt6 pygame hidapi
   ```

3. **Launch the application:**
   ```bash
   python code.pyw
   ```

---

## ⚙️ Configuration & Calibration

All settings are automatically persisted in `bridge_config.json`:

| Setting | Description |
| :--- | :--- |
| **Output JSON** | Target file path for the live JSON stream (defaults to `./controller_data.json`). |
| **JSON Write Interval** | Minimum time in seconds between file writes (`0.01s` to `5.00s`). |
| **Per-Device Deadzone** | Individual deadzone (`0.00` – `0.50`) stored per controller model. |
| **Active / Ignored Toggle** | Controls whether a detected controller is included in `controller_data.json`. |
| **⚙ Calibration** | Opens the interactive wizard to capture rest positions, map axes/buttons, and assign custom JSON key names. |

---

## 🙏 Credits & Acknowledgments

* Controller auto-mapping database powered by **[mdqinc/SDL_GameControllerDB](https://github.com/mdqinc/SDL_GameControllerDB)**.
* Built with **Python**, **PyQt6**, **pygame (SDL2)**, and **hidapi**.