import json
import os
import ctypes
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
os.environ["SDL_HINT_JOYSTICK_HIDAPI"] = "0"
os.environ["SDL_HINT_XINPUT_ENABLED"] = "1"

from PyQt6.QtCore import QPointF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QStyle,
    QStyleOptionSpinBox,
    QTableWidget,
    QTableWidgetItem,
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

import updater
from language import LANGUAGES, detect_system_language, set_language, tr

__version__ = "1.0"
GITHUB_REPO = "Leontierchen/Any-Controller-to-json-Readout"
GCDB_RAW_URL = "https://raw.githubusercontent.com/mdqinc/SDL_GameControllerDB/master/gamecontrollerdb.txt"


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(filename: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / filename
        if bundled.exists():
            return bundled
    return get_app_dir() / filename


APP_DIR = get_app_dir()
DEFAULT_OUTPUT_JSON = APP_DIR / "controller_data.json"
CONFIG_FILE = APP_DIR / "bridge_config.json"
GCDB_FILE = APP_DIR / "gamecontrollerdb.txt"
ICON_FILE = get_resource_path("Controller.ico")

if GCDB_FILE.exists():
    os.environ["SDL_GAMECONTROLLERCONFIG_FILE"] = str(GCDB_FILE)

SPACEMOUSE_VENDORS = (0x256F, 0x046D)
SPACEMOUSE_AXES = ["x", "y", "z", "pitch", "roll", "yaw"]

SIDEPANEL_VENDORS = (0x0738, 0x046D)
SIDEPANEL_PIDS = (0x2218,)


def sanitize_key(name: str, fallback: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned.strip("_")
    return cleaned if cleaned else fallback


def format_button_label(btn_key: str) -> str:
    if btn_key.startswith("btn_") and btn_key[4:].isdigit():
        return "B" + btn_key[4:]
    if btn_key.startswith("ax_btn_"):
        return "A" + btn_key[7:]
    if btn_key.startswith("hat_"):
        parts = btn_key.split("_")
        if len(parts) == 3:
            arrows = {"up": "↑", "down": "↓", "left": "←", "right": "→"}
            return f"H{parts[1]}{arrows.get(parts[2], parts[2])}"
    short_names = {
        "leftshoulder": "LB",
        "rightshoulder": "RB",
        "leftstick": "L3",
        "rightstick": "R3",
        "dpup": "D↑",
        "dpdown": "D↓",
        "dpleft": "D←",
        "dpright": "D→",
        "guide": "HOME",
        "misc1": "MISC",
        "paddle1": "P1",
        "paddle2": "P2",
        "paddle3": "P3",
        "paddle4": "P4",
        "touchpad": "PAD",
    }
    return short_names.get(btn_key, btn_key.upper()[:6])


def parse_sdl_gamecontroller_db(db_path: Path) -> dict[str, dict]:
    """Parst gamecontrollerdb.txt in ein Lookup-Dictionary nach normalisierter Windows-GUID."""
    db: dict[str, dict] = {}
    if not db_path.exists():
        return db

    try:
        lines = db_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return db

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "platform:Windows" not in line and "platform:" in line:
            continue

        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) < 3:
            continue

        guid = parts[0].lower()
        db_name = parts[1]
        bindings: dict[str, tuple[str, str]] = {}

        for token in parts[2:]:
            if ":" not in token:
                continue
            target, source = token.split(":", 1)
            target = target.strip()
            source = source.strip()
            if target == "platform":
                continue

            if source.startswith("b") and source[1:].isdigit():
                src_key = f"btn_{int(source[1:]) + 1}"
                bindings[target] = ("button", src_key)
            elif source.startswith("a") and source[1:].isdigit():
                src_key = f"axis_{int(source[1:])}"
                bindings[target] = ("axis", src_key)
            elif source.startswith("+a") and source[2:].isdigit():
                src_key = f"axis_{int(source[2:])}"
                bindings[target] = ("axis_pos", src_key)
            elif source.startswith("-a") and source[2:].isdigit():
                src_key = f"axis_{int(source[2:])}"
                bindings[target] = ("axis_neg", src_key)
            elif source.startswith("h") and "." in source:
                h_part, mask_part = source[1:].split(".", 1)
                if h_part.isdigit() and mask_part.isdigit():
                    h_idx = int(h_part)
                    mask = int(mask_part)
                    dir_map = {1: "up", 2: "right", 4: "down", 8: "left"}
                    if mask in dir_map:
                        bindings[target] = ("button", f"hat_{h_idx}_{dir_map[mask]}")

        if bindings:
            db[guid] = {"name": db_name, "bindings": bindings}

    return db


class GameControllerDBSyncWorker(QThread):
    db_updated = pyqtSignal(dict, str, str, int)

    def __init__(self, saved_etag: str = ""):
        super().__init__()
        self.saved_etag = saved_etag

    def run(self):
        local_db = parse_sdl_gamecontroller_db(GCDB_FILE)
        req = urllib.request.Request(
            GCDB_RAW_URL,
            headers={"User-Agent": "Universal Json ControllerBridge"},
        )
        if self.saved_etag and GCDB_FILE.exists():
            req.add_header("If-None-Match", self.saved_etag)

        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                new_etag = resp.headers.get("ETag", "").strip()
                content = resp.read().decode("utf-8", errors="replace")
                if len(content) > 10000 and "platform:" in content:
                    tmp_file = GCDB_FILE.with_suffix(".tmp")
                    tmp_file.write_text(content, encoding="utf-8")
                    tmp_file.replace(GCDB_FILE)
                    os.environ["SDL_GAMECONTROLLERCONFIG_FILE"] = str(GCDB_FILE)
                    new_db = parse_sdl_gamecontroller_db(GCDB_FILE)
                    self.db_updated.emit(new_db, new_etag, "gcdb_updated", len(new_db))
                else:
                    self.db_updated.emit(local_db, self.saved_etag, "gcdb_local", len(local_db))
        except urllib.error.HTTPError as e:
            if e.code == 304:
                self.db_updated.emit(local_db, self.saved_etag, "gcdb_up_to_date", len(local_db))
            else:
                self.db_updated.emit(local_db, self.saved_etag, "gcdb_offline_local", len(local_db))
        except Exception:
            self.db_updated.emit(local_db, self.saved_etag, "gcdb_offline_local", len(local_db))


class UpdateCheckWorker(QThread):
    result_ready = pyqtSignal(object, bool)

    def __init__(self, manual: bool = False):
        super().__init__()
        self.manual = manual

    def run(self):
        update_info = updater.check_for_updates(GITHUB_REPO, __version__)
        self.result_ready.emit(update_info, self.manual)


class UpdateDownloadWorker(QThread):
    progress_changed = pyqtSignal(int)
    failed = pyqtSignal()

    def __init__(self, update_info: dict):
        super().__init__()
        self.update_info = update_info

    def run(self):
        ok = updater.download_and_install(self.update_info, progress_callback=self.progress_changed.emit)
        if not ok:
            self.failed.emit()


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ArrowDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox mit exakten Button-Hitboxen und selbst gezeichneten Hoch/Runter-Pfeilen."""

    def paintEvent(self, event):
        super().paintEvent(event)
        opt = QStyleOptionSpinBox()
        self.initStyleOption(opt)
        style = self.style()

        up_rect = style.subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            opt,
            QStyle.SubControl.SC_SpinBoxUp,
            self,
        )
        down_rect = style.subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            opt,
            QStyle.SubControl.SC_SpinBoxDown,
            self,
        )

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e6e6e6") if self.isEnabled() else QColor("#666666"))

        if up_rect.isValid():
            cx = up_rect.center().x() + 0.5
            cy = up_rect.center().y() + 0.5
            up_poly = QPolygonF(
                [
                    QPointF(cx, cy - 2.5),
                    QPointF(cx - 4.0, cy + 2.5),
                    QPointF(cx + 4.0, cy + 2.5),
                ]
            )
            painter.drawPolygon(up_poly)

        if down_rect.isValid():
            cx = down_rect.center().x() + 0.5
            cy = down_rect.center().y() + 0.5
            down_poly = QPolygonF(
                [
                    QPointF(cx - 4.0, cy - 2.5),
                    QPointF(cx + 4.0, cy - 2.5),
                    QPointF(cx, cy + 2.5),
                ]
            )
            painter.drawPolygon(down_poly)

        painter.end()


class BipolarAxisBar(QWidget):
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


class CalibrationWizardDialog(QDialog):
    def __init__(self, cid: str, device_name: str, existing_profile: dict | None = None, parent=None):
        super().__init__(parent)
        self.cid = cid
        self.device_name = device_name
        self.setWindowTitle(tr("cal_win_title", device=device_name))
        self.resize(780, 640)
        self.setStyleSheet(
            """
            QDialog, QWidget {
                background-color: #141418;
                color: #e6e6e6;
                font-family: 'Segoe UI', sans-serif;
            }
            QFrame#StepBox {
                background-color: #1c1c22;
                border: 1px solid #2e2e38;
                border-radius: 10px;
            }
            QPushButton {
                background-color: #262630;
                color: #e6e6e6;
                border: 1px solid #3a3a46;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #32323f;
                border-color: #00d2ff;
            }
            QPushButton#PrimaryBtn {
                background-color: #00a8cc;
                color: #111111;
                border: none;
            }
            QPushButton#PrimaryBtn:hover {
                background-color: #00d2ff;
            }
            QPushButton#DangerBtn {
                background-color: #3a1d22;
                color: #ff6b6b;
                border: 1px solid #6b2c35;
            }
            QTableWidget {
                background-color: #18181d;
                border: 1px solid #2e2e38;
                border-radius: 8px;
                gridline-color: #282832;
            }
            QHeaderView::section {
                background-color: #202028;
                color: #00d2ff;
                font-weight: bold;
                padding: 6px;
                border: none;
            }
            QLineEdit {
                background-color: #22222a;
                color: #ffffff;
                border: 1px solid #3c3c4a;
                border-radius: 4px;
                padding: 3px 6px;
            }
            """
        )

        self.baseline_axes: dict[str, float] = dict(existing_profile.get("baseline_axes", {})) if existing_profile else {}
        self.baseline_buttons: dict[str, int] = dict(existing_profile.get("baseline_buttons", {})) if existing_profile else {}

        self.entries: list[dict] = []
        if existing_profile and "mappings" in existing_profile:
            for m in existing_profile["mappings"]:
                self.entries.append(dict(m))

        self.latest_raw_axes: dict[str, float] = {}
        self.latest_raw_buttons: dict[str, int] = {}

        self.phase = "idle"
        self.baseline_samples_axes: dict[str, list[float]] = {}
        self.baseline_samples_btns: dict[str, list[int]] = {}
        self.baseline_start = 0.0

        self.active_candidate: dict | None = None
        self.release_started_at: float | None = None
        self.prompt_open = False
        self.result_profile: dict | None = None

        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start(20)

        if not self.baseline_axes and not self.entries:
            self.start_baseline_capture()
        else:
            self.phase = "listening"
            self.lbl_instruction.setText(tr("cal_instr_ready"))
            self.progress_bar.setValue(100)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        step_box = QFrame()
        step_box.setObjectName("StepBox")
        step_layout = QVBoxLayout(step_box)
        step_layout.setContentsMargins(14, 12, 14, 12)
        step_layout.setSpacing(8)

        title = QLabel(tr("cal_header", device=self.device_name))
        title.setStyleSheet("color: #00d2ff; font-size: 14px; font-weight: bold;")
        step_layout.addWidget(title)

        self.lbl_instruction = QLabel(tr("cal_instr_init"))
        self.lbl_instruction.setWordWrap(True)
        self.lbl_instruction.setStyleSheet("font-size: 12px; color: #e0e0e0;")
        step_layout.addWidget(self.lbl_instruction)

        self.lbl_live_hint = QLabel(tr("cal_hint_waiting"))
        self.lbl_live_hint.setStyleSheet("font-size: 11px; color: #9a9aa8; font-family: 'Consolas', monospace;")
        step_layout.addWidget(self.lbl_live_hint)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(10)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: #262630; border-radius: 5px; } "
            "QProgressBar::chunk { background-color: #00d2ff; border-radius: 5px; }"
        )
        step_layout.addWidget(self.progress_bar)

        ctrl_row = QHBoxLayout()
        btn_rebaseline = QPushButton(tr("cal_btn_rebaseline"))
        btn_rebaseline.clicked.connect(self.start_baseline_capture)

        btn_import_all_raw = QPushButton(tr("cal_btn_import_all"))
        btn_import_all_raw.clicked.connect(self._import_all_raw_inputs)

        btn_clear_all = QPushButton(tr("cal_btn_clear"))
        btn_clear_all.setObjectName("DangerBtn")
        btn_clear_all.clicked.connect(self._clear_entries)

        ctrl_row.addWidget(btn_rebaseline)
        ctrl_row.addWidget(btn_import_all_raw)
        ctrl_row.addWidget(btn_clear_all)
        ctrl_row.addStretch()
        step_layout.addLayout(ctrl_row)

        layout.addWidget(step_box)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            [
                tr("cal_col_source"),
                tr("cal_col_type"),
                tr("cal_col_name"),
                tr("cal_col_cal"),
                "",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        self._rebuild_table()

        bottom_row = QHBoxLayout()
        btn_reset_default = QPushButton(tr("cal_btn_reset_default"))
        btn_reset_default.setObjectName("DangerBtn")
        btn_reset_default.clicked.connect(self._reset_to_uncalibrated)

        btn_cancel = QPushButton(tr("btn_cancel"))
        btn_cancel.clicked.connect(self.reject)

        btn_save = QPushButton(tr("cal_btn_save"))
        btn_save.setObjectName("PrimaryBtn")
        btn_save.clicked.connect(self._save_and_accept)

        bottom_row.addWidget(btn_reset_default)
        bottom_row.addStretch()
        bottom_row.addWidget(btn_cancel)
        bottom_row.addWidget(btn_save)
        layout.addLayout(bottom_row)

    def feed_raw_state(self, raw_axes: dict[str, float], raw_buttons: dict[str, int]):
        self.latest_raw_axes = dict(raw_axes)
        self.latest_raw_buttons = dict(raw_buttons)

    def start_baseline_capture(self):
        self.phase = "baseline"
        self.baseline_samples_axes = {}
        self.baseline_samples_btns = {}
        self.baseline_start = time.time()
        self.active_candidate = None
        self.release_started_at = None
        self.progress_bar.setValue(0)
        self.lbl_instruction.setText(tr("cal_instr_baseline"))

    def _import_all_raw_inputs(self):
        added = 0
        for a_key, rest_val in self.latest_raw_axes.items():
            already = any(e["source_type"] == "axis" and e["source_key"] == a_key for e in self.entries)
            if not already:
                self.entries.append(
                    {
                        "source_type": "axis",
                        "source_key": a_key,
                        "target_type": "axis",
                        "custom_name": a_key,
                        "rest": round(self.baseline_axes.get(a_key, rest_val), 4),
                        "min": -1.0,
                        "max": 1.0,
                        "target_val": 0.0,
                        "tolerance": 0.015,
                        "direction": 0,
                    }
                )
                added += 1

        for b_key in self.latest_raw_buttons.keys():
            already = any(e["source_type"] == "button" and e["source_key"] == b_key for e in self.entries)
            if not already:
                self.entries.append(
                    {
                        "source_type": "button",
                        "source_key": b_key,
                        "target_type": "button",
                        "custom_name": b_key,
                        "rest": 0.0,
                        "min": 0.0,
                        "max": 1.0,
                        "target_val": 1.0,
                        "tolerance": 0.015,
                        "direction": 0,
                    }
                )
                added += 1
        self._rebuild_table()
        self.lbl_live_hint.setText(tr("cal_hint_imported", added=added, total=len(self.entries)))

    def _on_tick(self):
        if self.prompt_open:
            return

        now = time.time()

        if self.phase == "baseline":
            for k, v in self.latest_raw_axes.items():
                self.baseline_samples_axes.setdefault(k, []).append(v)
            for k, v in self.latest_raw_buttons.items():
                self.baseline_samples_btns.setdefault(k, []).append(v)

            elapsed = now - self.baseline_start
            pct = min(100, int((elapsed / 1.2) * 100))
            self.progress_bar.setValue(pct)

            if elapsed >= 1.2:
                self.baseline_axes = {
                    k: round(sum(vals) / len(vals), 4)
                    for k, vals in self.baseline_samples_axes.items()
                    if vals
                }
                self.baseline_buttons = {
                    k: (1 if sum(vals) > len(vals) / 2 else 0)
                    for k, vals in self.baseline_samples_btns.items()
                    if vals
                }
                self.phase = "listening"
                self.lbl_instruction.setText(tr("cal_instr_step2"))
                self.lbl_live_hint.setText(tr("cal_hint_baseline_saved"))
            return

        if self.phase != "listening":
            return

        pressed_btns = []
        for b_key, val in self.latest_raw_buttons.items():
            rest_b = self.baseline_buttons.get(b_key, 0)
            if val != rest_b and val == 1:
                pressed_btns.append(b_key)

        moved_axes = []
        for a_key, val in self.latest_raw_axes.items():
            rest_a = self.baseline_axes.get(a_key, 0.0)
            delta = val - rest_a
            if abs(delta) > 0.015:
                moved_axes.append((a_key, val, rest_a, delta))

        if pressed_btns or moved_axes:
            self.release_started_at = None
            if self.active_candidate is None:
                self.active_candidate = {
                    "buttons": set(pressed_btns),
                    "axes": {
                        a_key: {
                            "rest": rest_a,
                            "min": min(rest_a, val),
                            "max": max(rest_a, val),
                            "samples": [val],
                        }
                        for a_key, val, rest_a, _ in moved_axes
                    },
                }
            else:
                self.active_candidate["buttons"].update(pressed_btns)
                for a_key, val, rest_a, _ in moved_axes:
                    if a_key not in self.active_candidate["axes"]:
                        self.active_candidate["axes"][a_key] = {
                            "rest": rest_a,
                            "min": min(rest_a, val),
                            "max": max(rest_a, val),
                            "samples": [val],
                        }
                    else:
                        info = self.active_candidate["axes"][a_key]
                        info["min"] = min(info["min"], val)
                        info["max"] = max(info["max"], val)
                        info["samples"].append(val)

            b_str = ", ".join(sorted(self.active_candidate["buttons"])) or "-"
            a_parts = [
                f"{k}({info['min']:+.3f}..{info['max']:+.3f})"
                for k, info in sorted(self.active_candidate["axes"].items())
            ]
            a_str = ", ".join(a_parts) or "-"
            self.lbl_live_hint.setText(tr("cal_hint_active", buttons=b_str, axes=a_str))
        else:
            if self.active_candidate is not None:
                if self.release_started_at is None:
                    self.release_started_at = now
                    self.lbl_live_hint.setText(tr("cal_hint_released"))
                elif (now - self.release_started_at) >= 0.45:
                    cand = self.active_candidate
                    self.active_candidate = None
                    self.release_started_at = None
                    self.lbl_live_hint.setText(tr("cal_hint_captured"))
                    self._evaluate_candidate(cand)

    def _add_or_update_mapping(self, mapping: dict):
        for idx, e in enumerate(self.entries):
            if e["source_type"] == mapping["source_type"] and e["source_key"] == mapping["source_key"]:
                if mapping["source_type"] == "button":
                    mapping["custom_name"] = e["custom_name"]
                    self.entries[idx] = mapping
                    self._rebuild_table()
                    return

                if mapping["source_type"] == "axis":
                    if e["target_type"] == "axis" and mapping["target_type"] == "axis":
                        mapping["custom_name"] = e["custom_name"]
                        self.entries[idx] = mapping
                        self._rebuild_table()
                        return

                    if e["target_type"] == "button" and mapping["target_type"] == "button":
                        existing_tval = float(e.get("target_val", 0.0))
                        new_tval = float(mapping.get("target_val", 0.0))
                        if abs(existing_tval - new_tval) <= 0.008:
                            mapping["custom_name"] = e["custom_name"]
                            self.entries[idx] = mapping
                            self._rebuild_table()
                            return

        self.entries.append(mapping)
        self._rebuild_table()

    def _compute_held_plateau(self, samples: list[float], rest: float) -> float:
        if not samples:
            return rest
        peak = max(samples, key=lambda x: abs(x - rest))
        near_peak = [s for s in samples if abs(s - peak) <= 0.015]
        if near_peak:
            return round(sum(near_peak) / len(near_peak), 4)
        return round(peak, 4)

    def _evaluate_candidate(self, cand: dict):
        btns = sorted(cand["buttons"])
        axes = cand["axes"]

        if btns and not axes:
            for b_key in btns:
                self._add_or_update_mapping(
                    {
                        "source_type": "button",
                        "source_key": b_key,
                        "target_type": "button",
                        "custom_name": b_key,
                        "rest": 0.0,
                        "min": 0.0,
                        "max": 1.0,
                        "target_val": 1.0,
                        "tolerance": 0.008,
                        "direction": 0,
                    }
                )
            return

        if axes and not btns:
            best_key = max(axes.keys(), key=lambda k: abs(axes[k]["max"] - axes[k]["min"]))
            info = axes[best_key]
            rest = info["rest"]
            pos_span = info["max"] - rest
            neg_span = rest - info["min"]

            if pos_span > 0.25 and neg_span > 0.25:
                self._add_or_update_mapping(
                    {
                        "source_type": "axis",
                        "source_key": best_key,
                        "target_type": "axis",
                        "custom_name": best_key,
                        "rest": round(rest, 4),
                        "min": round(info["min"], 4),
                        "max": round(info["max"], 4),
                        "target_val": 0.0,
                        "tolerance": 0.008,
                        "direction": 0,
                    }
                )
                return

            direction = 1 if pos_span >= neg_span else -1
            plateau_val = self._compute_held_plateau(info["samples"], rest)

            self.prompt_open = True
            box = QMessageBox(self)
            box.setWindowTitle(tr("cal_dlg_type_title"))
            box.setText(
                tr(
                    "cal_dlg_oneway_text",
                    axis=best_key,
                    rest=f"{rest:+.3f}",
                    peak=f"{plateau_val:+.3f}",
                )
            )
            btn_as_button = box.addButton(
                tr("cal_dlg_btn_at_val", val=f"{plateau_val:+.3f}"),
                QMessageBox.ButtonRole.AcceptRole,
            )
            btn_as_axis = box.addButton(
                tr("cal_dlg_btn_oneway_axis"),
                QMessageBox.ButtonRole.ActionRole,
            )
            box.addButton(tr("btn_ignore"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            self.prompt_open = False

            if clicked == btn_as_button:
                val_tag = f"{plateau_val:+.2f}".replace("+", "p").replace("-", "m").replace(".", "")
                default_name = f"ax_btn_{best_key.replace('axis_', '')}_{val_tag}"
                self._add_or_update_mapping(
                    {
                        "source_type": "axis",
                        "source_key": best_key,
                        "target_type": "button",
                        "custom_name": default_name,
                        "rest": round(rest, 4),
                        "min": round(info["min"], 4),
                        "max": round(info["max"], 4),
                        "target_val": plateau_val,
                        "tolerance": 0.008,
                        "direction": direction,
                    }
                )
            elif clicked == btn_as_axis:
                self._add_or_update_mapping(
                    {
                        "source_type": "axis",
                        "source_key": best_key,
                        "target_type": "axis",
                        "custom_name": best_key,
                        "rest": round(rest, 4),
                        "min": round(info["min"], 4),
                        "max": round(info["max"], 4),
                        "target_val": plateau_val,
                        "tolerance": 0.008,
                        "direction": direction,
                    }
                )
            return

        if btns and axes:
            b_key = btns[0]
            a_key = max(axes.keys(), key=lambda k: abs(axes[k]["max"] - axes[k]["min"]))
            info = axes[a_key]
            rest = info["rest"]
            pos_span = info["max"] - rest
            neg_span = rest - info["min"]
            direction = 0 if (pos_span > 0.25 and neg_span > 0.25) else (1 if pos_span >= neg_span else -1)
            plateau_val = self._compute_held_plateau(info["samples"], rest)

            self.prompt_open = True
            box = QMessageBox(self)
            box.setWindowTitle(tr("cal_dlg_ambig_title"))
            box.setText(
                tr(
                    "cal_dlg_ambig_text",
                    button=b_key,
                    axis=a_key,
                    val=f"{plateau_val:+.3f}",
                )
            )
            btn_use_btn = box.addButton(tr("cal_dlg_use_btn", button=b_key), QMessageBox.ButtonRole.AcceptRole)
            btn_use_ax = box.addButton(tr("cal_dlg_use_ax", axis=a_key), QMessageBox.ButtonRole.ActionRole)
            box.addButton(tr("btn_ignore"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            self.prompt_open = False

            if clicked == btn_use_btn:
                self._add_or_update_mapping(
                    {
                        "source_type": "button",
                        "source_key": b_key,
                        "target_type": "button",
                        "custom_name": b_key,
                        "rest": 0.0,
                        "min": 0.0,
                        "max": 1.0,
                        "target_val": 1.0,
                        "tolerance": 0.008,
                        "direction": 0,
                    }
                )
            elif clicked == btn_use_ax:
                self._add_or_update_mapping(
                    {
                        "source_type": "axis",
                        "source_key": a_key,
                        "target_type": "axis",
                        "custom_name": a_key,
                        "rest": round(rest, 4),
                        "min": round(info["min"], 4),
                        "max": round(info["max"], 4),
                        "target_val": plateau_val,
                        "tolerance": 0.008,
                        "direction": direction,
                    }
                )

    def _rebuild_table(self):
        self.table.setRowCount(0)
        for row_idx, entry in enumerate(self.entries):
            self.table.insertRow(row_idx)

            src_text = entry["source_key"]
            if entry["source_type"] == "axis" and entry["target_type"] == "button":
                tval = float(entry.get("target_val", 0.0))
                src_text += f" @ {tval:+.3f}"
            elif entry["source_type"] == "axis" and entry.get("direction", 0) != 0:
                src_text += tr("cal_one_way")
            elif entry["source_type"] == "axis":
                src_text += tr("cal_two_way")

            item_src = QTableWidgetItem(src_text)
            item_src.setFlags(item_src.flags() & ~Qt.ItemFlag.ItemIsEditable)

            type_label = tr("cal_type_button") if entry["target_type"] == "button" else tr("cal_type_axis")
            item_type = QTableWidgetItem(type_label)
            item_type.setFlags(item_type.flags() & ~Qt.ItemFlag.ItemIsEditable)

            name_edit = QLineEdit(entry["custom_name"])
            name_edit.textChanged.connect(lambda txt, idx=row_idx: self._on_name_edited(idx, txt))

            if entry["source_type"] == "axis" and entry["target_type"] == "button":
                tval = float(entry.get("target_val", 0.0))
                tol = float(entry.get("tolerance", 0.008))
                cal_info = tr("cal_info_target", target=f"{tval:+.3f}", tol=f"{tol:.3f}")
            elif entry["source_type"] == "axis":
                cal_info = tr(
                    "cal_info_range",
                    rest=f"{entry['rest']:+.2f}",
                    min=f"{entry['min']:+.2f}",
                    max=f"{entry['max']:+.2f}",
                )
            else:
                cal_info = tr("cal_info_digital")

            item_cal = QTableWidgetItem(cal_info)
            item_cal.setFlags(item_cal.flags() & ~Qt.ItemFlag.ItemIsEditable)

            btn_del = QPushButton("✕")
            btn_del.setObjectName("DangerBtn")
            btn_del.setFixedWidth(32)
            btn_del.clicked.connect(lambda _, idx=row_idx: self._remove_entry(idx))

            self.table.setItem(row_idx, 0, item_src)
            self.table.setItem(row_idx, 1, item_type)
            self.table.setCellWidget(row_idx, 2, name_edit)
            self.table.setItem(row_idx, 3, item_cal)
            self.table.setCellWidget(row_idx, 4, btn_del)

    def _on_name_edited(self, idx: int, new_text: str):
        if 0 <= idx < len(self.entries):
            self.entries[idx]["custom_name"] = new_text

    def _remove_entry(self, idx: int):
        if 0 <= idx < len(self.entries):
            self.entries.pop(idx)
            self._rebuild_table()

    def _clear_entries(self):
        self.entries.clear()
        self._rebuild_table()

    def _reset_to_uncalibrated(self):
        self.result_profile = None
        self.timer.stop()
        self.accept()

    def _save_and_accept(self):
        used_names = set()
        cleaned_entries = []
        for idx, e in enumerate(self.entries):
            item = dict(e)
            base_name = sanitize_key(item["custom_name"], f"input_{idx + 1}")
            final_name = base_name
            counter = 2
            while final_name in used_names:
                final_name = f"{base_name}_{counter}"
                counter += 1
            used_names.add(final_name)
            item["custom_name"] = final_name
            cleaned_entries.append(item)

        self.result_profile = {
            "baseline_axes": self.baseline_axes,
            "baseline_buttons": self.baseline_buttons,
            "mappings": cleaned_entries,
        }
        self.timer.stop()
        self.accept()

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)


class MultiControllerWorker(QThread):
    state_updated = pyqtSignal(dict)
    status_updated = pyqtSignal(str)

    def __init__(
        self,
        output_path: Path = DEFAULT_OUTPUT_JSON,
        default_deadzone: float = 0.04,
        deadzones_map: dict[str, float] | None = None,
        write_interval: float = 0.045,
        enabled_map: dict[str, bool] | None = None,
        profiles_map: dict[str, dict] | None = None,
        gcdb_map: dict[str, dict] | None = None,
    ):
        super().__init__()
        self.output_path = Path(output_path)
        self.default_deadzone = max(0.0, min(0.5, float(default_deadzone)))
        self.deadzones_map: dict[str, float] = (
            {str(k): max(0.0, min(0.5, float(v))) for k, v in deadzones_map.items()}
            if deadzones_map
            else {}
        )
        self.write_interval = max(0.005, float(write_interval))
        self.enabled_map: dict[str, bool] = dict(enabled_map) if enabled_map else {}
        self.profiles_map: dict[str, dict] = dict(profiles_map) if profiles_map else {}
        self.gcdb_map: dict[str, dict] = dict(gcdb_map) if gcdb_map else {}
        self.running = True
        self.force_rewrite = False

    def set_output_path(self, output_path: Path):
        self.output_path = Path(output_path)
        self.force_rewrite = True

    def set_device_deadzone(self, device_key: str, deadzone: float):
        self.deadzones_map[device_key] = round(max(0.0, min(0.5, float(deadzone))), 2)
        self.force_rewrite = True

    def get_device_deadzone(self, device_key: str) -> float:
        return self.deadzones_map.get(device_key, self.default_deadzone)

    def set_gcdb_map(self, gcdb_map: dict[str, dict]):
        self.gcdb_map = dict(gcdb_map)
        self.force_rewrite = True

    def set_controller_enabled(self, cid: str, enabled: bool):
        self.enabled_map[cid] = enabled
        self.force_rewrite = True

    def is_controller_enabled(self, cid: str) -> bool:
        return self.enabled_map.get(cid, True)

    def set_device_profile(self, device_key: str, profile: dict | None):
        if profile is None:
            self.profiles_map.pop(device_key, None)
        else:
            self.profiles_map[device_key] = profile
        self.force_rewrite = True

    @staticmethod
    def _apply_deadzone(val: float, deadzone: float) -> float:
        val = max(-1.0, min(1.0, float(val)))
        if abs(val) < deadzone:
            return 0.0
        sign = 1.0 if val > 0 else -1.0
        scaled = (abs(val) - deadzone) / (1.0 - deadzone)
        return round(max(-1.0, min(1.0, sign * scaled)), 4)

    def _apply_gcdb_entry(
        self,
        gcdb_entry: dict,
        raw_axes: dict[str, float],
        raw_buttons: dict[str, int],
        deadzone: float,
    ) -> tuple[dict, dict]:
        bindings = gcdb_entry.get("bindings", {})
        out_axes: dict[str, float] = {}
        out_buttons: dict[str, int] = {}
        used_axes: set[str] = set()
        used_buttons: set[str] = set()

        for target_name, (src_kind, src_key) in bindings.items():
            if src_kind == "button":
                out_buttons[target_name] = 1 if raw_buttons.get(src_key, 0) else 0
                used_buttons.add(src_key)
            elif src_kind == "axis":
                raw_v = raw_axes.get(src_key, 0.0)
                if "trigger" in target_name:
                    norm = max(0.0, min(1.0, (raw_v + 1.0) / 2.0)) if raw_v < -0.05 else max(0.0, min(1.0, raw_v))
                    out_axes[target_name] = self._apply_deadzone(norm, deadzone)
                else:
                    out_axes[target_name] = self._apply_deadzone(raw_v, deadzone)
                used_axes.add(src_key)
            elif src_kind == "axis_pos":
                raw_v = raw_axes.get(src_key, 0.0)
                out_axes[target_name] = self._apply_deadzone(max(0.0, raw_v), deadzone)
                used_axes.add(src_key)
            elif src_kind == "axis_neg":
                raw_v = raw_axes.get(src_key, 0.0)
                out_axes[target_name] = self._apply_deadzone(max(0.0, -raw_v), deadzone)
                used_axes.add(src_key)

        for a_key, val in raw_axes.items():
            if a_key not in used_axes and not a_key.startswith("hat_"):
                out_axes[a_key] = self._apply_deadzone(val, deadzone)

        for b_key, val in raw_buttons.items():
            if b_key not in used_buttons and not b_key.startswith("hat_"):
                out_buttons[b_key] = val

        return out_axes, out_buttons

    def _apply_profile(
        self,
        device_key: str,
        raw_axes: dict[str, float],
        raw_buttons: dict[str, int],
        guid: str = "",
    ) -> tuple[dict, dict, str]:
        deadzone = self.get_device_deadzone(device_key)
        profile = self.profiles_map.get(device_key)
        if profile and profile.get("mappings"):
            out_axes: dict[str, float] = {}
            out_buttons: dict[str, int] = {}

            axis_button_mappings_by_src: dict[str, list[dict]] = {}
            for m in profile["mappings"]:
                if m["source_type"] == "axis" and m["target_type"] == "button":
                    axis_button_mappings_by_src.setdefault(m["source_key"], []).append(m)

            active_axis_button_names: set[str] = set()
            for src_key, btn_list in axis_button_mappings_by_src.items():
                raw_val = raw_axes.get(src_key, 0.0)
                best_match = None
                best_dist = 999.0
                for m in btn_list:
                    rest = float(m.get("rest", 0.0))
                    if abs(raw_val - rest) < 0.008:
                        continue
                    target_val = float(m.get("target_val", 0.0))
                    tol = float(m.get("tolerance", 0.008))
                    dist = abs(raw_val - target_val)
                    if dist <= tol and dist < best_dist:
                        best_dist = dist
                        best_match = m
                if best_match is not None:
                    active_axis_button_names.add(best_match["custom_name"])

            for m in profile["mappings"]:
                src_type = m["source_type"]
                src_key = m["source_key"]
                tgt_type = m["target_type"]
                name = m["custom_name"]

                if src_type == "button":
                    pressed = 1 if raw_buttons.get(src_key, 0) else 0
                    if tgt_type == "button":
                        out_buttons[name] = pressed
                    else:
                        out_axes[name] = 1.0 if pressed else 0.0

                elif src_type == "axis":
                    if tgt_type == "button":
                        out_buttons[name] = 1 if name in active_axis_button_names else 0
                    else:
                        raw_val = raw_axes.get(src_key, m.get("rest", 0.0))
                        rest = float(m.get("rest", 0.0))
                        min_v = float(m.get("min", -1.0))
                        max_v = float(m.get("max", 1.0))
                        direction = int(m.get("direction", 0))

                        if direction == 1:
                            span = max(0.05, max_v - rest)
                            norm = max(0.0, min(1.0, (raw_val - rest) / span))
                            out_axes[name] = self._apply_deadzone(norm, deadzone)
                        elif direction == -1:
                            span = max(0.05, rest - min_v)
                            norm = max(0.0, min(1.0, (rest - raw_val) / span))
                            out_axes[name] = self._apply_deadzone(norm, deadzone)
                        else:
                            if raw_val >= rest:
                                span = max(0.05, max_v - rest)
                                norm = (raw_val - rest) / span
                            else:
                                span = max(0.05, rest - min_v)
                                norm = -(rest - raw_val) / span
                            out_axes[name] = self._apply_deadzone(norm, deadzone)

            return out_axes, out_buttons, "custom"

        if guid and guid.lower() in self.gcdb_map:
            ax, bt = self._apply_gcdb_entry(self.gcdb_map[guid.lower()], raw_axes, raw_buttons, deadzone)
            return ax, bt, "gcdb"

        cal_axes = {k: self._apply_deadzone(v, deadzone) for k, v in raw_axes.items()}
        cal_buttons = dict(raw_buttons)
        return cal_axes, cal_buttons, "none"

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

    def _scan_sidepanels(self) -> list[dict]:
        found = []
        if hid is None:
            return found
        seen_paths = set()
        for dev in hid.enumerate():
            vid = dev["vendor_id"]
            pid = dev["product_id"]
            pname = (dev.get("product_string") or "").lower()
            is_sidepanel = (vid in SIDEPANEL_VENDORS and pid in SIDEPANEL_PIDS) or ("side panel" in pname)
            if is_sidepanel and dev.get("usage_page") == 1:
                path = dev["path"]
                if path not in seen_paths:
                    seen_paths.add(path)
                    found.append(dev)
        return found

    @staticmethod
    def _norm_u8(val: int) -> float:
        if val == 128:
            return 0.0
        if val > 128:
            return round((val - 128) / 127.0, 4)
        return round((val - 128) / 128.0, 4)

    def _decode_sidepanel_packet(self, pkt: list[int], raw_axes: dict[str, float], raw_buttons: dict[str, int]):
        if len(pkt) < 10:
            return

        btn_mask = pkt[0] | (pkt[1] << 8) | (pkt[2] << 16) | (pkt[3] << 24)
        for b_idx in range(28):
            raw_buttons[f"btn_{b_idx + 1}"] = 1 if (btn_mask & (1 << b_idx)) else 0

        # Modus 1 (LED Rot)
        raw_axes["axis_0"] = self._norm_u8(pkt[8])  # Stick X1
        raw_axes["axis_1"] = self._norm_u8(pkt[9])  # Stick Y1
        raw_axes["axis_2"] = self._norm_u8(pkt[7])  # Stick Z1 (Drehung)

        # Modus 2 (LED Blau - Umschalter am Stick-Kopf)
        raw_axes["axis_3"] = self._norm_u8(pkt[4])  # Stick X2
        raw_axes["axis_4"] = self._norm_u8(pkt[5])  # Stick Y2
        raw_axes["axis_5"] = self._norm_u8(pkt[6])  # Stick Z2 (Drehung)

    def run(self):
        if pygame is not None:
            pygame.init()
            pygame.joystick.init()

        sm_handles = {}
        sp_handles = {}
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

                    current_sp = self._scan_sidepanels()
                    active_sp_ids = set()
                    for idx, info in enumerate(current_sp):
                        cid = f"sidepanel_{idx}"
                        active_sp_ids.add(cid)
                        if cid not in sp_handles:
                            try:
                                h = hid.device()
                                h.open_path(info["path"])
                                h.set_nonblocking(True)
                                name = info.get("product_string") or f"Saitek Side Panel #{idx + 1}"
                                sp_handles[cid] = {
                                    "handle": h,
                                    "name": name,
                                    "raw": {f"axis_{a}": 0.0 for a in range(6)},
                                    "buttons": {f"btn_{b}": 0 for b in range(1, 29)},
                                }
                            except Exception:
                                pass

                    for cid in list(sp_handles.keys()):
                        if cid not in active_sp_ids:
                            try:
                                sp_handles[cid]["handle"].close()
                            except Exception:
                                pass
                            del sp_handles[cid]

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
                            jlow = jname.lower()
                            if (
                                "3dconnexion" in jlow
                                or "spacemouse" in jlow
                                or "6-achse 2-taste" in jlow
                                or "6 axis 2 buttons" in jlow
                            ):
                                continue
                            if sp_handles and ("side panel" in jlow or "control deck" in jlow):
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

                    raw_axes_copy = dict(raw)
                    raw_btns_copy = dict(btns)
                    dev_name = entry["name"]
                    cal_axes, cal_buttons, mode = self._apply_profile(dev_name, raw_axes_copy, raw_btns_copy)

                    all_controllers[cid] = {
                        "id": cid,
                        "name": dev_name,
                        "profile_mode": mode,
                        "deadzone": self.get_device_deadzone(dev_name),
                        "raw_axes": raw_axes_copy,
                        "raw_buttons": raw_btns_copy,
                        "axes": cal_axes,
                        "buttons": cal_buttons,
                    }
                except Exception:
                    try:
                        h.close()
                    except Exception:
                        pass
                    del sm_handles[cid]

            for cid, entry in list(sp_handles.items()):
                h = entry["handle"]
                raw = entry["raw"]
                btns = entry["buttons"]
                try:
                    while True:
                        pkt = h.read(64)
                        if not pkt:
                            break
                        self._decode_sidepanel_packet(pkt, raw, btns)

                    raw_axes_copy = dict(raw)
                    raw_btns_copy = dict(btns)
                    dev_name = entry["name"]
                    cal_axes, cal_buttons, mode = self._apply_profile(dev_name, raw_axes_copy, raw_btns_copy)

                    all_controllers[cid] = {
                        "id": cid,
                        "name": dev_name,
                        "profile_mode": mode,
                        "deadzone": self.get_device_deadzone(dev_name),
                        "raw_axes": raw_axes_copy,
                        "raw_buttons": raw_btns_copy,
                        "axes": cal_axes,
                        "buttons": cal_buttons,
                    }
                except Exception:
                    try:
                        h.close()
                    except Exception:
                        pass
                    del sp_handles[cid]

            if pygame is not None:
                pygame.event.pump()
                for cid, joy in list(pg_joysticks.items()):
                    try:
                        raw_axes = {}
                        raw_buttons = {}

                        for b_idx in range(joy.get_numbuttons()):
                            raw_buttons[f"btn_{b_idx + 1}"] = 1 if joy.get_button(b_idx) else 0

                        for a_idx in range(joy.get_numaxes()):
                            raw_axes[f"axis_{a_idx}"] = round(float(joy.get_axis(a_idx)), 4)

                        for h_idx in range(joy.get_numhats()):
                            hx, hy = joy.get_hat(h_idx)
                            raw_axes[f"hat_{h_idx}_x"] = float(hx)
                            raw_axes[f"hat_{h_idx}_y"] = float(hy)
                            raw_buttons[f"hat_{h_idx}_up"] = 1 if hy > 0 else 0
                            raw_buttons[f"hat_{h_idx}_down"] = 1 if hy < 0 else 0
                            raw_buttons[f"hat_{h_idx}_left"] = 1 if hx < 0 else 0
                            raw_buttons[f"hat_{h_idx}_right"] = 1 if hx > 0 else 0

                        dev_name = joy.get_name()
                        guid = joy.get_guid() if hasattr(joy, "get_guid") else ""
                        cal_axes, cal_buttons, mode = self._apply_profile(
                            dev_name, raw_axes, raw_buttons, guid=guid
                        )

                        all_controllers[cid] = {
                            "id": cid,
                            "name": dev_name,
                            "profile_mode": mode,
                            "deadzone": self.get_device_deadzone(dev_name),
                            "raw_axes": raw_axes,
                            "raw_buttons": raw_buttons,
                            "axes": cal_axes,
                            "buttons": cal_buttons,
                        }
                    except Exception:
                        del pg_joysticks[cid]

            exported_controllers = {}
            for cid, cdata in all_controllers.items():
                if self.is_controller_enabled(cid):
                    exported_controllers[cid] = {
                        "id": cdata["id"],
                        "name": cdata["name"],
                        "axes": cdata["axes"],
                        "buttons": cdata["buttons"],
                    }

            self.state_updated.emit({"controllers": all_controllers})
            self.status_updated.emit(
                tr("status_devices", detected=len(all_controllers), active=len(exported_controllers))
            )

            if now - last_write >= self.write_interval or self.force_rewrite:
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
        for entry in sp_handles.values():
            try:
                entry["handle"].close()
            except Exception:
                pass
        if pygame is not None:
            pygame.quit()

    def _write_safe(self, payload_str: str) -> bool:
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "r+" if self.output_path.exists() else "w"
            with open(self.output_path, mode, encoding="utf-8") as f:
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
    toggled_export = pyqtSignal(str, bool)
    calibrate_requested = pyqtSignal(str)
    deadzone_changed = pyqtSignal(str, float)

    def __init__(
        self,
        cid: str,
        name: str,
        axes_keys: list[str],
        btn_keys: list[str],
        checked: bool = True,
        profile_mode: str = "none",
        deadzone: float = 0.04,
    ):
        super().__init__()
        self.cid = cid
        self.device_name = name
        self.current_axes_keys = list(axes_keys)
        self.current_btn_keys = list(btn_keys)
        self.current_profile_mode = profile_mode

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
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(14, 12, 14, 12)
        self.main_layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self.check_box = CheckToggle(checked=checked)
        self.check_box.toggled.connect(self._on_check_changed)

        self.title_lbl = QLabel(f"{name}")
        self.title_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_lbl.mousePressEvent = lambda e: self.check_box.mousePressEvent(e)

        self.id_lbl = QLabel(f"[{cid}]")
        self.id_lbl.setStyleSheet("color: #6c6c75; font-size: 11px; font-family: 'Consolas', monospace;")

        self.btn_calibrate = QPushButton(tr("card_btn_calibrate"))
        self.btn_calibrate.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_calibrate.setFixedHeight(24)
        self.btn_calibrate.clicked.connect(lambda: self.calibrate_requested.emit(self.cid))
        self._update_calibrate_btn_style(profile_mode)

        self.state_badge = QLabel()
        self.state_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_badge.setFixedHeight(22)
        self._update_badge_style(checked)

        top_row.addWidget(self.check_box)
        top_row.addWidget(self.title_lbl)
        top_row.addWidget(self.id_lbl)
        top_row.addStretch()
        top_row.addWidget(self.btn_calibrate)
        top_row.addWidget(self.state_badge)
        self.main_layout.addLayout(top_row)

        dz_row = QHBoxLayout()
        dz_row.setSpacing(8)

        self.dz_lbl = QLabel(tr("lbl_deadzone"))
        self.dz_lbl.setStyleSheet("color: #9e9ea8; font-size: 11px; font-weight: bold;")

        self.dz_slider = QSlider(Qt.Orientation.Horizontal)
        self.dz_slider.setRange(0, 50)
        self.dz_slider.setSingleStep(1)
        self.dz_slider.setPageStep(5)
        self.dz_slider.setFixedHeight(20)
        self.dz_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dz_slider.setValue(int(round(max(0.0, min(0.5, deadzone)) * 100)))
        self.dz_slider.valueChanged.connect(self._on_slider_changed)

        self.dz_val_lbl = QLabel(f"{deadzone:.2f}")
        self.dz_val_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dz_val_lbl.setFixedWidth(46)
        self.dz_val_lbl.setStyleSheet(
            "background-color: #232329; border: 1px solid #34343c; border-radius: 5px; "
            "padding: 2px 6px; color: #00d2ff; font-family: 'Consolas', monospace; "
            "font-size: 11px; font-weight: bold;"
        )

        dz_row.addWidget(self.dz_lbl)
        dz_row.addWidget(self.dz_slider, 1)
        dz_row.addWidget(self.dz_val_lbl)
        self.main_layout.addLayout(dz_row)

        self.controls_container = QWidget()
        self.controls_layout = QVBoxLayout(self.controls_container)
        self.controls_layout.setContentsMargins(0, 0, 0, 0)
        self.controls_layout.setSpacing(10)
        self.main_layout.addWidget(self.controls_container)

        self.axis_bars: dict[str, BipolarAxisBar] = {}
        self.btn_pills: dict[str, ButtonPill] = {}
        self._rebuild_controls(axes_keys, btn_keys)

    def _on_slider_changed(self, raw_val: int):
        val = round(raw_val / 100.0, 2)
        self.dz_val_lbl.setText(f"{val:.2f}")
        self.deadzone_changed.emit(self.cid, val)

    def set_deadzone_silent(self, deadzone: float):
        target_tick = int(round(max(0.0, min(0.5, deadzone)) * 100))
        if self.dz_slider.value() != target_tick:
            self.dz_slider.blockSignals(True)
            self.dz_slider.setValue(target_tick)
            self.dz_slider.blockSignals(False)
        self.dz_val_lbl.setText(f"{target_tick / 100.0:.2f}")

    def retranslate_ui(self):
        self.dz_lbl.setText(tr("lbl_deadzone"))
        self._update_calibrate_btn_style(self.current_profile_mode)
        self._update_badge_style(self.check_box.checked)

    def _update_calibrate_btn_style(self, mode: str):
        self.current_profile_mode = mode
        if mode == "custom":
            self.btn_calibrate.setText(tr("card_btn_calibrated"))
            self.btn_calibrate.setStyleSheet(
                "QPushButton { background-color: #193240; color: #00d2ff; border: 1px solid #26536b; "
                "border-radius: 10px; padding: 0px 10px; font-size: 11px; font-weight: bold; } "
                "QPushButton:hover { background-color: #214559; }"
            )
        elif mode == "gcdb":
            self.btn_calibrate.setText(tr("card_btn_sdldb"))
            self.btn_calibrate.setStyleSheet(
                "QPushButton { background-color: #1d2c3b; color: #54a0ff; border: 1px solid #2e4a66; "
                "border-radius: 10px; padding: 0px 10px; font-size: 11px; font-weight: bold; } "
                "QPushButton:hover { background-color: #25394d; }"
            )
        else:
            self.btn_calibrate.setText(tr("card_btn_calibrate"))
            self.btn_calibrate.setStyleSheet(
                "QPushButton { background-color: #26262e; color: #cccccc; border: 1px solid #3a3a45; "
                "border-radius: 10px; padding: 0px 10px; font-size: 11px; font-weight: bold; } "
                "QPushButton:hover { border-color: #00d2ff; color: #ffffff; }"
            )

    def _rebuild_controls(self, axes_keys: list[str], btn_keys: list[str]):
        self.current_axes_keys = list(axes_keys)
        self.current_btn_keys = list(btn_keys)
        self.axis_bars.clear()
        self.btn_pills.clear()

        while self.controls_layout.count():
            item = self.controls_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

        if axes_keys:
            axes_grid = QGridLayout()
            axes_grid.setHorizontalSpacing(14)
            axes_grid.setVerticalSpacing(6)
            for idx, a_key in enumerate(axes_keys):
                bar = BipolarAxisBar(a_key.upper())
                self.axis_bars[a_key] = bar
                axes_grid.addWidget(bar, idx // 2, idx % 2)
            self.controls_layout.addLayout(axes_grid)

        if btn_keys:
            btn_grid = QGridLayout()
            btn_grid.setSpacing(6)
            for idx, b_key in enumerate(btn_keys):
                pill = ButtonPill(format_button_label(b_key))
                pill.setToolTip(b_key)
                self.btn_pills[b_key] = pill
                btn_grid.addWidget(pill, idx // 8, idx % 8)
            self.controls_layout.addLayout(btn_grid)

    def _on_check_changed(self, state: bool):
        self._update_badge_style(state)
        self.toggled_export.emit(self.cid, state)

    def _update_badge_style(self, active: bool):
        if active:
            self.state_badge.setText(tr("badge_active"))
            self.state_badge.setStyleSheet(
                "background-color: #153626; color: #2ecc71; border: 1px solid #235c40; "
                "border-radius: 11px; padding: 0px 10px; font-size: 10px; font-weight: bold;"
            )
            self.title_lbl.setStyleSheet("color: #00d2ff; font-weight: bold; font-size: 13px;")
        else:
            self.state_badge.setText(tr("badge_ignored"))
            self.state_badge.setStyleSheet(
                "background-color: #252529; color: #777780; border: 1px solid #35353b; "
                "border-radius: 11px; padding: 0px 10px; font-size: 10px; font-weight: bold;"
            )
            self.title_lbl.setStyleSheet("color: #777780; font-weight: bold; font-size: 13px;")

    def update_values(self, axes: dict, buttons: dict, profile_mode: str = "none"):
        self._update_calibrate_btn_style(profile_mode)
        new_axes_keys = list(axes.keys())
        new_btn_keys = list(buttons.keys())
        if new_axes_keys != self.current_axes_keys or new_btn_keys != self.current_btn_keys:
            self._rebuild_controls(new_axes_keys, new_btn_keys)

        for k, v in axes.items():
            if k in self.axis_bars:
                self.axis_bars[k].set_value(v)
        for k, v in buttons.items():
            if k in self.btn_pills:
                self.btn_pills[k].set_pressed(bool(v))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = self._load_config()
        saved_lang = self.config.get("language") or detect_system_language()
        active_lang = set_language(saved_lang)
        if self.config.get("language") != active_lang:
            self.config["language"] = active_lang
            self._save_config()

        self.setWindowTitle(tr("win_title", version=__version__))
        self.resize(680, 720)
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #121215;
                color: #e6e6e6;
                font-family: 'Segoe UI', sans-serif;
            }
            QComboBox, QLineEdit {
                background-color: #1e1e23;
                border: 1px solid #34343c;
                border-radius: 6px;
                padding: 4px 10px;
                color: white;
                font-weight: bold;
            }
            QDoubleSpinBox {
                background-color: #1e1e23;
                border: 1px solid #34343c;
                border-radius: 6px;
                padding: 4px 30px 4px 10px;
                color: white;
                font-weight: bold;
                min-height: 22px;
            }
            QDoubleSpinBox::up-button {
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 24px;
                border-left: 1px solid #34343c;
                border-bottom: 1px solid #34343c;
                border-top-right-radius: 5px;
                background-color: #26262e;
            }
            QDoubleSpinBox::down-button {
                subcontrol-origin: border;
                subcontrol-position: bottom right;
                width: 24px;
                border-left: 1px solid #34343c;
                border-bottom-right-radius: 5px;
                background-color: #26262e;
            }
            QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
                background-color: #32323e;
            }
            QDoubleSpinBox::up-button:pressed, QDoubleSpinBox::down-button:pressed {
                background-color: #00a8cc;
            }
            QDoubleSpinBox::up-arrow, QDoubleSpinBox::down-arrow {
                image: none;
                width: 0px;
                height: 0px;
            }
            QSlider::groove:horizontal {
                background: #232329;
                border: 1px solid #34343c;
                height: 6px;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #00a8cc;
                border: 1px solid #00a8cc;
                height: 6px;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #00d2ff;
                border: 2px solid #1b1b1e;
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #ffffff;
                border-color: #00d2ff;
            }
            QLineEdit#PathDisplay {
                color: #bdbdc7;
                font-weight: normal;
                font-family: 'Consolas', monospace;
                font-size: 11px;
            }
            QPushButton#CompactBtn {
                background-color: #1e1e23;
                border: 1px solid #34343c;
                border-radius: 6px;
                padding: 4px 10px;
                color: #e6e6e6;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton#CompactBtn:hover {
                border-color: #00d2ff;
                background-color: #26262e;
            }
            QPushButton#CompactBtn:disabled {
                color: #555560;
                border-color: #26262e;
            }
            QComboBox:hover, QDoubleSpinBox:hover, QLineEdit:hover {
                border-color: #00d2ff;
            }
            QComboBox::drop-down {
                border: none;
                width: 22px;
            }
            QComboBox QAbstractItemView {
                background-color: #1e1e23;
                color: #e6e6e6;
                border: 1px solid #34343c;
                selection-background-color: #00a8cc;
                selection-color: #111111;
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

        saved_output = str(self.config.get("output_path", "")).strip()
        if saved_output:
            candidate = Path(saved_output)
            if candidate.is_dir() or candidate.suffix.lower() != ".json":
                candidate = candidate / "controller_data.json"
            self.output_path = candidate
        else:
            self.output_path = DEFAULT_OUTPUT_JSON

        saved_default_deadzone = float(self.config.get("deadzone", 0.04))
        saved_deadzones = self.config.get("controller_deadzones", {})
        saved_interval = float(self.config.get("write_interval", 0.045))
        saved_enabled = self.config.get("enabled_controllers", {})
        saved_profiles = self.config.get("device_profiles", {})
        saved_gcdb_etag = str(self.config.get("gcdb_etag", ""))

        initial_gcdb = parse_sdl_gamecontroller_db(GCDB_FILE)
        self.gcdb_status_key: str | None = None
        self.gcdb_status_count: int = len(initial_gcdb)

        self.latest_update_info: dict | None = None
        self.update_state = "checking"
        self.update_worker: UpdateCheckWorker | None = None
        self.download_worker: UpdateDownloadWorker | None = None
        self.gcdb_worker: GameControllerDBSyncWorker | None = None
        self.latest_controllers_state: dict[str, dict] = {}
        self.active_wizard: CalibrationWizardDialog | None = None

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)
        self.update_lbl = ClickableLabel(tr("upd_checking", version=__version__))
        self._set_update_banner("checking", tr("upd_checking", version=__version__))
        self.update_lbl.clicked.connect(self._on_manual_update_click)

        self.lang_lbl = QLabel(tr("lbl_language"))
        self.lang_lbl.setStyleSheet("color: #aaaaaa; font-size: 12px; font-weight: bold;")

        self.lang_combo = QComboBox()
        for code, native_name in LANGUAGES.items():
            self.lang_combo.addItem(native_name, code)
        idx = self.lang_combo.findData(active_lang)
        if idx >= 0:
            self.lang_combo.setCurrentIndex(idx)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)

        top_bar.addWidget(self.update_lbl, 1)
        top_bar.addWidget(self.lang_lbl)
        top_bar.addWidget(self.lang_combo)
        main_layout.addLayout(top_bar)

        self.status_lbl = QLabel(tr("status_searching"))
        self.status_lbl.setStyleSheet(
            "background-color: #1b2e38; color: #00d2ff; padding: 8px 12px; "
            "border-radius: 10px; font-weight: bold;"
        )
        main_layout.addWidget(self.status_lbl)

        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self.output_path_lbl = QLabel(tr("lbl_output_path"))
        self.output_path_lbl.setStyleSheet("color: #aaaaaa; font-size: 11px; font-weight: bold;")

        self.path_edit = QLineEdit(str(self.output_path))
        self.path_edit.setObjectName("PathDisplay")
        self.path_edit.setReadOnly(True)
        self.path_edit.setToolTip(str(self.output_path))

        self.btn_browse_output = QPushButton(tr("btn_browse"))
        self.btn_browse_output.setObjectName("CompactBtn")
        self.btn_browse_output.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse_output.clicked.connect(self._on_browse_output_path)

        self.btn_reset_output = QPushButton("↺")
        self.btn_reset_output.setObjectName("CompactBtn")
        self.btn_reset_output.setFixedWidth(30)
        self.btn_reset_output.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reset_output.setToolTip(tr("tip_reset_output"))
        self.btn_reset_output.setEnabled(self.output_path.resolve() != DEFAULT_OUTPUT_JSON.resolve())
        self.btn_reset_output.clicked.connect(self._on_reset_output_path)

        path_row.addWidget(self.output_path_lbl)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.btn_browse_output)
        path_row.addWidget(self.btn_reset_output)
        main_layout.addLayout(path_row)

        info_row = QHBoxLayout()
        self.gcdb_lbl = QLabel(
            tr("gcdb_checking", count=len(initial_gcdb))
            if initial_gcdb
            else tr("gcdb_loading")
        )
        self.gcdb_lbl.setStyleSheet("color: #54a0ff; font-size: 11px;")
        info_row.addStretch()
        info_row.addWidget(self.gcdb_lbl)
        main_layout.addLayout(info_row)

        interval_row = QHBoxLayout()
        self.interval_lbl = QLabel(tr("lbl_write_interval"))
        interval_row.addWidget(self.interval_lbl)
        self.interval_spin = ArrowDoubleSpinBox()
        self.interval_spin.setRange(0.01, 5.00)
        self.interval_spin.setDecimals(2)
        self.interval_spin.setSingleStep(0.01)
        self.interval_spin.setValue(saved_interval)
        self.interval_spin.valueChanged.connect(self._on_interval_changed)
        interval_row.addStretch()
        interval_row.addWidget(self.interval_spin)
        main_layout.addLayout(interval_row)

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

        self.worker = MultiControllerWorker(
            output_path=self.output_path,
            default_deadzone=saved_default_deadzone,
            deadzones_map=saved_deadzones,
            write_interval=saved_interval,
            enabled_map=saved_enabled,
            profiles_map=saved_profiles,
            gcdb_map=initial_gcdb,
        )
        self.worker.state_updated.connect(self._on_state_updated)
        self.worker.status_updated.connect(self.status_lbl.setText)
        self.worker.start()

        self._start_gcdb_sync(saved_gcdb_etag)
        self._start_update_check(manual=False)

    def _apply_new_output_path(self, new_path: Path):
        self.output_path = new_path
        is_default = self.output_path.resolve() == DEFAULT_OUTPUT_JSON.resolve()
        if is_default:
            self.config.pop("output_path", None)
        else:
            self.config["output_path"] = str(self.output_path)
        self._save_config()

        self.path_edit.setText(str(self.output_path))
        self.path_edit.setToolTip(str(self.output_path))
        self.btn_reset_output.setEnabled(not is_default)
        self.worker.set_output_path(self.output_path)

    def _on_browse_output_path(self):
        start_target = self.output_path if self.output_path.parent.exists() else DEFAULT_OUTPUT_JSON
        selected, _ = QFileDialog.getSaveFileName(
            self,
            tr("dlg_select_output_title"),
            str(start_target),
            "JSON (*.json)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not selected:
            return
        chosen = Path(selected)
        if chosen.suffix.lower() != ".json":
            chosen = chosen.with_suffix(".json")
        self._apply_new_output_path(chosen)

    def _on_reset_output_path(self):
        self._apply_new_output_path(DEFAULT_OUTPUT_JSON)

    def _on_language_changed(self, idx: int):
        code = self.lang_combo.itemData(idx)
        if not code:
            return
        active = set_language(code)
        self.config["language"] = active
        self._save_config()
        self._retranslate_ui()

    def _retranslate_ui(self):
        self.setWindowTitle(tr("win_title", version=__version__))
        self.lang_lbl.setText(tr("lbl_language"))
        self.output_path_lbl.setText(tr("lbl_output_path"))
        self.btn_browse_output.setText(tr("btn_browse"))
        self.btn_reset_output.setToolTip(tr("tip_reset_output"))
        self.interval_lbl.setText(tr("lbl_write_interval"))

        if self.update_state == "update" and self.latest_update_info:
            new_ver = self.latest_update_info.get("version", "")
            self._set_update_banner("update", tr("upd_available", version=new_ver))
        elif self.update_state == "ok":
            self._set_update_banner("ok", tr("upd_up_to_date", version=__version__))
        elif self.update_state == "failed":
            self._set_update_banner("failed", tr("upd_failed_banner"))
        else:
            self._set_update_banner("checking", tr("upd_checking", version=__version__))

        if self.gcdb_status_key:
            self.gcdb_lbl.setText(f"SDL-DB: {tr(self.gcdb_status_key, count=self.gcdb_status_count)}")
        elif self.gcdb_status_count > 0:
            self.gcdb_lbl.setText(tr("gcdb_checking", count=self.gcdb_status_count))
        else:
            self.gcdb_lbl.setText(tr("gcdb_loading"))

        if not self.latest_controllers_state:
            self.status_lbl.setText(tr("status_searching"))

        for card in self.cards.values():
            card.retranslate_ui()

    def _start_gcdb_sync(self, saved_etag: str):
        self.gcdb_worker = GameControllerDBSyncWorker(saved_etag=saved_etag)
        self.gcdb_worker.db_updated.connect(self._on_gcdb_synced)
        self.gcdb_worker.start()

    def _on_gcdb_synced(self, new_db: dict, new_etag: str, status_key: str, count: int):
        self.gcdb_status_key = status_key
        self.gcdb_status_count = count
        self.gcdb_lbl.setText(f"SDL-DB: {tr(status_key, count=count)}")
        if new_db:
            self.worker.set_gcdb_map(new_db)
        if new_etag and new_etag != self.config.get("gcdb_etag"):
            self.config["gcdb_etag"] = new_etag
            self._save_config()

    def _set_update_banner(self, state: str, text: str):
        self.update_state = state
        self.update_lbl.setText(text)
        if state in ("update", "failed"):
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

    def _start_update_check(self, manual: bool):
        if self.download_worker and self.download_worker.isRunning():
            return
        if self.update_worker and self.update_worker.isRunning():
            return

        self._set_update_banner("checking", tr("upd_checking", version=__version__))
        self.update_worker = UpdateCheckWorker(manual=manual)
        self.update_worker.result_ready.connect(self._on_update_check_finished)
        self.update_worker.start()

    def _on_manual_update_click(self):
        if self.latest_update_info:
            self._prompt_and_install_update(self.latest_update_info)
        else:
            self._start_update_check(manual=True)

    def _on_update_check_finished(self, update_info: dict | None, manual: bool):
        self.latest_update_info = update_info
        if update_info:
            new_ver = update_info.get("version", "")
            self._set_update_banner("update", tr("upd_available", version=new_ver))
        else:
            self._set_update_banner("ok", tr("upd_up_to_date", version=__version__))

    def _prompt_and_install_update(self, update_info: dict):
        if not getattr(sys, "frozen", False):
            QMessageBox.information(
                self,
                tr("upd_dev_title"),
                tr("upd_dev_text", version=update_info.get("version", "")),
            )
            return

        new_ver = update_info.get("version", "")
        notes = update_info.get("notes", "").strip()
        if notes:
            msg = tr("upd_confirm_text_notes", new_ver=new_ver, cur_ver=__version__, notes=notes[:400])
        else:
            msg = tr("upd_confirm_text", new_ver=new_ver, cur_ver=__version__)

        reply = QMessageBox.question(
            self,
            tr("upd_confirm_title"),
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._set_update_banner("update", tr("upd_downloading", version=new_ver, pct=0))
            self.download_worker = UpdateDownloadWorker(update_info)
            self.download_worker.progress_changed.connect(
                lambda pct: self._set_update_banner("update", tr("upd_downloading", version=new_ver, pct=pct))
            )
            self.download_worker.failed.connect(self._on_update_download_failed)
            self.download_worker.start()

    def _on_update_download_failed(self):
        self._set_update_banner("failed", tr("upd_failed_banner"))
        QMessageBox.warning(
            self,
            tr("upd_err_title"),
            tr("upd_err_text"),
        )

    def _on_controller_deadzone_changed(self, cid: str, val: float):
        cdata = self.latest_controllers_state.get(cid)
        dev_name = cdata.get("name", cid) if cdata else cid
        self.worker.set_device_deadzone(dev_name, val)

        if "controller_deadzones" not in self.config:
            self.config["controller_deadzones"] = {}
        self.config["controller_deadzones"][dev_name] = val
        self._save_config()

        for other_cid, card in self.cards.items():
            if other_cid != cid and card.device_name == dev_name:
                card.set_deadzone_silent(val)

    def _on_interval_changed(self, val: float):
        self.worker.write_interval = max(0.005, float(val))
        self.worker.force_rewrite = True
        self.config["write_interval"] = float(val)
        self._save_config()

    def _on_controller_toggled(self, cid: str, enabled: bool):
        self.worker.set_controller_enabled(cid, enabled)
        if "enabled_controllers" not in self.config:
            self.config["enabled_controllers"] = {}
        self.config["enabled_controllers"][cid] = enabled
        self._save_config()

    def _open_calibration_wizard(self, cid: str):
        cdata = self.latest_controllers_state.get(cid)
        if not cdata:
            return
        dev_name = cdata.get("name", cid)
        existing = self.config.get("device_profiles", {}).get(dev_name)

        wizard = CalibrationWizardDialog(cid, dev_name, existing_profile=existing, parent=self)
        wizard.feed_raw_state(cdata.get("raw_axes", {}), cdata.get("raw_buttons", {}))
        self.active_wizard = wizard

        if wizard.exec() == QDialog.DialogCode.Accepted:
            profile = wizard.result_profile
            if "device_profiles" not in self.config:
                self.config["device_profiles"] = {}
            if profile is None:
                self.config["device_profiles"].pop(dev_name, None)
            else:
                self.config["device_profiles"][dev_name] = profile
            self._save_config()
            self.worker.set_device_profile(dev_name, profile)

        self.active_wizard = None

    def _on_state_updated(self, data: dict):
        controllers = data.get("controllers", {})
        self.latest_controllers_state = controllers

        if self.active_wizard is not None:
            w_data = controllers.get(self.active_wizard.cid)
            if w_data:
                self.active_wizard.feed_raw_state(
                    w_data.get("raw_axes", {}),
                    w_data.get("raw_buttons", {}),
                )

        for cid in list(self.cards.keys()):
            if cid not in controllers:
                card = self.cards.pop(cid)
                self.cards_layout.removeWidget(card)
                card.deleteLater()

        for cid, cdata in controllers.items():
            axes = cdata.get("axes", {})
            buttons = cdata.get("buttons", {})
            profile_mode = str(cdata.get("profile_mode", "none"))
            dev_name = cdata.get("name", cid)
            if cid not in self.cards:
                is_checked = self.worker.is_controller_enabled(cid)
                dev_dz = self.worker.get_device_deadzone(dev_name)
                card = ControllerCard(
                    cid,
                    dev_name,
                    list(axes.keys()),
                    list(buttons.keys()),
                    checked=is_checked,
                    profile_mode=profile_mode,
                    deadzone=dev_dz,
                )
                card.toggled_export.connect(self._on_controller_toggled)
                card.calibrate_requested.connect(self._open_calibration_wizard)
                card.deadzone_changed.connect(self._on_controller_deadzone_changed)
                self.cards[cid] = card
                self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            self.cards[cid].update_values(axes, buttons, profile_mode=profile_mode)

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
        if self.update_worker and self.update_worker.isRunning():
            self.update_worker.wait(500)
        if self.gcdb_worker and self.gcdb_worker.isRunning():
            self.gcdb_worker.wait(500)
        event.accept()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Leontierchen.UniversalControllerBridge"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app_icon = QIcon(str(ICON_FILE)) if ICON_FILE.exists() else QIcon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    win = MainWindow()
    if not app_icon.isNull():
        win.setWindowIcon(app_icon)
    win.show()
    sys.exit(app.exec())