import ctypes
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

try:
    import hid
except ImportError:
    hid = None

try:
    import pygame
except ImportError:
    pygame = None

# --------------------------------------------------------------------------
# GitHub Update-Konfiguration
# --------------------------------------------------------------------------

GITHUB_REPO = "Leontierchen/Any-Controller-to-json-Readout"
GITHUB_TOKEN = os.environ.get("MY_GITHUB_TOKEN", "")  # Wird beim Kompilieren durch den GitHub Uploader gesetzt
__version__ = "0.3"


def parse_version(v_str: str) -> tuple:
    nums = re.findall(r"\d+", v_str)
    return tuple(map(int, nums)) if nums else (0,)


def _build_github_headers(accept: str = "application/vnd.github+json") -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": accept,
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


class UpdateCheckWorker(QThread):
    """Prüft beim Programmstart asynchron im Hintergrund auf GitHub-Releases."""

    result_ready = pyqtSignal(str, str, str)  # status_state ("ok", "update", "error"), latest_ver, message

    def run(self):
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(url, headers=_build_github_headers())
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))

            latest_tag = data.get("tag_name", "").strip()
            latest_version = latest_tag.lstrip("v")

            if latest_version and parse_version(latest_version) > parse_version(__version__):
                self.result_ready.emit(
                    "update",
                    latest_version,
                    f"App Version v{__version__} - veraltet - Neues Update verfügbar: v{latest_version} – Klicken zum Installieren",
                )
            else:
                self.result_ready.emit(
                    "ok",
                    __version__,
                    f"App Version v{__version__} - aktuell – Klicken zum erneuten Prüfen",
                )
        except Exception:
            self.result_ready.emit(
                "error",
                "",
                f"App Version v{__version__} - status unbekannt – Klicken zum Prüfen",
            )


def check_for_github_update(parent_widget=None, manual: bool = False, status_callback=None):
    is_frozen = getattr(sys, "frozen", False)
    if not is_frozen and not manual:
        return

    try:
        if status_callback:
            status_callback("checking", "", "Prüfe GitHub auf Updates...")

        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers=_build_github_headers())

        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        latest_tag = data.get("tag_name", "").strip()
        latest_version = latest_tag.lstrip("v")

        if not latest_version or parse_version(latest_version) <= parse_version(__version__):
            if status_callback:
                status_callback("ok", __version__, f"App Version v{__version__} - Aktuell – Klicken zum erneuten Prüfen")
            if manual:
                QMessageBox.information(
                    parent_widget,
                    "Kein Update vorhanden",
                    f"Du verwendest bereits die aktuellste Version (v{__version__}).",
                )
            return

        if status_callback:
            status_callback(
                "update",
                latest_version,
                f"App Version v{__version__} - Neues Update verfügbar: v{latest_version} – Klicken zum Installieren",
            )

        download_url = None
        for asset in data.get("assets", []):
            if asset.get("name", "").lower().endswith(".zip"):
                download_url = asset.get("url")
                break

        if not download_url:
            if manual:
                QMessageBox.warning(
                    parent_widget,
                    "Update-Fehler",
                    f"Eine neue Version (v{latest_version}) wurde gefunden, enthält aber kein ZIP-Paket.",
                )
            return

        reply = QMessageBox.question(
            parent_widget,
            "Update verfügbar",
            f"Eine neue Version (v{latest_version}) ist verfügbar! (Aktuell: v{__version__})\n\n"
            f"Möchtest du das Update jetzt herunterladen und installieren?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        if not is_frozen:
            QMessageBox.information(
                parent_widget,
                "Entwickler-Hinweis",
                "Das Update kann nur in der gebauten EXE-Version automatisch installiert werden.",
            )
            return

        app_dir = Path(sys.executable).resolve().parent
        temp_zip = app_dir / "update_temp.zip"

        asset_req = urllib.request.Request(
            download_url,
            headers=_build_github_headers(accept="application/octet-stream"),
        )
        with urllib.request.urlopen(asset_req, timeout=15) as resp, open(temp_zip, "wb") as out_file:
            shutil.copyfileobj(resp, out_file)

        current_exe = Path(sys.executable).resolve()
        bat_path = app_dir / "update_updater.bat"

        bat_script = f"""@echo off
timeout /t 2 /nobreak > NUL
powershell -NoProfile -ExecutionPolicy Bypass -Command "$appDir = '{app_dir}'; $tempZip = '{temp_zip}'; $extractDir = Join-Path $appDir 'update_extracted'; Expand-Archive -Path $tempZip -DestinationPath $extractDir -Force; Get-ChildItem -Path $extractDir -Recurse | ForEach-Object {{ $rel = $_.FullName.Substring($extractDir.Length); $target = $appDir + $rel; if ($_.PSIsContainer) {{ if (-not (Test-Path $target)) {{ New-Item -ItemType Directory -Path $target | Out-Null }} }} else {{ if ($_.Extension -eq '.json' -and (Test-Path $target)) {{ Write-Host 'Schütze existierende Datei: ' $target }} else {{ Copy-Item -Path $_.FullName -Destination $target -Force }} }} }}; Remove-Item -Path $extractDir -Recurse -Force; Remove-Item -Path $tempZip -Force;"
start "" "{current_exe}"
del "%~f0"
"""
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_script)

        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen(["cmd.exe", "/c", str(bat_path)], creationflags=creation_flags)
        sys.exit(0)

    except Exception as e:
        if status_callback:
            status_callback("error", "", f"App Version v{__version__} - Update-Prüfung fehlgeschlagen – Klicken zum Wiederholen")
        if manual:
            QMessageBox.critical(parent_widget, "Fehler", f"Update-Prüfung fehlgeschlagen:\n{e}")


def ensure_single_instance(mutex_name: str = "Global\\ScrapMechanicUniversalControllerBridgeMutex"):
    """Verhindert Mehrfachstarts unter Windows über einen Named Mutex und zeigt eine Info-Meldung."""
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, mutex_name)
    # ERROR_ALREADY_EXISTS = 183
    if kernel32.GetLastError() == 183:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Die Universal Multi-Controller Bridge läuft bereits im Hintergrund.\n\n"
            "Bitte prüfe deine Taskleiste oder das bereits geöffnete Fenster.",
            "Controller Bridge bereits aktiv",
            0x00000030 | 0x00040000,
        )
        sys.exit(0)
    return mutex


def get_app_dir() -> Path:
    """Gibt immer den Ordner zurück, in dem die EXE (oder das .pyw-Skript) liegt."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(filename: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", get_app_dir()))
    return base / filename


APP_DIR = get_app_dir()
OUTPUT_JSON = APP_DIR / "controller_data.json"
CONFIG_FILE = APP_DIR / "bridge_config.json"
ICON_FILE = get_resource_path("Controller.ico")

SPACEMOUSE_VENDORS = (0x256F, 0x046D)
SPACEMOUSE_AXES = ["x", "y", "z", "pitch", "roll", "yaw"]


class ClickableLabel(QLabel):
    """Klickbares QLabel für die Update-Statuszeile."""

    clicked = pyqtSignal()

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class BipolarAxisBar(QWidget):
    """Kompakter -1.0 bis +1.0 Live-Balken für Achsen."""

    def __init__(self, label_text: str, accent_color: str = "#00d2ff"):
        super().__init__()
        self.label_text = label_text
        self.accent_color = QColor(accent_color)
        self.value = 0.0
        self.setMinimumHeight(34)

    def set_value(self, val: float):
        val = max(-1.0, min(1.0, float(val)))
        if abs(val - self.value) > 0.002:
            self.value = val
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()

        painter.setPen(QColor("#dcdcdc"))
        painter.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        painter.drawText(2, 12, self.label_text)

        val_str = f"{self.value:+.2f}"
        painter.setPen(self.accent_color if abs(self.value) > 0.005 else QColor("#777777"))
        painter.drawText(w - 45, 12, val_str)

        bar_y, bar_h = 16, 12
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#262626"))
        painter.drawRoundedRect(0, bar_y, w, bar_h, 4, 4)

        center_x = w / 2.0
        if abs(self.value) > 0.001:
            painter.setBrush(self.accent_color)
            if self.value > 0:
                painter.drawRoundedRect(int(center_x), bar_y, int((w / 2.0) * self.value), bar_h, 3, 3)
            else:
                bw = (w / 2.0) * abs(self.value)
                painter.drawRoundedRect(int(center_x - bw), bar_y, int(bw), bar_h, 3, 3)

        painter.setPen(QPen(QColor("#ffffff"), 1))
        painter.drawLine(int(center_x), bar_y - 1, int(center_x), bar_y + bar_h + 1)


class ButtonPill(QLabel):
    """Visueller Indikator für einen digitalen Button (0 oder 1)."""

    def __init__(self, name: str):
        super().__init__(name)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(24)
        self.setMinimumWidth(46)
        self.active = None
        self.set_pressed(False)

    def set_pressed(self, pressed: bool):
        if self.active == pressed:
            return
        self.active = pressed
        if pressed:
            self.setStyleSheet(
                "background-color: #00d2ff; color: #111111; border-radius: 10px; "
                "font-weight: bold; font-size: 11px; padding: 2px 6px;"
            )
        else:
            self.setStyleSheet(
                "background-color: #262626; color: #888888; border-radius: 10px; "
                "font-size: 11px; padding: 2px 6px;"
            )


class MultiControllerWorker(QThread):
    """Liest alle SpaceMice (HID) und Gamepads/Joysticks (Pygame) aus und schreibt nur aktivierte Geräte."""

    state_updated = pyqtSignal(dict)
    status_updated = pyqtSignal(str)

    def __init__(self, deadzone: float = 0.04, enabled_map: dict[str, bool] | None = None):
        super().__init__()
        self.deadzone = deadzone
        self.enabled_map: dict[str, bool] = dict(enabled_map) if enabled_map else {}
        self.running = True
        self.force_rewrite = False

    def set_controller_enabled(self, cid: str, enabled: bool):
        self.enabled_map[cid] = enabled
        self.force_rewrite = True

    def is_controller_enabled(self, cid: str) -> bool:
        return self.enabled_map.get(cid, True)

    def _apply_deadzone(self, val: float) -> float:
        val = max(-1.0, min(1.0, float(val)))
        if abs(val) < self.deadzone:
            return 0.0
        sign = 1.0 if val > 0 else -1.0
        scaled = (abs(val) - self.deadzone) / (1.0 - self.deadzone)
        return round(max(-1.0, min(1.0, sign * scaled)), 4)

    def _scan_spacemice(self) -> list[dict]:
        found = []
        if hid is None:
            return found
        seen_paths = set()
        for dev in hid.enumerate():
            if dev["vendor_id"] in SPACEMOUSE_VENDORS:
                if dev.get("usage_page") == 1 and dev.get("usage") == 8:
                    path = dev["path"]
                    if path not in seen_paths:
                        seen_paths.add(path)
                        found.append(dev)
        return found

    def run(self):
        if pygame is not None:
            pygame.init()
            pygame.joystick.init()

        sm_handles = {}
        pg_joysticks = {}
        last_scan = 0.0
        last_write = 0.0
        last_payload = ""

        while self.running:
            now = time.time()

            if now - last_scan >= 2.0:
                last_scan = now

                if hid is not None:
                    current_sm = self._scan_spacemice()
                    active_sm_ids = set()
                    for idx, info in enumerate(current_sm):
                        cid = f"spacemouse_{idx}"
                        active_sm_ids.add(cid)
                        if cid not in sm_handles:
                            try:
                                h = hid.device()
                                h.open_path(info["path"])
                                h.set_nonblocking(True)
                                name = info.get("product_string") or f"3Dconnexion SpaceMouse #{idx + 1}"
                                sm_handles[cid] = {
                                    "handle": h,
                                    "name": name,
                                    "raw": {a: 0.0 for a in SPACEMOUSE_AXES},
                                    "buttons": {f"btn_{b}": 0 for b in range(1, 9)},
                                }
                            except Exception:
                                pass

                    for cid in list(sm_handles.keys()):
                        if cid not in active_sm_ids:
                            try:
                                sm_handles[cid]["handle"].close()
                            except Exception:
                                pass
                            del sm_handles[cid]

                if pygame is not None:
                    pygame.event.pump()
                    count = pygame.joystick.get_count()
                    active_pg_ids = set()
                    for i in range(count):
                        try:
                            joy = pygame.joystick.Joystick(i)
                            if not joy.get_init():
                                joy.init()
                            jname = joy.get_name()
                            if "3dconnexion" in jname.lower() or "spacemouse" in jname.lower():
                                continue
                            cid = f"joy_{joy.get_instance_id()}"
                            active_pg_ids.add(cid)
                            if cid not in pg_joysticks:
                                pg_joysticks[cid] = joy
                        except Exception:
                            pass

                    for cid in list(pg_joysticks.keys()):
                        if cid not in active_pg_ids:
                            del pg_joysticks[cid]

            all_controllers = {}

            for cid, entry in list(sm_handles.items()):
                h = entry["handle"]
                raw = entry["raw"]
                btns = entry["buttons"]
                try:
                    while True:
                        pkt = h.read(64)
                        if not pkt:
                            break
                        rid = pkt[0]
                        if rid == 1 and len(pkt) >= 7:
                            tx, ty, tz = struct.unpack("<hhh", bytes(pkt[1:7]))
                            raw["x"] = tx / 350.0
                            raw["y"] = -ty / 350.0
                            raw["z"] = -tz / 350.0
                            if len(pkt) >= 13:
                                rx, ry, rz = struct.unpack("<hhh", bytes(pkt[7:13]))
                                raw["pitch"] = rx / 350.0
                                raw["roll"] = ry / 350.0
                                raw["yaw"] = rz / 350.0
                        elif rid == 2 and len(pkt) >= 7:
                            rx, ry, rz = struct.unpack("<hhh", bytes(pkt[1:7]))
                            raw["pitch"] = rx / 350.0
                            raw["roll"] = ry / 350.0
                            raw["yaw"] = rz / 350.0
                        elif rid == 3 and len(pkt) >= 2:
                            mask = pkt[1]
                            for b_idx in range(8):
                                btns[f"btn_{b_idx + 1}"] = 1 if (mask & (1 << b_idx)) else 0

                    all_controllers[cid] = {
                        "id": cid,
                        "name": entry["name"],
                        "axes": {k: self._apply_deadzone(v) for k, v in raw.items()},
                        "buttons": dict(btns),
                    }
                except Exception:
                    try:
                        h.close()
                    except Exception:
                        pass
                    del sm_handles[cid]

            if pygame is not None:
                pygame.event.pump()
                for cid, joy in list(pg_joysticks.items()):
                    try:
                        axes = {}
                        for a_idx in range(joy.get_numaxes()):
                            axes[f"axis_{a_idx}"] = self._apply_deadzone(joy.get_axis(a_idx))

                        for h_idx in range(joy.get_numhats()):
                            hx, hy = joy.get_hat(h_idx)
                            axes[f"hat_{h_idx}_x"] = float(hx)
                            axes[f"hat_{h_idx}_y"] = float(hy)

                        buttons = {}
                        for b_idx in range(joy.get_numbuttons()):
                            buttons[f"btn_{b_idx + 1}"] = 1 if joy.get_button(b_idx) else 0

                        all_controllers[cid] = {
                            "id": cid,
                            "name": joy.get_name(),
                            "axes": axes,
                            "buttons": buttons,
                        }
                    except Exception:
                        del pg_joysticks[cid]

            exported_controllers = {
                cid: cdata
                for cid, cdata in all_controllers.items()
                if self.is_controller_enabled(cid)
            }

            self.state_updated.emit({"controllers": all_controllers})
            self.status_updated.emit(
                f"Erkannt: {len(all_controllers)}  |  Im Spiel aktiv: {len(exported_controllers)}  |  {OUTPUT_JSON.name}"
            )

            if now - last_write >= 0.045 or self.force_rewrite:
                clean_data = {
                    "controller_count": len(exported_controllers),
                    "controllers": exported_controllers,
                }
                payload_str = json.dumps(clean_data, separators=(",", ":"))
                if payload_str != last_payload or self.force_rewrite:
                    if self._write_safe(payload_str):
                        last_payload = payload_str
                        self.force_rewrite = False
                last_write = now

            time.sleep(0.008)

        for entry in sm_handles.values():
            try:
                entry["handle"].close()
            except Exception:
                pass
        if pygame is not None:
            pygame.quit()

    def _write_safe(self, payload_str: str) -> bool:
        try:
            mode = "r+" if OUTPUT_JSON.exists() else "w"
            with open(OUTPUT_JSON, mode, encoding="utf-8") as f:
                f.seek(0)
                f.write(payload_str)
                f.truncate()
                f.flush()
            return True
        except Exception:
            return False

    def stop(self):
        self.running = False
        self.wait(1000)


class CheckToggle(QWidget):
    """Zeichnet ein klares, modernes Häkchen-Kästchen mit QPainter."""

    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True):
        super().__init__()
        self.checked = checked
        self.hovered = False
        self.setFixedSize(22, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event):
        self.hovered = True
        self.update()

    def leaveEvent(self, event):
        self.hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.checked = not self.checked
            self.update()
            self.toggled.emit(self.checked)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(1, 1, -1, -1)
        if self.checked:
            border = QColor("#38e882") if self.hovered else QColor("#2ecc71")
            fill = QColor("#23b866") if self.hovered else QColor("#1a9c54")
            painter.setPen(QPen(border, 1.8))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 6, 6)

            pen = QPen(QColor("#ffffff"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(6, 11, 10, 15)
            painter.drawLine(10, 15, 16, 7)
        else:
            border = QColor("#777777") if self.hovered else QColor("#4a4a4a")
            painter.setPen(QPen(border, 1.8))
            painter.setBrush(QColor("#242424"))
            painter.drawRoundedRect(rect, 6, 6)


class ControllerCard(QFrame):
    """UI-Karte für einen Controller mit sauberem Dark-Mode-Styling."""

    toggled_export = pyqtSignal(str, bool)

    def __init__(self, cid: str, name: str, axes_keys: list[str], btn_keys: list[str], checked: bool = True):
        super().__init__()
        self.cid = cid
        self.setObjectName("ControllerCard")
        self.setStyleSheet(
            """
            QFrame#ControllerCard {
                background-color: #1b1b1e;
                border: 1px solid #2c2c32;
                border-radius: 12px;
            }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self.check_box = CheckToggle(checked=checked)
        self.check_box.toggled.connect(self._on_check_changed)

        self.title_lbl = QLabel(f"{name}")
        self.title_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_lbl.mousePressEvent = lambda e: self.check_box.mousePressEvent(e)

        self.id_lbl = QLabel(f"[{cid}]")
        self.id_lbl.setStyleSheet("color: #6c6c75; font-size: 11px; font-family: 'Consolas', monospace;")

        self.state_badge = QLabel()
        self.state_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_badge.setFixedHeight(22)
        self._update_badge_style(checked)

        top_row.addWidget(self.check_box)
        top_row.addWidget(self.title_lbl)
        top_row.addWidget(self.id_lbl)
        top_row.addStretch()
        top_row.addWidget(self.state_badge)
        layout.addLayout(top_row)

        self.axis_bars: dict[str, BipolarAxisBar] = {}
        axes_grid = QGridLayout()
        axes_grid.setHorizontalSpacing(14)
        axes_grid.setVerticalSpacing(6)
        for idx, a_key in enumerate(axes_keys):
            bar = BipolarAxisBar(a_key.upper())
            self.axis_bars[a_key] = bar
            axes_grid.addWidget(bar, idx // 2, idx % 2)
        layout.addLayout(axes_grid)

        self.btn_pills: dict[str, ButtonPill] = {}
        if btn_keys:
            btn_grid = QGridLayout()
            btn_grid.setSpacing(6)
            for idx, b_key in enumerate(btn_keys):
                pill = ButtonPill(b_key.replace("btn_", "B"))
                self.btn_pills[b_key] = pill
                btn_grid.addWidget(pill, idx // 8, idx % 8)
            layout.addLayout(btn_grid)

    def _on_check_changed(self, state: bool):
        self._update_badge_style(state)
        self.toggled_export.emit(self.cid, state)

    def _update_badge_style(self, active: bool):
        if active:
            self.state_badge.setText("AKTIV")
            self.state_badge.setStyleSheet(
                "background-color: #153626; color: #2ecc71; border: 1px solid #235c40; "
                "border-radius: 11px; padding: 0px 10px; font-size: 10px; font-weight: bold;"
            )
            self.title_lbl.setStyleSheet("color: #00d2ff; font-weight: bold; font-size: 13px;")
        else:
            self.state_badge.setText("IGNORIERT")
            self.state_badge.setStyleSheet(
                "background-color: #252529; color: #777780; border: 1px solid #35353b; "
                "border-radius: 11px; padding: 0px 10px; font-size: 10px; font-weight: bold;"
            )
            self.title_lbl.setStyleSheet("color: #777780; font-weight: bold; font-size: 13px;")

    def update_values(self, axes: dict, buttons: dict):
        for k, v in axes.items():
            if k in self.axis_bars:
                self.axis_bars[k].set_value(v)
        for k, v in buttons.items():
            if k in self.btn_pills:
                self.btn_pills[k].set_pressed(bool(v))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Scrap Mechanic – Universal Multi-Controller Bridge v{__version__}")
        self.resize(650, 640)
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #121215;
                color: #e6e6e6;
                font-family: 'Segoe UI', sans-serif;
            }
            QDoubleSpinBox {
                background-color: #1e1e23;
                border: 1px solid #34343c;
                border-radius: 6px;
                padding: 4px 10px;
                color: white;
                font-weight: bold;
            }
            QScrollBar:vertical {
                background: #16161a;
                width: 8px;
                border-radius: 4px;
                margin: 2px;
            }
            QScrollBar::handle:vertical {
                background: #363640;
                min-height: 28px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: #00d2ff;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            """
        )

        self.config = self._load_config()
        saved_deadzone = float(self.config.get("deadzone", 0.04))
        saved_enabled = self.config.get("enabled_controllers", {})

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(10)

        # Klickbare Update-Statuszeile ganz oben
        self.update_lbl = ClickableLabel(f"Prüfe auf Updates (v{__version__})... – Klicken zum Prüfen")
        self._set_update_banner("checking", "", f"Prüfe auf Updates (v{__version__})... – Klicken zum Prüfen")
        self.update_lbl.clicked.connect(self._on_manual_update_click)
        main_layout.addWidget(self.update_lbl)

        # Status & Pfad-Info
        self.status_lbl = QLabel("Suche nach Controllern...")
        self.status_lbl.setStyleSheet(
            "background-color: #1b2e38; color: #00d2ff; padding: 8px 12px; "
            "border-radius: 10px; font-weight: bold;"
        )
        main_layout.addWidget(self.status_lbl)

        path_lbl = QLabel(f"Ausgabepfad (neben EXE): {OUTPUT_JSON}")
        path_lbl.setStyleSheet("color: #888888; font-size: 11px;")
        path_lbl.setWordWrap(True)
        main_layout.addWidget(path_lbl)

        # Deadzone-Regler
        dz_row = QHBoxLayout()
        dz_row.addWidget(QLabel("Globale Totzone (Deadzone):"))
        self.dz_spin = QDoubleSpinBox()
        self.dz_spin.setRange(0.0, 0.50)
        self.dz_spin.setSingleStep(0.01)
        self.dz_spin.setValue(saved_deadzone)
        self.dz_spin.valueChanged.connect(self._on_dz_changed)
        dz_row.addStretch()
        dz_row.addWidget(self.dz_spin)
        main_layout.addLayout(dz_row)

        # Scrollbereich für alle erkannten Controller
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setSpacing(12)
        self.cards_layout.addStretch()
        scroll.setWidget(self.cards_container)
        main_layout.addWidget(scroll)

        self.cards: dict[str, ControllerCard] = {}

        self.worker = MultiControllerWorker(deadzone=saved_deadzone, enabled_map=saved_enabled)
        self.worker.state_updated.connect(self._on_state_updated)
        self.worker.status_updated.connect(self.status_lbl.setText)
        self.worker.start()

        # Automatische Hintergrund-Update-Prüfung beim Programmstart
        self.update_worker = UpdateCheckWorker()
        self.update_worker.result_ready.connect(self._on_startup_update_result)
        self.update_worker.start()

    def _set_update_banner(self, state: str, latest_ver: str, text: str):
        self.update_lbl.setText(text)
        if state == "update":
            self.update_lbl.setStyleSheet(
                "background-color: #3a2410; color: #ff9f43; border: 1px solid #d37624; "
                "padding: 7px 12px; border-radius: 10px; font-weight: bold; font-size: 12px;"
            )
        elif state == "ok":
            self.update_lbl.setStyleSheet(
                "background-color: #153024; color: #2ecc71; border: 1px solid #22543d; "
                "padding: 7px 12px; border-radius: 10px; font-size: 11px;"
            )
        else:
            self.update_lbl.setStyleSheet(
                "background-color: #1c1c22; color: #9a9aa6; border: 1px solid #2e2e38; "
                "padding: 7px 12px; border-radius: 10px; font-size: 11px;"
            )

    def _on_startup_update_result(self, state: str, latest_ver: str, text: str):
        self._set_update_banner(state, latest_ver, text)
        # Wenn in der gebauten EXE ein neues Update gefunden wurde, direkt Installations-Dialog anbieten
        if state == "update" and getattr(sys, "frozen", False):
            check_for_github_update(self, manual=False, status_callback=self._set_update_banner)

    def _on_manual_update_click(self):
        check_for_github_update(self, manual=True, status_callback=self._set_update_banner)

    def _on_dz_changed(self, val: float):
        self.worker.deadzone = val
        self.config["deadzone"] = val
        self._save_config()

    def _on_controller_toggled(self, cid: str, enabled: bool):
        self.worker.set_controller_enabled(cid, enabled)
        if "enabled_controllers" not in self.config:
            self.config["enabled_controllers"] = {}
        self.config["enabled_controllers"][cid] = enabled
        self._save_config()

    def _on_state_updated(self, data: dict):
        controllers = data.get("controllers", {})

        for cid in list(self.cards.keys()):
            if cid not in controllers:
                card = self.cards.pop(cid)
                self.cards_layout.removeWidget(card)
                card.deleteLater()

        for cid, cdata in controllers.items():
            axes = cdata.get("axes", {})
            buttons = cdata.get("buttons", {})
            if cid not in self.cards:
                is_checked = self.worker.is_controller_enabled(cid)
                card = ControllerCard(
                    cid,
                    cdata.get("name", cid),
                    list(axes.keys()),
                    list(buttons.keys()),
                    checked=is_checked,
                )
                card.toggled_export.connect(self._on_controller_toggled)
                self.cards[cid] = card
                self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            self.cards[cid].update_values(axes, buttons)

    def _load_config(self) -> dict:
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_config(self):
        try:
            CONFIG_FILE.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        except Exception:
            pass

    def closeEvent(self, event):
        self.worker.stop()
        if self.update_worker.isRunning():
            self.update_worker.wait(500)
        event.accept()


if __name__ == "__main__":
    _instance_mutex = ensure_single_instance()
    app = QApplication(sys.argv)
    if ICON_FILE.exists():
        app.setWindowIcon(QIcon(str(ICON_FILE)))

    win = MainWindow()
    win.show()
    sys.exit(app.exec())