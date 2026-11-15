import json
import os
import struct
import sys
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
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


def get_app_dir() -> Path:
    """Gibt immer den Ordner zurück, in dem die EXE (oder das .py-Skript) liegt."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
OUTPUT_JSON = APP_DIR / "controller_data.json"
CONFIG_FILE = APP_DIR / "bridge_config.json"

SPACEMOUSE_VENDORS = (0x256F, 0x046D)
SPACEMOUSE_AXES = ["x", "y", "z", "pitch", "roll", "yaw"]


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
    """Liest alle SpaceMice (HID) und Gamepads/Joysticks (Pygame) gleichzeitig aus."""

    state_updated = pyqtSignal(dict)
    status_updated = pyqtSignal(str)

    def __init__(self, deadzone: float = 0.04):
        super().__init__()
        self.deadzone = deadzone
        self.running = True

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

        sm_handles = {}  # id -> {"handle": hid.device, "name": str, "raw": dict}
        pg_joysticks = {}  # id -> pygame.joystick.Joystick
        last_scan = 0.0
        last_write = 0.0

        while self.running:
            now = time.time()

            # Alle 2 Sekunden nach neu angeschlossenen/getrennten Geräten suchen
            if now - last_scan >= 2.0:
                last_scan = now

                # 1. SpaceMice prüfen
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

                # 2. Pygame Joysticks / Gamepads prüfen
                if pygame is not None:
                    pygame.event.pump()
                    count = pygame.joystick.get_count()
                    active_pg_ids = set()
                    for i in range(count):
                        try:
                            joy = pygame.joystick.Joystick(i)
                            if not joy.get_init():
                                joy.init()
                            # SpaceMouse nicht doppelt über DirectInput erfassen, falls Treiber sie als Joystick meldet
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

            # Daten aller Controller einsammeln
            controllers_payload = {}

            # SpaceMouse Pakete auslesen
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

                    controllers_payload[cid] = {
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

            # Pygame Joysticks / Gamepads auslesen
            if pygame is not None:
                pygame.event.pump()
                for cid, joy in list(pg_joysticks.items()):
                    try:
                        axes = {}
                        for a_idx in range(joy.get_numaxes()):
                            axes[f"axis_{a_idx}"] = self._apply_deadzone(joy.get_axis(a_idx))

                        # D-Pad (Hat) ebenfalls als Achsen bereitstellen
                        for h_idx in range(joy.get_numhats()):
                            hx, hy = joy.get_hat(h_idx)
                            axes[f"hat_{h_idx}_x"] = float(hx)
                            axes[f"hat_{h_idx}_y"] = float(hy)

                        buttons = {}
                        for b_idx in range(joy.get_numbuttons()):
                            buttons[f"btn_{b_idx + 1}"] = 1 if joy.get_button(b_idx) else 0

                        controllers_payload[cid] = {
                            "id": cid,
                            "name": joy.get_name(),
                            "axes": axes,
                            "buttons": buttons,
                        }
                    except Exception:
                        del pg_joysticks[cid]

            full_data = {
                "timestamp": round(now, 3),
                "controller_count": len(controllers_payload),
                "controllers": controllers_payload,
            }

            self.state_updated.emit(full_data)
            self.status_updated.emit(
                f"Aktive Controller: {len(controllers_payload)}  |  Ziel: {OUTPUT_JSON.name}"
            )

            # Mit ~40 Hz atomar in JSON neben der EXE schreiben
            if now - last_write >= 0.025:
                self._write_atomic(full_data)
                last_write = now

            time.sleep(0.008)

        for entry in sm_handles.values():
            try:
                entry["handle"].close()
            except Exception:
                pass
        if pygame is not None:
            pygame.quit()

    def _write_atomic(self, data: dict):
        try:
            tmp = OUTPUT_JSON.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, OUTPUT_JSON)
        except Exception:
            pass

    def stop(self):
        self.running = False
        self.wait(1000)


class ControllerCard(QFrame):
    """UI-Karte für einen einzelnen erkannten Controller mit allen Achsen & Buttons."""

    def __init__(self, cid: str, name: str, axes_keys: list[str], btn_keys: list[str]):
        super().__init__()
        self.setStyleSheet("background-color: #1e1e1e; border-radius: 10px; padding: 10px;")
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        header = QLabel(f"{name}   [{cid}]")
        header.setStyleSheet("color: #00d2ff; font-weight: bold; font-size: 13px;")
        layout.addWidget(header)

        # Achsen-Grid (2 Spalten)
        self.axis_bars: dict[str, BipolarAxisBar] = {}
        axes_grid = QGridLayout()
        axes_grid.setSpacing(8)
        for idx, a_key in enumerate(axes_keys):
            bar = BipolarAxisBar(a_key.upper())
            self.axis_bars[a_key] = bar
            axes_grid.addWidget(bar, idx // 2, idx % 2)
        layout.addLayout(axes_grid)

        # Buttons-Flow
        self.btn_pills: dict[str, ButtonPill] = {}
        if btn_keys:
            btn_grid = QGridLayout()
            btn_grid.setSpacing(5)
            for idx, b_key in enumerate(btn_keys):
                pill = ButtonPill(b_key.replace("btn_", "B"))
                self.btn_pills[b_key] = pill
                btn_grid.addWidget(pill, idx // 8, idx % 8)
            layout.addLayout(btn_grid)

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
        self.setWindowTitle("Scrap Mechanic – Universal Multi-Controller Bridge")
        self.resize(640, 620)
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #121212;
                color: #e6e6e6;
                font-family: 'Segoe UI', sans-serif;
            }
            QDoubleSpinBox {
                background-color: #252525;
                border: 1px solid #3a3a3a;
                border-radius: 6px;
                padding: 4px 8px;
                color: white;
            }
            """
        )

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(10)

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
        self.dz_spin.setValue(0.04)
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

        self.worker = MultiControllerWorker(deadzone=self.dz_spin.value())
        self.worker.state_updated.connect(self._on_state_updated)
        self.worker.status_updated.connect(self.status_lbl.setText)
        self.worker.start()

    def _on_dz_changed(self, val: float):
        self.worker.deadzone = val

    def _on_state_updated(self, data: dict):
        controllers = data.get("controllers", {})

        # Entfernte Controller aus UI löschen
        for cid in list(self.cards.keys()):
            if cid not in controllers:
                card = self.cards.pop(cid)
                self.cards_layout.removeWidget(card)
                card.deleteLater()

        # Neue Controller anlegen & bestehende aktualisieren
        for cid, cdata in controllers.items():
            axes = cdata.get("axes", {})
            buttons = cdata.get("buttons", {})
            if cid not in self.cards:
                card = ControllerCard(cid, cdata.get("name", cid), list(axes.keys()), list(buttons.keys()))
                self.cards[cid] = card
                self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            self.cards[cid].update_values(axes, buttons)

    def closeEvent(self, event):
        self.worker.stop()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())