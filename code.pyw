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
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:
    import hid
except ImportError:
    hid = None


CONFIG_FILE = Path(__file__).parent / "sm_spacemouse_config.json"

# 3Dconnexion Vendor IDs (0x256F = neuere Modelle, 0x046D = ältere Logitech/3Dconnexion Modelle)
VENDOR_IDS = (0x256F, 0x046D)

AXES = [
    ("x", "X (Links / Rechts)"),
    ("y", "Y (Vor / Zurück)"),
    ("z", "Z (Hoch / Runter)"),
    ("pitch", "Pitch (Nicken)"),
    ("roll", "Roll (Rollen)"),
    ("yaw", "Yaw (Drehen)"),
]


class BipolarAxisBar(QWidget):
    """Zeichnet einen zentrierten -1.0 bis +1.0 Balken mit exakter Wertanzeige."""

    def __init__(self, label_text: str, accent_color: str = "#00d2ff"):
        super().__init__()
        self.label_text = label_text
        self.accent_color = QColor(accent_color)
        self.value = 0.0
        self.setMinimumHeight(46)

    def set_value(self, val: float):
        val = max(-1.0, min(1.0, float(val)))
        if abs(val - self.value) > 0.001:
            self.value = val
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()

        # Header-Text
        painter.setPen(QColor("#e0e0e0"))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.drawText(2, 14, self.label_text)

        val_str = f"{self.value:+.3f}"
        painter.setPen(self.accent_color if abs(self.value) > 0.001 else QColor("#888888"))
        painter.drawText(w - 60, 14, val_str)

        # Track-Hintergrund
        bar_y = 20
        bar_h = 18
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2b2b2b"))
        painter.drawRoundedRect(0, bar_y, w, bar_h, 6, 6)

        # Aktiver Balken von der Mitte aus
        center_x = w / 2.0
        if abs(self.value) > 0.0001:
            painter.setBrush(self.accent_color)
            if self.value > 0:
                bar_w = (w / 2.0) * self.value
                painter.drawRoundedRect(int(center_x), bar_y, int(bar_w), bar_h, 4, 4)
            else:
                bar_w = (w / 2.0) * abs(self.value)
                painter.drawRoundedRect(int(center_x - bar_w), bar_y, int(bar_w), bar_h, 4, 4)

        # Mittellinie (0.0 Marker)
        painter.setPen(QPen(QColor("#ffffff"), 2))
        painter.drawLine(int(center_x), bar_y - 2, int(center_x), bar_y + bar_h + 2)


class SpaceMouseWorker(QThread):
    """Liest die SpaceMouse direkt per USB-HID aus und schreibt atomar die JSON-Datei."""

    data_updated = pyqtSignal(dict)
    status_changed = pyqtSignal(str, bool)

    def __init__(self, output_path: str, deadzone: float = 0.03):
        super().__init__()
        self.output_path = output_path
        self.deadzone = deadzone
        self.running = True

    def update_settings(self, output_path: str, deadzone: float):
        self.output_path = output_path
        self.deadzone = deadzone

    def _apply_deadzone(self, raw_val: int, max_range: float = 350.0) -> float:
        # 3Dconnexion liefert Werte typischerweise im Bereich -350 bis +350
        norm = max(-1.0, min(1.0, raw_val / max_range))
        if abs(norm) < self.deadzone:
            return 0.0
        sign = 1.0 if norm > 0 else -1.0
        scaled = (abs(norm) - self.deadzone) / (1.0 - self.deadzone)
        return round(max(-1.0, min(1.0, sign * scaled)), 4)

    def _find_spacemouse_device(self):
        """Sucht das passende HID-Interface (Multi-Axis Controller: Usage Page 1, Usage 8)."""
        devices = hid.enumerate()
        fallback = None
        for dev in devices:
            if dev["vendor_id"] in VENDOR_IDS:
                # Usage Page 1 (Generic Desktop), Usage 8 (Multi-axis Controller)
                if dev.get("usage_page") == 1 and dev.get("usage") == 8:
                    return dev
                if fallback is None:
                    fallback = dev
        return fallback

    def run(self):
        if hid is None:
            self.status_changed.emit("Fehler: 'hidapi' nicht installiert (pip install hidapi)", False)
            return

        dev_handle = None
        last_write = 0.0

        # Aktueller Zustand aller 6 Achsen + Buttons
        raw_state = {
            "x": 0,
            "y": 0,
            "z": 0,
            "pitch": 0,
            "roll": 0,
            "yaw": 0,
            "buttons": [0, 0],
        }

        while self.running:
            if dev_handle is None:
                try:
                    info = self._find_spacemouse_device()
                    if info is None:
                        self.status_changed.emit("Keine SpaceMouse gefunden – suche...", False)
                        time.sleep(1.5)
                        continue

                    dev_handle = hid.device()
                    dev_handle.open_path(info["path"])
                    dev_handle.set_nonblocking(True)
                    prod_name = info.get("product_string") or "3Dconnexion SpaceMouse"
                    self.status_changed.emit(f"Verbunden: {prod_name}", True)
                except Exception as e:
                    dev_handle = None
                    self.status_changed.emit(f"Verbindungsfehler: {e}", False)
                    time.sleep(1.5)
                    continue

            try:
                # Alle gepufferten HID-Pakete im aktuellen Zyklus auslesen
                updated = False
                while True:
                    packet = dev_handle.read(64)
                    if not packet:
                        break
                    updated = True
                    report_id = packet[0]

                    # Report 1: Translation (X, Y, Z) als 3x signed 16-bit int (Little Endian)
                    if report_id == 1 and len(packet) >= 7:
                        tx, ty, tz = struct.unpack("<hhh", bytes(packet[1:7]))
                        raw_state["x"] = tx
                        raw_state["y"] = -ty  # Vor/Zurück invertieren für intuitive Achse
                        raw_state["z"] = -tz  # Hoch/Runter invertieren

                        # Manche kompakten Modelle senden alle 6 Achsen in Report 1 (13 Bytes)
                        if len(packet) >= 13:
                            rx, ry, rz = struct.unpack("<hhh", bytes(packet[7:13]))
                            raw_state["pitch"] = rx
                            raw_state["roll"] = ry
                            raw_state["yaw"] = rz

                    # Report 2: Rotation (Pitch, Roll, Yaw) als 3x signed 16-bit int
                    elif report_id == 2 and len(packet) >= 7:
                        rx, ry, rz = struct.unpack("<hhh", bytes(packet[1:7]))
                        raw_state["pitch"] = rx
                        raw_state["roll"] = ry
                        raw_state["yaw"] = rz

                    # Report 3: Buttons (Bitmaske)
                    elif report_id == 3 and len(packet) >= 2:
                        btn_mask = packet[1]
                        raw_state["buttons"] = [
                            1 if (btn_mask & 1) else 0,
                            1 if (btn_mask & 2) else 0,
                        ]

                if updated:
                    axes_data = {
                        "x": self._apply_deadzone(raw_state["x"]),
                        "y": self._apply_deadzone(raw_state["y"]),
                        "z": self._apply_deadzone(raw_state["z"]),
                        "pitch": self._apply_deadzone(raw_state["pitch"]),
                        "roll": self._apply_deadzone(raw_state["roll"]),
                        "yaw": self._apply_deadzone(raw_state["yaw"]),
                        "buttons": raw_state["buttons"],
                        "timestamp": round(time.time(), 3),
                    }
                    self.data_updated.emit(axes_data)

                # Mit ~40 Hz in JSON schreiben
                now = time.time()
                if now - last_write >= 0.025 and self.output_path:
                    axes_data = {
                        "x": self._apply_deadzone(raw_state["x"]),
                        "y": self._apply_deadzone(raw_state["y"]),
                        "z": self._apply_deadzone(raw_state["z"]),
                        "pitch": self._apply_deadzone(raw_state["pitch"]),
                        "roll": self._apply_deadzone(raw_state["roll"]),
                        "yaw": self._apply_deadzone(raw_state["yaw"]),
                        "buttons": raw_state["buttons"],
                        "timestamp": round(now, 3),
                    }
                    self._write_atomic_json(axes_data)
                    last_write = now

                time.sleep(0.005)
            except Exception as e:
                self.status_changed.emit(f"Verbindung getrennt ({e}) – Reconnect...", False)
                try:
                    dev_handle.close()
                except Exception:
                    pass
                dev_handle = None
                time.sleep(1.0)

        if dev_handle is not None:
            try:
                dev_handle.close()
            except Exception:
                pass

    def _write_atomic_json(self, data: dict):
        try:
            target = Path(self.output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = target.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_file, target)
        except Exception:
            pass

    def stop(self):
        self.running = False
        self.wait(1000)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scrap Mechanic – SpaceMouse 6-DOF Bridge")
        self.resize(520, 520)
        self._apply_dark_theme()

        config = self._load_config()
        default_json = config.get(
            "output_path",
            str(Path(__file__).parent / "spacemouse_data.json"),
        )
        default_deadzone = config.get("deadzone", 0.03)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        # Status-Badge
        self.status_label = QLabel("Initialisiere SpaceMouse...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(
            "background-color: #2d2d2d; color: #ffaa00; padding: 8px; "
            "border-radius: 12px; font-weight: bold;"
        )
        layout.addWidget(self.status_label)

        # 6 Achsen-Balken
        axes_frame = QFrame()
        axes_frame.setStyleSheet("background-color: #1e1e1e; border-radius: 10px; padding: 8px;")
        axes_layout = QVBoxLayout(axes_frame)
        axes_layout.setSpacing(8)

        self.bars: dict[str, BipolarAxisBar] = {}
        colors = ["#00d2ff", "#00d2ff", "#00d2ff", "#ff7b54", "#ff7b54", "#ff7b54"]
        for (key, title), col in zip(AXES, colors):
            bar = BipolarAxisBar(title, accent_color=col)
            self.bars[key] = bar
            axes_layout.addWidget(bar)

        layout.addWidget(axes_frame)

        # Deadzone & Export-Pfad Einstellungen
        settings_frame = QFrame()
        settings_frame.setStyleSheet("background-color: #1e1e1e; border-radius: 10px; padding: 10px;")
        settings_layout = QVBoxLayout(settings_frame)

        dz_row = QHBoxLayout()
        dz_label = QLabel("Totzone (Deadzone):")
        self.dz_spin = QDoubleSpinBox()
        self.dz_spin.setRange(0.0, 0.50)
        self.dz_spin.setSingleStep(0.01)
        self.dz_spin.setValue(default_deadzone)
        self.dz_spin.valueChanged.connect(self._on_settings_changed)
        dz_row.addWidget(dz_label)
        dz_row.addStretch()
        dz_row.addWidget(self.dz_spin)
        settings_layout.addLayout(dz_row)

        path_label = QLabel("Ziel-JSON im Scrap Mechanic Mod-Ordner:")
        settings_layout.addWidget(path_label)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(default_json)
        self.path_edit.textChanged.connect(self._on_settings_changed)
        browse_btn = QPushButton("Durchsuchen...")
        browse_btn.clicked.connect(self._browse_path)
        path_row.addWidget(self.path_edit)
        path_row.addWidget(browse_btn)
        settings_layout.addLayout(path_row)

        layout.addWidget(settings_frame)

        # Worker starten
        self.worker = SpaceMouseWorker(self.path_edit.text(), self.dz_spin.value())
        self.worker.data_updated.connect(self._update_ui)
        self.worker.status_changed.connect(self._update_status)
        self.worker.start()

    def _apply_dark_theme(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #141414;
                color: #e6e6e6;
                font-family: 'Segoe UI', sans-serif;
            }
            QLineEdit, QDoubleSpinBox {
                background-color: #262626;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                padding: 5px 8px;
                color: #ffffff;
            }
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #198ae0;
            }
            """
        )

    def _browse_path(self):
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Speicherort für spacemouse_data.json wählen",
            self.path_edit.text(),
            "JSON Dateien (*.json)",
        )
        if file_path:
            self.path_edit.setText(file_path)

    def _on_settings_changed(self):
        path = self.path_edit.text().strip()
        dz = self.dz_spin.value()
        self.worker.update_settings(path, dz)
        self._save_config({"output_path": path, "deadzone": dz})

    def _update_ui(self, data: dict):
        for key, bar in self.bars.items():
            if key in data:
                bar.set_value(data[key])

    def _update_status(self, text: str, ok: bool):
        color = "#2ecc71" if ok else "#ff6b6b"
        bg = "#1b3324" if ok else "#381e1e"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"background-color: {bg}; color: {color}; padding: 8px; "
            "border-radius: 12px; font-weight: bold;"
        )

    def _load_config(self) -> dict:
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_config(self, cfg: dict):
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass

    def closeEvent(self, event):
        self.worker.stop()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())