from __future__ import annotations

import ctypes
import json
import logging
import os
import psutil
import sys
import threading
import time
import webbrowser
import pyperclip
import asyncio as _asyncio
from pathlib import Path
from typing import Dict, Optional, Callable
from PIL import Image, ImageDraw, ImageFont

import proxy.tg_ws_proxy as tg_ws_proxy

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QCheckBox, QPushButton, QMessageBox,
    QDialog, QFrame, QMenu, QSystemTrayIcon, QAction, QDesktopWidget,
    QAbstractButton
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QPoint, QRect, QPropertyAnimation, QEasingCurve, pyqtProperty, \
    QPointF, QSize
from PyQt5.QtGui import QFont, QPalette, QColor, QIcon, QPainter, QBrush, QPen, QCursor, QPixmap, QLinearGradient, \
    QRadialGradient

APP_NAME = "Samsuka"
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "samsuka.log"
FIRST_RUN_MARKER = APP_DIR / ".first_run_done"
IPV6_WARN_MARKER = APP_DIR / ".ipv6_warned"
SETTINGS_FILE = APP_DIR / "settings.json"

DEFAULT_CONFIG = {
    "port": 1080,
    "host": "127.0.0.1",
    "dc_ip": ["2:149.154.167.220", "4:149.154.167.220"],
    "verbose": False,
    "enabled": False,
}

DEFAULT_SETTINGS = {
    "theme": "dark",
    "start_with_windows": False,
    "start_minimized": False,
    "show_in_tray": True,
    "auto_start_proxy": True,
    "close_to_tray": True,
}

_proxy_thread: Optional[threading.Thread] = None
_async_stop: Optional[object] = None
_tray_icon: Optional[QSystemTrayIcon] = None
_tray_menu: Optional[QMenu] = None
_config: dict = {}
_settings: dict = {}
_exiting: bool = False
_lock_file_path: Optional[Path] = None
_qt_app: Optional[QApplication] = None
_main_window: Optional[QMainWindow] = None

log = logging.getLogger("samsuka")

FONT_FAMILY = "Inter"

def get_font(size: int = 13, weight: int = QFont.Normal) -> QFont:
    font = QFont(FONT_FAMILY, size)
    font.setWeight(weight)
    return font

def load_icon(name: str) -> QIcon:
    icon_path = Path(__file__).parent / "icons" / name
    if icon_path.exists():
        return QIcon(str(icon_path))
    return QIcon()

class SignalEmitter(QObject):
    show_error_signal = pyqtSignal(str, str)
    show_info_signal = pyqtSignal(str, str)
    restart_proxy_signal = pyqtSignal()
    proxy_status_changed = pyqtSignal(bool)
    proxy_started_signal = pyqtSignal()
    proxy_stopped_signal = pyqtSignal()
    theme_changed_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()

    def show_error(self, text: str, title: str = "Samsuka — Ошибка"):
        self.show_error_signal.emit(text, title)

    def show_info(self, text: str, title: str = "Samsuka"):
        self.show_info_signal.emit(text, title)

_signal_emitter = SignalEmitter()

def _same_process(lock_meta: dict, proc: psutil.Process) -> bool:
    try:
        lock_ct = float(lock_meta.get("create_time", 0.0))
        proc_ct = float(proc.create_time())
        if lock_ct > 0 and abs(lock_ct - proc_ct) > 1.0:
            return False
    except Exception:
        return False

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return os.path.basename(sys.executable) == proc.name()

    return False

def _release_lock():
    global _lock_file_path
    if not _lock_file_path:
        return
    try:
        _lock_file_path.unlink(missing_ok=True)
    except Exception:
        pass
    _lock_file_path = None

def _acquire_lock() -> bool:
    global _lock_file_path
    _ensure_dirs()
    lock_files = list(APP_DIR.glob("*.lock"))

    for f in lock_files:
        pid = None
        meta: dict = {}

        try:
            pid = int(f.stem)
        except Exception:
            f.unlink(missing_ok=True)
            continue

        try:
            raw = f.read_text(encoding="utf-8").strip()
            if raw:
                meta = json.loads(raw)
        except Exception:
            meta = {}

        try:
            proc = psutil.Process(pid)
            if _same_process(meta, proc):
                return False
        except Exception:
            pass

        f.unlink(missing_ok=True)

    lock_file = APP_DIR / f"{os.getpid()}.lock"
    try:
        proc = psutil.Process(os.getpid())
        payload = {
            "create_time": proc.create_time(),
        }
        lock_file.write_text(json.dumps(payload, ensure_ascii=False),
                             encoding="utf-8")
    except Exception:
        lock_file.touch()

    _lock_file_path = lock_file
    return True

def _ensure_dirs():
    APP_DIR.mkdir(parents=True, exist_ok=True)

def load_config() -> dict:
    _ensure_dirs()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception as exc:
            log.warning("Failed to load config: %s", exc)
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    _ensure_dirs()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def load_settings() -> dict:
    _ensure_dirs()
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
            return data
        except Exception as exc:
            log.warning("Failed to load settings: %s", exc)
    return dict(DEFAULT_SETTINGS)

def save_settings(settings: dict):
    _ensure_dirs()
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

def setup_logging(verbose: bool = False):
    _ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%H:%M:%S"))
    root.addHandler(ch)

def _create_tray_icon_pixmap(size: int = 64) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    painter.setBrush(QBrush(QColor(52, 152, 219)))
    painter.setPen(Qt.NoPen)
    margin = 2
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)

    painter.setPen(QPen(Qt.white, 2))
    font = get_font(int(size * 0.55), QFont.Bold)
    painter.setFont(font)

    fm = painter.fontMetrics()
    text = "S"
    text_width = fm.horizontalAdvance(text)
    text_height = fm.height()
    text_x = (size - text_width) // 2
    text_y = (size + text_height // 2) // 2

    painter.drawText(text_x, text_y, text)
    painter.end()

    return pixmap

def _load_icon() -> QIcon:
    icon_path = Path(__file__).parent / "icon.ico"
    if icon_path.exists():
        return QIcon(str(icon_path))

    pixmap = _create_tray_icon_pixmap(64)
    return QIcon(pixmap)

def add_to_startup():
    import winreg
    try:
        key = winreg.HKEY_CURRENT_USER
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(key, key_path, 0, winreg.KEY_SET_VALUE) as reg_key:
            if _settings.get("start_with_windows", False):
                winreg.SetValueEx(reg_key, APP_NAME, 0, winreg.REG_SZ, sys.executable)
            else:
                try:
                    winreg.DeleteValue(reg_key, APP_NAME)
                except:
                    pass
    except Exception as e:
        log.error("Failed to update startup: %s", e)

def get_colors(theme: str = None):
    if theme is None:
        theme = _settings.get("theme", "dark")

    if theme == "light":
        return {
            "bg": "#f5f5f5",
            "bg_secondary": "#ffffff",
            "text": "#2c3e50",
            "text_secondary": "#7f8c8d",
            "border": "#bdc3c7",
            "accent": "#3498db",
            "accent_hover": "#2980b9",
            "success": "#2ecc71",
            "warning": "#f39c12",
            "error": "#e74c3c",
            "info_bg": "#ecf0f1",
        }
    else:
        return {
            "bg": "#1e1e1e",
            "bg_secondary": "#2d2d2d",
            "text": "#e0e0e0",
            "text_secondary": "#a0a0a0",
            "border": "#3d3d3d",
            "accent": "#3498db",
            "accent_hover": "#2980b9",
            "success": "#2ecc71",
            "warning": "#f39c12",
            "error": "#e74c3c",
            "info_bg": "#34495e",
        }

def get_stylesheet(theme: str = None):
    colors = get_colors(theme)

    return f"""
        QDialog, QMainWindow, QWidget {{
            background-color: {colors["bg"]};
            color: {colors["text"]};
        }}
        QLabel {{
            color: {colors["text"]};
            font-size: 13px;
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QLabel#titleLabel {{
            font-size: 24px;
            font-weight: bold;
            color: {colors["accent"]};
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QLabel#statusLabel {{
            font-size: 16px;
            font-weight: 500;
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QLineEdit, QTextEdit {{
            background-color: {colors["bg_secondary"]};
            border: 1px solid {colors["border"]};
            border-radius: 10px;
            padding: 8px 12px;
            font-size: 13px;
            color: {colors["text"]};
            selection-background-color: {colors["accent"]};
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QLineEdit:focus, QTextEdit:focus {{
            border: 2px solid {colors["accent"]};
        }}
        QCheckBox {{
            color: {colors["text"]};
            font-size: 13px;
            spacing: 8px;
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
            border-radius: 4px;
            border: 2px solid {colors["border"]};
            background-color: {colors["bg_secondary"]};
        }}
        QCheckBox::indicator:checked {{
            background-color: {colors["accent"]};
            border-color: {colors["accent"]};
        }}
        QCheckBox::indicator:hover {{
            border-color: {colors["accent"]};
        }}
        QPushButton {{
            border-radius: 10px;
            padding: 8px 20px;
            font-size: 14px;
            font-weight: bold;
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QPushButton#saveButton {{
            background-color: {colors["accent"]};
            color: white;
            border: none;
        }}
        QPushButton#saveButton:hover {{
            background-color: {colors["accent_hover"]};
        }}
        QPushButton#cancelButton {{
            background-color: {colors["bg_secondary"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
        }}
        QPushButton#cancelButton:hover {{
            background-color: {colors["border"]};
        }}
        QFrame#separator {{
            background-color: {colors["border"]};
            max-height: 1px;
        }}
        QFrame#infoFrame {{
            background-color: {colors["bg_secondary"]};
            border-radius: 20px;
            padding: 20px;
        }}
        QMenu {{
            background-color: {colors["bg_secondary"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
            border-radius: 8px;
            padding: 4px;
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QMenu::item {{
            padding: 8px 24px;
            border-radius: 4px;
        }}
        QMenu::item:selected {{
            background-color: {colors["accent"]};
            color: white;
        }}
        QMenu::separator {{
            height: 1px;
            background-color: {colors["border"]};
            margin: 4px 8px;
        }}
        QMessageBox {{
            background-color: {colors["bg_secondary"]};
            color: {colors["text"]};
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QMessageBox QLabel {{
            color: {colors["text"]};
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QMessageBox QPushButton {{
            background-color: {colors["accent"]};
            color: white;
            border: none;
            border-radius: 6px;
            padding: 6px 16px;
            min-width: 80px;
            font-family: {FONT_FAMILY}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        QMessageBox QPushButton:hover {{
            background-color: {colors["accent_hover"]};
        }}
    """

class ThemeToggleSwitch(QAbstractButton):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(60, 30)

        self.sun_icon = load_icon("sun.png")
        self.moon_icon = load_icon("moon.png")

        self._circle_position = 32 if self.isChecked() else 4
        self._animation = QPropertyAnimation(self, b"circle_position")
        self._animation.setDuration(200)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

        self.update()

    def circle_position(self):
        return self._circle_position

    def set_circle_position(self, pos):
        self._circle_position = pos
        self.update()

    circle_position = pyqtProperty(int, circle_position, set_circle_position)

    def nextCheckState(self):
        self.setChecked(not self.isChecked())
        self._animate()
        theme = "light" if self.isChecked() else "dark"
        _signal_emitter.theme_changed_signal.emit(theme)

    def _animate(self):
        self._animation.setStartValue(self.circle_position)
        self._animation.setEndValue(32 if self.isChecked() else 4)
        self._animation.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.nextCheckState()
            event.accept()
        else:
            super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(0, 0, 0, 0)

        if self.isChecked():
            bg_color = QColor(255, 214, 0)
        else:
            bg_color = QColor(80, 80, 140)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(bg_color))
        painter.drawRoundedRect(rect, 15, 15)

        if not self.sun_icon.isNull():
            pixmap = self.sun_icon.pixmap(20, 20)
            painter.drawPixmap(8, 5, pixmap)
        else:
            font = get_font(12)
            painter.setFont(font)
            painter.setPen(QPen(Qt.white))
            painter.drawText(QRect(8, 5, 20, 20), Qt.AlignCenter, "☀")

        if not self.moon_icon.isNull():
            pixmap = self.moon_icon.pixmap(20, 20)
            painter.drawPixmap(32, 5, pixmap)
        else:
            font = get_font(12)
            painter.setFont(font)
            painter.setPen(QPen(Qt.white))
            painter.drawText(QRect(32, 5, 20, 20), Qt.AlignCenter, "☾")

        circle_rect = QRect(
            self.circle_position - 2,
            2,
            26,
            26
        )

        painter.setBrush(QBrush(QColor(0, 0, 0, 30)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(circle_rect.translated(1, 1))

        painter.setBrush(QBrush(Qt.white))
        painter.setPen(QPen(QColor(255, 255, 255, 50), 1))
        painter.drawEllipse(circle_rect)

        painter.end()

class SettingsToggleSwitch(QAbstractButton):

    def __init__(self, parent=None, initial_state=False):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(initial_state)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(50, 24)

        self._circle_position = 26 if self.isChecked() else 2
        self._animation = QPropertyAnimation(self, b"circle_position")
        self._animation.setDuration(200)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

        self.on_color = QColor(52, 152, 219)
        self.off_color = QColor(149, 165, 166)

        self.update()

    def circle_position(self):
        return self._circle_position

    def set_circle_position(self, pos):
        self._circle_position = pos
        self.update()

    circle_position = pyqtProperty(int, circle_position, set_circle_position)

    def nextCheckState(self):
        self.setChecked(not self.isChecked())
        self._animate()

    def _animate(self):
        self._animation.setStartValue(self.circle_position)
        self._animation.setEndValue(26 if self.isChecked() else 2)
        self._animation.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.nextCheckState()
            event.accept()
        else:
            super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(0, 0, 0, 0)

        if self.isChecked():
            bg_color = self.on_color
        else:
            bg_color = self.off_color

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(bg_color))
        painter.drawRoundedRect(rect, 12, 12)

        circle_rect = QRect(
            self.circle_position,
            2,
            20,
            20
        )

        painter.setBrush(QBrush(QColor(0, 0, 0, 30)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(circle_rect.translated(1, 1))

        painter.setBrush(QBrush(Qt.white))
        painter.setPen(QPen(QColor(255, 255, 255, 50), 1))
        painter.drawEllipse(circle_rect)

        painter.end()

class PowerButton(QPushButton):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(140, 140)

        self._scale = 1.0
        self._animation = QPropertyAnimation(self, b"scale")
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.OutQuad)

        self.update()

    def get_scale(self):
        return self._scale

    def set_scale(self, scale):
        self._scale = scale
        self.update()

    scale = pyqtProperty(float, get_scale, set_scale)

    def nextCheckState(self):
        self._animation.setStartValue(0.9)
        self._animation.setEndValue(1.0)
        self._animation.start()

        self.setChecked(not self.isChecked())
        _signal_emitter.proxy_status_changed.emit(self.isChecked())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.nextCheckState()
            event.accept()
        else:
            super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        center = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) / 2 * self._scale

        if self.isChecked():
            main_color = QColor(52, 152, 219)
            glow_color = QColor(52, 152, 219, 100)
            text_color = Qt.white
            status_text = "ВКЛ"
        else:
            main_color = QColor(149, 165, 166)
            glow_color = QColor(149, 165, 166, 80)
            text_color = QColor(220, 220, 220)
            status_text = "ВЫКЛ"

        for i in range(3, 0, -1):
            alpha = 40 - i * 10
            glow_radius = radius + i * 5
            gradient = QRadialGradient(center, glow_radius)
            gradient.setColorAt(0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), alpha))
            gradient.setColorAt(1, Qt.transparent)
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(center, glow_radius, glow_radius)

        gradient = QRadialGradient(center, radius)
        gradient.setColorAt(0, main_color.lighter(120))
        gradient.setColorAt(0.7, main_color)
        gradient.setColorAt(1, main_color.darker(150))

        painter.setBrush(QBrush(gradient))
        painter.setPen(QPen(QColor(255, 255, 255, 50), 2))
        painter.drawEllipse(center, radius - 2, radius - 2)

        highlight = QRadialGradient(QPointF(center.x() - radius * 0.3, center.y() - radius * 0.3), radius * 0.5)
        highlight.setColorAt(0, QColor(255, 255, 255, 80))
        highlight.setColorAt(1, Qt.transparent)
        painter.setBrush(QBrush(highlight))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(center, radius - 10, radius - 10)

        font = get_font(24, QFont.Bold)
        painter.setFont(font)

        fm = painter.fontMetrics()
        text_width = fm.horizontalAdvance(status_text)
        text_x = int(center.x() - text_width / 2)
        text_y = int(center.y() + fm.height() / 4)

        painter.setPen(QPen(text_color))
        painter.drawText(text_x, text_y, status_text)

        painter.end()

class CustomTrayMenu(QMenu):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(get_stylesheet(_settings.get("theme", "dark")))
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def showEvent(self, event):
        super().showEvent(event)

        cursor_pos = QCursor.pos()

        screen = QApplication.primaryScreen()
        screen_rect = screen.availableGeometry()

        x = cursor_pos.x()
        y = cursor_pos.y() - self.height() - 5

        if x + self.width() > screen_rect.right():
            x = screen_rect.right() - self.width()
        if y < screen_rect.top():
            y = cursor_pos.y() + 20

        self.move(x, y)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Samsuka — Telegram WS Proxy")
        self.setFixedSize(450, 600)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.center()

        self.dragging = False
        self.drag_position = QPoint()

        self._init_ui()

        self.apply_theme()

        _signal_emitter.proxy_status_changed.connect(self.on_proxy_status_changed)
        _signal_emitter.proxy_started_signal.connect(self.on_proxy_started)
        _signal_emitter.proxy_stopped_signal.connect(self.on_proxy_stopped)
        _signal_emitter.theme_changed_signal.connect(self.on_theme_changed)

    def center(self):
        screen = QDesktopWidget().availableGeometry()
        window = self.geometry()
        self.move(
            (screen.width() - window.width()) // 2,
            (screen.height() - window.height()) // 2
        )

    def apply_theme(self):
        colors = get_colors()
        self.setStyleSheet(get_stylesheet())

        if hasattr(self, 'status_icon'):
            if _config.get("enabled", True):
                self.status_icon.setStyleSheet(f"color: {colors['success']}; font-size: 20px;")
            else:
                self.status_icon.setStyleSheet(f"color: {colors['text_secondary']}; font-size: 20px;")

    def _init_ui(self):
        self.settings_icon = load_icon("settings.png")
        self.proxy_settings_icon = load_icon("proxy-settings.png")
        self.close_icon = load_icon("close.png")
        self.telegram_icon = load_icon("telegram.png")
        self.logs_icon = load_icon("logs.png")
        self.host_icon = load_icon("host.png")
        self.port_icon = load_icon("port.png")
        self.dc_icon = load_icon("dc.png")

        container = QFrame(self)
        container.setObjectName("container")
        container.setStyleSheet("""
            QFrame#container {
                background-color: #1e1e1e;
                border-radius: 30px;
                border: 1px solid #3d3d3d;
            }
        """)
        container.setGeometry(0, 0, 450, 600)

        container.mousePressEvent = self.mousePressEvent
        container.mouseMoveEvent = self.mouseMoveEvent
        container.mouseReleaseEvent = self.mouseReleaseEvent

        top_bar = QWidget(container)
        top_bar.setGeometry(0, 0, 450, 60)
        top_bar.setAttribute(Qt.WA_TranslucentBackground)
        top_bar.mousePressEvent = self.mousePressEvent
        top_bar.mouseMoveEvent = self.mouseMoveEvent
        top_bar.mouseReleaseEvent = self.mouseReleaseEvent

        close_btn = QPushButton(top_bar)
        close_btn.setGeometry(400, 15, 30, 30)
        if not self.close_icon.isNull():
            close_btn.setIcon(self.close_icon)
            close_btn.setIconSize(QSize(18, 18))
        else:
            close_btn.setText("✕")
            close_btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #8e8e93;
                    border: none;
                    font-size: 18px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    color: #ff3b30;
                }
            """)
        close_btn.clicked.connect(self.close_clicked)
        close_btn.setCursor(Qt.PointingHandCursor)

        settings_btn = QPushButton(top_bar)
        settings_btn.setGeometry(360, 15, 30, 30)
        if not self.settings_icon.isNull():
            settings_btn.setIcon(self.settings_icon)
            settings_btn.setIconSize(QSize(20, 20))
        else:
            settings_btn.setText("⚙")
            settings_btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #8e8e93;
                    border: none;
                    font-size: 20px;
                }
                QPushButton:hover {
                    color: #3498db;
                }
            """)
        settings_btn.clicked.connect(self.open_app_settings)
        settings_btn.setCursor(Qt.PointingHandCursor)

        proxy_settings_btn = QPushButton(top_bar)
        proxy_settings_btn.setGeometry(320, 15, 30, 30)
        if not self.proxy_settings_icon.isNull():
            proxy_settings_btn.setIcon(self.proxy_settings_icon)
            proxy_settings_btn.setIconSize(QSize(18, 18))
        else:
            proxy_settings_btn.setText("🔧")
            proxy_settings_btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #8e8e93;
                    border: none;
                    font-size: 18px;
                }
                QPushButton:hover {
                    color: #3498db;
                }
            """)
        proxy_settings_btn.clicked.connect(self.open_proxy_settings)
        proxy_settings_btn.setCursor(Qt.PointingHandCursor)

        self.theme_toggle = ThemeToggleSwitch(top_bar)
        self.theme_toggle.setGeometry(260, 15, 60, 30)
        self.theme_toggle.setChecked(_settings.get("theme", "dark") == "light")

        content = QFrame(container)
        content.setGeometry(0, 60, 450, 540)
        content.setObjectName("content")
        content.setStyleSheet("QFrame#content { background-color: transparent; }")

        layout = QVBoxLayout(content)
        layout.setContentsMargins(30, 20, 30, 30)
        layout.setSpacing(25)

        title_label = QLabel("Samsuka")
        title_label.setObjectName("titleLabel")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        status_layout = QHBoxLayout()
        status_layout.setAlignment(Qt.AlignCenter)
        status_layout.setSpacing(10)

        colors = get_colors()
        self.status_icon = QLabel("●")
        self.status_icon.setStyleSheet(f"color: {colors['success']}; font-size: 20px;")
        status_layout.addWidget(self.status_icon)

        self.status_label = QLabel("Прокси работает")
        self.status_label.setObjectName("statusLabel")
        status_layout.addWidget(self.status_label)

        layout.addLayout(status_layout)

        layout.addSpacing(10)

        button_layout = QHBoxLayout()
        button_layout.setAlignment(Qt.AlignCenter)

        self.power_button = PowerButton()
        self.power_button.setChecked(_config.get("enabled", True))
        self.power_button.toggled.connect(self.on_power_button_toggled)
        button_layout.addWidget(self.power_button)

        layout.addLayout(button_layout)

        layout.addSpacing(20)

        info_widget = QWidget()
        info_widget.setObjectName("infoWidget")
        info_layout = QVBoxLayout(info_widget)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(15)

        host = _config.get("host", DEFAULT_CONFIG["host"])
        port = _config.get("port", DEFAULT_CONFIG["port"])

        host_widget = QWidget()
        host_layout = QHBoxLayout(host_widget)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(10)

        host_icon_label = QLabel()
        if not self.host_icon.isNull():
            host_icon_label.setPixmap(self.host_icon.pixmap(18, 18))
        else:
            host_icon_label.setText("🌐")
            host_icon_label.setStyleSheet("font-size: 16px;")
        host_layout.addWidget(host_icon_label)

        colors = get_colors()
        host_label = QLabel(
            f"<b style='color: {colors['accent']};'>Хост:</b> <span style='color: {colors['text']};'>{host}</span>")
        host_label.setStyleSheet("font-size: 14px;")
        host_layout.addWidget(host_label)
        host_layout.addStretch()

        info_layout.addWidget(host_widget)

        port_widget = QWidget()
        port_layout = QHBoxLayout(port_widget)
        port_layout.setContentsMargins(0, 0, 0, 0)
        port_layout.setSpacing(10)

        port_icon_label = QLabel()
        if not self.port_icon.isNull():
            port_icon_label.setPixmap(self.port_icon.pixmap(18, 18))
        else:
            port_icon_label.setText("🔌")
            port_icon_label.setStyleSheet("font-size: 16px;")
        port_layout.addWidget(port_icon_label)

        port_label = QLabel(
            f"<b style='color: {colors['accent']};'>Порт:</b> <span style='color: {colors['text']};'>{port}</span>")
        port_label.setStyleSheet("font-size: 14px;")
        port_layout.addWidget(port_label)
        port_layout.addStretch()

        info_layout.addWidget(port_widget)

        dc_widget = QWidget()
        dc_layout = QHBoxLayout(dc_widget)
        dc_layout.setContentsMargins(0, 0, 0, 0)
        dc_layout.setSpacing(10)

        dc_icon_label = QLabel()
        if not self.dc_icon.isNull():
            dc_icon_label.setPixmap(self.dc_icon.pixmap(18, 18))
        else:
            dc_icon_label.setText("🖧")
            dc_icon_label.setStyleSheet("font-size: 16px;")
        dc_layout.addWidget(dc_icon_label)

        dc_list = _config.get("dc_ip", DEFAULT_CONFIG["dc_ip"])
        dc_text = ", ".join([dc.split(':')[0] for dc in dc_list])
        dc_label = QLabel(
            f"<b style='color: {colors['accent']};'>DC:</b> <span style='color: {colors['text']};'>{dc_text}</span>")
        dc_label.setStyleSheet("font-size: 14px;")
        dc_layout.addWidget(dc_label)
        dc_layout.addStretch()

        info_layout.addWidget(dc_widget)

        layout.addWidget(info_widget)

        layout.addStretch()

        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(15)

        open_btn = QPushButton(" Открыть в Telegram")
        if not self.telegram_icon.isNull():
            open_btn.setIcon(self.telegram_icon)
            open_btn.setIconSize(QSize(20, 20))
        open_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {colors['accent']};
                color: white;
                border: none;
                border-radius: 15px;
                padding: 12px 20px;
                font-size: 14px;
                font-weight: bold;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: {colors['accent_hover']};
            }}
        """)
        open_btn.clicked.connect(_on_open_in_telegram)
        open_btn.setCursor(Qt.PointingHandCursor)
        bottom_layout.addWidget(open_btn)

        logs_btn = QPushButton(" Логи")
        if not self.logs_icon.isNull():
            logs_btn.setIcon(self.logs_icon)
            logs_btn.setIconSize(QSize(20, 20))
        logs_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {colors['bg_secondary']};
                color: {colors['text']};
                border: 1px solid {colors['border']};
                border-radius: 15px;
                padding: 12px 20px;
                font-size: 14px;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: {colors['border']};
            }}
        """)
        logs_btn.clicked.connect(_on_open_logs)
        logs_btn.setCursor(Qt.PointingHandCursor)
        bottom_layout.addWidget(logs_btn)

        layout.addLayout(bottom_layout)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.dragging and event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.dragging = False
        event.accept()

    def close_clicked(self):
        if _settings.get("close_to_tray", True) and _settings.get("show_in_tray", True):
            self.hide()
        else:
            _on_exit()

    def open_app_settings(self):
        _edit_settings_dialog()

    def open_proxy_settings(self):
        _edit_config_dialog()

    def on_power_button_toggled(self, checked):
        colors = get_colors()
        if checked:
            self.status_label.setText("Запуск прокси...")
            self.status_icon.setStyleSheet(f"color: {colors['warning']}; font-size: 20px;")
            start_proxy()
        else:
            self.status_label.setText("Остановка прокси...")
            self.status_icon.setStyleSheet(f"color: {colors['warning']}; font-size: 20px;")
            stop_proxy()

    def on_proxy_status_changed(self, enabled):
        self.power_button.setChecked(enabled)

    def on_proxy_started(self):
        colors = get_colors()
        self.status_label.setText("Прокси работает")
        self.status_icon.setStyleSheet(f"color: {colors['success']}; font-size: 20px;")
        log.info("UI: Proxy started signal received")

    def on_proxy_stopped(self):
        colors = get_colors()
        self.status_label.setText("Прокси остановлен")
        self.status_icon.setStyleSheet(f"color: {colors['text_secondary']}; font-size: 20px;")
        log.info("UI: Proxy stopped signal received")

    def on_theme_changed(self, theme):
        global _settings
        _settings["theme"] = theme
        save_settings(_settings)
        self.apply_theme()

        colors = get_colors(theme)
        container = self.findChild(QFrame, "container")
        if container:
            container.setStyleSheet(f"""
                QFrame#container {{
                    background-color: {colors['bg']};
                    border-radius: 30px;
                    border: 1px solid {colors['border']};
                }}
            """)

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self.activateWindow()

class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.settings = settings.copy()
        self.setWindowTitle("Samsuka — Настройки")
        self.setFixedSize(450, 500)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setStyleSheet(get_stylesheet(_settings.get("theme", "dark")))

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        title_label = QLabel("Настройки")
        title_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        separator = QFrame()
        separator.setObjectName("separator")
        separator.setFixedHeight(1)
        layout.addWidget(separator)

        settings_layout = QVBoxLayout()
        settings_layout.setSpacing(15)

        startup_widget = QWidget()
        startup_layout = QHBoxLayout(startup_widget)
        startup_layout.setContentsMargins(0, 0, 0, 0)

        startup_label = QLabel("Запускать при старте Windows")
        startup_label.setStyleSheet("font-size: 14px;")
        startup_layout.addWidget(startup_label)
        startup_layout.addStretch()

        self.startup_toggle = SettingsToggleSwitch(initial_state=self.settings.get("start_with_windows", False))
        startup_layout.addWidget(self.startup_toggle)

        settings_layout.addWidget(startup_widget)

        minimized_widget = QWidget()
        minimized_layout = QHBoxLayout(minimized_widget)
        minimized_layout.setContentsMargins(0, 0, 0, 0)

        minimized_label = QLabel("Запускать свернутым в трей")
        minimized_label.setStyleSheet("font-size: 14px;")
        minimized_layout.addWidget(minimized_label)
        minimized_layout.addStretch()

        self.minimized_toggle = SettingsToggleSwitch(initial_state=self.settings.get("start_minimized", False))
        minimized_layout.addWidget(self.minimized_toggle)

        settings_layout.addWidget(minimized_widget)

        tray_widget = QWidget()
        tray_layout = QHBoxLayout(tray_widget)
        tray_layout.setContentsMargins(0, 0, 0, 0)

        tray_label = QLabel("Показывать иконку в трее")
        tray_label.setStyleSheet("font-size: 14px;")
        tray_layout.addWidget(tray_label)
        tray_layout.addStretch()

        self.tray_toggle = SettingsToggleSwitch(initial_state=self.settings.get("show_in_tray", True))
        tray_layout.addWidget(self.tray_toggle)

        settings_layout.addWidget(tray_widget)

        autostart_widget = QWidget()
        autostart_layout = QHBoxLayout(autostart_widget)
        autostart_layout.setContentsMargins(0, 0, 0, 0)

        autostart_label = QLabel("Автоматически запускать прокси")
        autostart_label.setStyleSheet("font-size: 14px;")
        autostart_layout.addWidget(autostart_label)
        autostart_layout.addStretch()

        self.autostart_toggle = SettingsToggleSwitch(initial_state=self.settings.get("auto_start_proxy", True))
        autostart_layout.addWidget(self.autostart_toggle)

        settings_layout.addWidget(autostart_widget)

        closetotray_widget = QWidget()
        closetotray_layout = QHBoxLayout(closetotray_widget)
        closetotray_layout.setContentsMargins(0, 0, 0, 0)

        closetotray_label = QLabel("Закрывать в трей (вместо выхода)")
        closetotray_label.setStyleSheet("font-size: 14px;")
        closetotray_layout.addWidget(closetotray_label)
        closetotray_layout.addStretch()

        self.closetotray_toggle = SettingsToggleSwitch(initial_state=self.settings.get("close_to_tray", True))
        closetotray_layout.addWidget(self.closetotray_toggle)

        settings_layout.addWidget(closetotray_widget)

        layout.addLayout(settings_layout)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(15)

        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("saveButton")
        self.save_btn.setFixedHeight(40)
        self.save_btn.clicked.connect(self.on_save)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        btn_layout.addWidget(self.save_btn)

        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.setObjectName("cancelButton")
        self.cancel_btn.setFixedHeight(40)
        self.cancel_btn.clicked.connect(self.reject)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        btn_layout.addWidget(self.cancel_btn)

        layout.addLayout(btn_layout)
        self.setLayout(layout)

    def on_save(self):
        self.new_settings = {
            "theme": _settings.get("theme", "dark"),
            "start_with_windows": self.startup_toggle.isChecked(),
            "start_minimized": self.minimized_toggle.isChecked(),
            "show_in_tray": self.tray_toggle.isChecked(),
            "auto_start_proxy": self.autostart_toggle.isChecked(),
            "close_to_tray": self.closetotray_toggle.isChecked(),
        }
        self.accept()

class ConfigDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.config = config.copy()
        self.setWindowTitle("Samsuka — Конфигурация прокси")
        self.setFixedSize(450, 550)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setStyleSheet(get_stylesheet(_settings.get("theme", "dark")))

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        title_label = QLabel("Настройки прокси")
        title_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        separator = QFrame()
        separator.setObjectName("separator")
        separator.setFixedHeight(1)
        layout.addWidget(separator)

        form_layout = QVBoxLayout()
        form_layout.setSpacing(15)

        host_label = QLabel("IP-адрес прокси")
        host_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        form_layout.addWidget(host_label)

        self.host_edit = QLineEdit()
        self.host_edit.setText(self.config.get("host", "127.0.0.1"))
        self.host_edit.setPlaceholderText("127.0.0.1")
        form_layout.addWidget(self.host_edit)

        port_label = QLabel("Порт прокси")
        port_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        form_layout.addWidget(port_label)

        self.port_edit = QLineEdit()
        self.port_edit.setText(str(self.config.get("port", 1080)))
        self.port_edit.setPlaceholderText("1080")
        self.port_edit.setFixedWidth(150)
        form_layout.addWidget(self.port_edit)

        dc_label = QLabel("DC → IP маппинги")
        dc_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        form_layout.addWidget(dc_label)

        dc_info = QLabel("По одному на строку, формат DC:IP")
        dc_info.setStyleSheet("color: #7f8c8d; font-size: 12px;")
        form_layout.addWidget(dc_info)

        self.dc_text = QTextEdit()
        self.dc_text.setFixedHeight(120)
        self.dc_text.setPlainText("\n".join(self.config.get("dc_ip", DEFAULT_CONFIG["dc_ip"])))
        form_layout.addWidget(self.dc_text)

        self.verbose_check = QCheckBox("Подробное логирование (verbose)")
        self.verbose_check.setChecked(self.config.get("verbose", False))
        self.verbose_check.setStyleSheet("font-size: 14px;")
        form_layout.addWidget(self.verbose_check)

        info_label = QLabel("Изменения вступят в силу после перезапуска прокси.")
        info_label.setStyleSheet("color: #7f8c8d; font-size: 12px; font-style: italic;")
        info_label.setAlignment(Qt.AlignCenter)
        form_layout.addWidget(info_label)

        layout.addLayout(form_layout)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(15)

        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("saveButton")
        self.save_btn.setFixedHeight(40)
        self.save_btn.clicked.connect(self.on_save)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        btn_layout.addWidget(self.save_btn)

        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.setObjectName("cancelButton")
        self.cancel_btn.setFixedHeight(40)
        self.cancel_btn.clicked.connect(self.reject)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        btn_layout.addWidget(self.cancel_btn)

        layout.addLayout(btn_layout)
        self.setLayout(layout)

    def on_save(self):
        import socket as _sock
        host_val = self.host_edit.text().strip()
        try:
            _sock.inet_aton(host_val)
        except OSError:
            _show_error("Некорректный IP-адрес.")
            return

        try:
            port_val = int(self.port_edit.text().strip())
            if not (1 <= port_val <= 65535):
                raise ValueError
        except ValueError:
            _show_error("Порт должен быть числом 1-65535")
            return

        lines = [l.strip() for l in self.dc_text.toPlainText().strip().splitlines()
                 if l.strip()]
        try:
            tg_ws_proxy.parse_dc_ip_list(lines)
        except ValueError as e:
            _show_error(str(e))
            return

        self.new_cfg = {
            "host": host_val,
            "port": port_val,
            "dc_ip": lines,
            "verbose": self.verbose_check.isChecked(),
            "enabled": self.config.get("enabled", True),
        }
        self.accept()

class FirstRunDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Samsuka")
        self.setFixedSize(550, 500)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setStyleSheet(get_stylesheet("dark"))

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        logo_label = QLabel("⚡")
        logo_label.setStyleSheet("""
            QLabel {
                color: #3498db;
                font-size: 48px;
                font-weight: bold;
            }
        """)
        logo_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo_label)

        title_label = QLabel("Добро пожаловать в Samsuka!")
        title_label.setStyleSheet("font-size: 24px; font-weight: bold; color: #3498db;")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        subtitle_label = QLabel("Telegram WebSocket Proxy")
        subtitle_label.setStyleSheet("font-size: 16px; color: #7f8c8d;")
        subtitle_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle_label)

        layout.addSpacing(20)

        info_frame = QFrame()
        info_frame.setObjectName("infoFrame")
        info_layout = QVBoxLayout(info_frame)
        info_layout.setSpacing(15)

        host = self.config.get("host", DEFAULT_CONFIG["host"])
        port = self.config.get("port", DEFAULT_CONFIG["port"])
        tg_url = f"tg://socks?server={host}&port={port}"

        sections = [
            ("🚀 Как подключить Telegram Desktop:", True),
            ("", False),
            ("  1. Автоматически:", True),
            (f"     🔹 ПКМ по иконке в трее → «Открыть в Telegram»", False),
            (f"     🔹 Или перейди по ссылке: {tg_url}", False),
            ("", False),
            ("  2. Вручную:", True),
            ("     🔹 Настройки → Продвинутые → Тип подключения → Прокси", False),
            (f"     🔹 SOCKS5 → {host} : {port} (без логина/пароля)", False),
        ]

        for text, bold in sections:
            if not text:
                info_layout.addSpacing(5)
                continue
            label = QLabel(text)
            if bold:
                label.setStyleSheet("font-weight: bold; color: #3498db; font-size: 14px;")
            else:
                label.setStyleSheet("color: #e0e0e0; font-size: 13px;")
            info_layout.addWidget(label)

        layout.addWidget(info_frame)

        layout.addSpacing(20)

        self.auto_start_check = QCheckBox("Автоматически запускать прокси при старте")
        self.auto_start_check.setChecked(True)
        self.auto_start_check.setStyleSheet("font-size: 14px;")
        layout.addWidget(self.auto_start_check)

        self.auto_connect_check = QCheckBox("Открыть прокси в Telegram сейчас")
        self.auto_connect_check.setChecked(True)
        self.auto_connect_check.setStyleSheet("font-size: 14px;")
        layout.addWidget(self.auto_connect_check)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("Начать работу")
        self.start_btn.setObjectName("saveButton")
        self.start_btn.setFixedHeight(50)
        self.start_btn.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.start_btn.clicked.connect(self.accept)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        btn_layout.addWidget(self.start_btn)

        layout.addLayout(btn_layout)

        self.setLayout(layout)

def _run_proxy_thread(port: int, dc_opt: Dict[int, str], verbose: bool,
                      host: str = '127.0.0.1'):
    global _async_stop

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)

    stop_ev = _asyncio.Event()
    _async_stop = (loop, stop_ev)

    try:
        loop.run_until_complete(
            tg_ws_proxy._run(port, dc_opt, stop_event=stop_ev, host=host))
    except Exception as exc:
        log.error("Proxy thread crashed: %s", exc)
        if "10048" in str(exc) or "Address already in use" in str(exc):
            _signal_emitter.show_error(
                "Не удалось запустить прокси:\nПорт уже используется другим приложением.\n\nЗакройте приложение, использующее этот порт, или измените порт в настройках прокси и перезапустите.")
    finally:
        try:
            loop.stop()
            loop.close()
        except:
            pass
        _async_stop = None
        log.info("Proxy thread finished")

def start_proxy():
    global _proxy_thread, _config, _async_stop

    if _proxy_thread and _proxy_thread.is_alive():
        log.info("Proxy already running")
        _signal_emitter.proxy_started_signal.emit()
        return

    cfg = _config
    port = cfg.get("port", DEFAULT_CONFIG["port"])
    host = cfg.get("host", DEFAULT_CONFIG["host"])
    dc_ip_list = cfg.get("dc_ip", DEFAULT_CONFIG["dc_ip"])
    verbose = cfg.get("verbose", False)

    try:
        dc_opt = tg_ws_proxy.parse_dc_ip_list(dc_ip_list)
        log.info(f"DC options: {dc_opt}")
    except ValueError as e:
        log.error("Bad config dc_ip: %s", e)
        _signal_emitter.show_error(f"Ошибка конфигурации:\n{e}")
        return

    log.info("Starting proxy on %s:%d ...", host, port)

    _async_stop = None

    _proxy_thread = threading.Thread(
        target=_run_proxy_thread,
        args=(port, dc_opt, verbose, host),
        daemon=True,
        name="proxy")
    _proxy_thread.start()

    time.sleep(0.5)

    if _proxy_thread.is_alive():
        cfg["enabled"] = True
        save_config(cfg)

        _signal_emitter.proxy_started_signal.emit()
        log.info("Proxy started successfully")
    else:
        log.error("Proxy failed to start")
        _signal_emitter.show_error("Не удалось запустить прокси. Проверьте логи.")
        _signal_emitter.proxy_stopped_signal.emit()

def stop_proxy():
    global _proxy_thread, _async_stop

    if not _proxy_thread or not _proxy_thread.is_alive():
        log.info("Proxy not running")
        _config["enabled"] = False
        save_config(_config)
        _signal_emitter.proxy_stopped_signal.emit()
        return

    log.info("Stopping proxy...")

    if _async_stop:
        loop, stop_ev = _async_stop
        try:
            loop.call_soon_threadsafe(stop_ev.set)
            log.info("Stop event set")
        except Exception as e:
            log.error(f"Error setting stop event: {e}")

    if _proxy_thread and _proxy_thread.is_alive():
        _proxy_thread.join(timeout=3)
        if _proxy_thread.is_alive():
            log.warning("Proxy thread did not stop gracefully")

    _proxy_thread = None
    _async_stop = None

    _config["enabled"] = False
    save_config(_config)

    _signal_emitter.proxy_stopped_signal.emit()
    log.info("Proxy stopped")

def restart_proxy():
    log.info("Restarting proxy...")

    def _do_restart():
        stop_proxy()
        time.sleep(1)
        start_proxy()

    threading.Thread(target=_do_restart, daemon=True).start()

def _create_tray_menu():
    global _tray_menu

    menu = CustomTrayMenu()

    host = _config.get("host", DEFAULT_CONFIG["host"])
    port = _config.get("port", DEFAULT_CONFIG["port"])

    show_action = QAction("Показать Samsuka", menu)
    show_action.triggered.connect(lambda: _main_window.show() if _main_window else None)
    menu.addAction(show_action)

    menu.addSeparator()

    open_action = QAction(f"Открыть в Telegram ({host}:{port})", menu)
    open_action.triggered.connect(_on_open_in_telegram)
    menu.addAction(open_action)

    menu.addSeparator()

    restart_action = QAction("Перезапустить прокси", menu)
    restart_action.triggered.connect(_on_restart)
    menu.addAction(restart_action)

    config_action = QAction("Настройки прокси...", menu)
    config_action.triggered.connect(_on_edit_config)
    menu.addAction(config_action)

    settings_action = QAction("Настройки приложения...", menu)
    settings_action.triggered.connect(_on_edit_settings)
    menu.addAction(settings_action)

    logs_action = QAction("Открыть логи", menu)
    logs_action.triggered.connect(_on_open_logs)
    menu.addAction(logs_action)

    menu.addSeparator()

    exit_action = QAction("Выход", menu)
    exit_action.triggered.connect(_on_exit)
    menu.addAction(exit_action)

    _tray_menu = menu
    return menu

def _show_error(text: str, title: str = "Samsuka — Ошибка"):
    if _qt_app and threading.current_thread() is threading.main_thread():
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Critical)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setStyleSheet(get_stylesheet(_settings.get("theme", "dark")))
        msg_box.exec_()
    else:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)

def _show_info(text: str, title: str = "Samsuka"):
    if _qt_app and threading.current_thread() is threading.main_thread():
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Information)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setStyleSheet(get_stylesheet(_settings.get("theme", "dark")))
        msg_box.exec_()
    else:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)

def _on_open_in_telegram():
    port = _config.get("port", DEFAULT_CONFIG["port"])
    url = f"tg://socks?server=127.0.0.1&port={port}"
    log.info("Opening %s", url)
    try:
        result = webbrowser.open(url)
        if not result:
            raise RuntimeError("webbrowser.open returned False")
    except Exception:
        log.info("Browser open failed, copying to clipboard")
        try:
            pyperclip.copy(url)
            _show_info(
                f"Не удалось открыть Telegram автоматически.\n\n"
                f"Ссылка скопирована в буфер обмена, отправьте её в телеграмм и нажмите по ней ЛКМ:\n{url}",
                "Samsuka")
        except Exception as exc:
            log.error("Clipboard copy failed: %s", exc)
            _show_error(f"Не удалось скопировать ссылку:\n{exc}")

def _on_restart():
    threading.Thread(target=restart_proxy, daemon=True).start()

def _on_edit_config():
    threading.Thread(target=_edit_config_dialog, daemon=True).start()

def _on_edit_settings():
    threading.Thread(target=_edit_settings_dialog, daemon=True).start()

def _on_open_logs():
    log.info("Opening log file: %s", LOG_FILE)
    if LOG_FILE.exists():
        os.startfile(str(LOG_FILE))
    else:
        _show_info("Файл логов ещё не создан.", "Samsuka")

def _on_exit():
    global _exiting
    if _exiting:
        os._exit(0)
        return
    _exiting = True
    log.info("User requested exit")

    stop_proxy()

    def _force_exit():
        time.sleep(2)
        os._exit(0)

    threading.Thread(target=_force_exit, daemon=True, name="force-exit").start()

    if _qt_app:
        _qt_app.quit()

def _edit_config_dialog():
    global _config, _tray_menu, _main_window

    if not _qt_app:
        return

    dialog = ConfigDialog(_config)
    if dialog.exec_() == QDialog.Accepted:
        new_cfg = dialog.new_cfg
        save_config(new_cfg)
        _config.update(new_cfg)
        log.info("Config saved: %s", new_cfg)

        if _tray_icon and _settings.get("show_in_tray", True):
            _tray_icon.setContextMenu(_create_tray_menu())

        if _main_window and _main_window.isVisible():
            _main_window.close()
            _main_window = MainWindow()
            _main_window.show()

        reply = QMessageBox.question(
            None, "Перезапустить?",
            "Настройки сохранены.\n\nПерезапустить прокси сейчас?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            restart_proxy()

def _edit_settings_dialog():
    global _settings, _tray_icon, _main_window, _qt_app

    if not _qt_app:
        return

    dialog = SettingsDialog(_settings)
    if dialog.exec_() == QDialog.Accepted:
        new_settings = dialog.new_settings
        save_settings(new_settings)
        _settings.update(new_settings)
        log.info("Settings saved: %s", new_settings)

        add_to_startup()

        if _tray_icon:
            if _settings.get("show_in_tray", True):
                _tray_icon.show()
                _tray_icon.setContextMenu(_create_tray_menu())
            else:
                _tray_icon.hide()

        if _main_window:
            _main_window.apply_theme()

def _show_first_run():
    global _config, _settings

    _ensure_dirs()
    if FIRST_RUN_MARKER.exists():
        return

    if not _qt_app:
        FIRST_RUN_MARKER.touch()
        return

    dialog = FirstRunDialog(_config)
    if dialog.exec_() == QDialog.Accepted:
        if not dialog.auto_start_check.isChecked():
            _settings["auto_start_proxy"] = False
            save_settings(_settings)

        FIRST_RUN_MARKER.touch()

        if dialog.auto_connect_check.isChecked():
            _on_open_in_telegram()

def _has_ipv6_enabled() -> bool:
    import socket as _sock
    try:
        addrs = _sock.getaddrinfo(_sock.gethostname(), None, _sock.AF_INET6)
        for addr in addrs:
            ip = addr[4][0]
            if ip and not ip.startswith('::1') and not ip.startswith('fe80::1'):
                return True
    except Exception:
        pass
    try:
        s = _sock.socket(_sock.AF_INET6, _sock.SOCK_STREAM)
        s.bind(('::1', 0))
        s.close()
        return True
    except Exception:
        return False

def _check_ipv6_warning():
    _ensure_dirs()
    if IPV6_WARN_MARKER.exists():
        return
    if not _has_ipv6_enabled():
        return

    IPV6_WARN_MARKER.touch()

    threading.Thread(target=_show_ipv6_dialog, daemon=True).start()

def _show_ipv6_dialog():
    _show_info(
        "На вашем компьютере включён IPv6.\n\n"
        "Telegram может пытаться подключаться через IPv6, "
        "что не поддерживается и может привести к ошибкам.\n\n"
        "Если прокси не работает или в логах видны ошибки связанные с IPv6, "
        "то проверьте в настройках Telegram, что рядом с настройкой прокси не включён "
        "пункт про IPv6. Если это не поможет, то выключите IPv6 в системе\n\n"
        "Это предупреждение будет показано только один раз.",
        "Samsuka")

def run_app():
    global _tray_icon, _config, _qt_app, _main_window, _settings

    _config = load_config()
    save_config(_config)

    _settings = load_settings()
    save_settings(_settings)

    if LOG_FILE.exists():
        try:
            LOG_FILE.unlink()
        except Exception:
            pass

    setup_logging(_config.get("verbose", False))
    log.info("=" * 60)
    log.info("Samsuka starting")
    log.info("Config: %s", _config)
    log.info("Settings: %s", _settings)
    log.info("Log file: %s", LOG_FILE)
    log.info("=" * 60)

    add_to_startup()

    if not QApplication.instance():
        _qt_app = QApplication(sys.argv)
        _qt_app.setQuitOnLastWindowClosed(False)
    else:
        _qt_app = QApplication.instance()

    _signal_emitter.show_error_signal.connect(_show_error)
    _signal_emitter.show_info_signal.connect(_show_info)

    if _settings.get("auto_start_proxy", True) and _config.get("enabled", True):
        log.info("Auto-starting proxy...")
        QTimer.singleShot(500, start_proxy)

    _main_window = MainWindow()

    if _settings.get("show_in_tray", True):
        _tray_icon = QSystemTrayIcon()
        _tray_icon.setIcon(_load_icon())
        _tray_icon.setToolTip("Samsuka")

        menu = _create_tray_menu()
        _tray_icon.setContextMenu(menu)

        _tray_icon.activated.connect(
            lambda reason: _main_window.show() if reason == QSystemTrayIcon.DoubleClick else None)

        _tray_icon.show()

    QTimer.singleShot(100, _show_first_run)
    QTimer.singleShot(200, _check_ipv6_warning)

    if not _settings.get("start_minimized", False):
        QTimer.singleShot(300, _main_window.show)

    log.info("Application running")

    exit_code = _qt_app.exec_()

    stop_proxy()
    log.info("App exited")

    return exit_code

def main():
    if not _acquire_lock():
        _show_info("Приложение уже запущено.", os.path.basename(sys.argv[0]))
        return

    try:
        exit_code = run_app()
        sys.exit(exit_code if exit_code is not None else 0)
    finally:
        _release_lock()

if __name__ == "__main__":
    main()