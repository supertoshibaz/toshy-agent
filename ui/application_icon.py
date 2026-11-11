"""Shared SVG and desktop identity for Toshy's windows and launchers."""

import sys
import os
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QIODevice, QSaveFile, QStandardPaths
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtSvg import QSvgRenderer

from core.paths import APP_PATHS

APPLICATION_ID = "org.toshy.AgentStudio"
APPLICATION_NAME = "Toshy Agent Studio"
LOGO_PATH = Path(__file__).parent / "assets" / "toshy_logo.svg"


def _warn(message):
    from core.logger import core_logger

    core_logger.warn("APP_ICON", message)


def prepare_application_identity():
    """Set platform identity before QApplication creates native windows."""
    QCoreApplication.setApplicationName(APPLICATION_ID)
    QGuiApplication.setApplicationDisplayName(APPLICATION_NAME)
    QGuiApplication.setDesktopFileName(APPLICATION_ID)
    if sys.platform == "win32":
        import ctypes

        try:
            setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            setter.argtypes = [ctypes.c_wchar_p]
            setter.restype = ctypes.c_long
            result = setter(APPLICATION_ID)
            if result < 0:
                raise OSError(f"AppUserModelID: HRESULT {result & 0xffffffff:#x}")
        except (OSError, AttributeError) as error:
            _warn(f"Не удалось задать идентификатор приложения Windows: {error}")


def _desktop_string(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _desktop_argument(value):
    text = str(value).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        text = text.replace(character, "\\" + character)
    return _desktop_string('"' + text + '"')


def _register_linux_launcher():
    if os.environ.get("TOSHY_INSTALL_ROOT"):
        return  # The managed launcher must survive version switches.
    """Associate the Wayland app ID with an icon and a reusable launcher."""
    location = QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation)
    if not location:
        raise OSError("Не определена папка пользовательских приложений.")
    path = Path(location) / "applications" / (APPLICATION_ID + ".desktop")
    marker = "X-Toshy-Generated=true"
    executable = str(Path(sys.executable).absolute())
    if "=" in executable:
        raise ValueError(
            "Путь Python содержит недопустимый для Desktop Entry символ «=»."
        )
    command = " ".join(
        _desktop_argument(value) for value in (executable, APP_PATHS.code_root / "run_ui.py")
    )
    content = "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APPLICATION_NAME}",
            "Comment=Graph-based AI agent studio",
            f"Exec={command}",
            "Path=" + _desktop_string(APP_PATHS.root),
            "Icon=" + _desktop_string(LOGO_PATH.resolve()),
            "Terminal=false",
            "Categories=Development;Utility;",
            f"StartupWMClass={APPLICATION_ID}",
            "StartupNotify=true",
            marker,
            "",
        )
    )
    if path.exists():
        previous = path.read_text(encoding="utf-8")
        if previous == content:
            return
        if marker not in previous.splitlines():
            _warn(f"Существующий ярлык не изменён: {path}")
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    target = QSaveFile(str(path))
    if not target.open(QIODevice.WriteOnly):
        raise OSError(target.errorString())
    encoded = content.encode("utf-8")
    if target.write(encoded) != len(encoded) or not target.commit():
        raise OSError(target.errorString())


def configure_application_icon(app):
    """All top-level widgets inherit this icon, including secondary windows."""
    if not LOGO_PATH.is_file():
        _warn(f"Не найден файл иконки приложения: {LOGO_PATH}")
        return
    if not QSvgRenderer(str(LOGO_PATH)).isValid():
        _warn(f"Некорректный SVG иконки приложения: {LOGO_PATH}")
        return
    app.setWindowIcon(QIcon(str(LOGO_PATH)))
    if sys.platform.startswith("linux"):
        try:
            _register_linux_launcher()
        except (OSError, ValueError) as error:
            _warn(f"Не удалось зарегистрировать ярлык Toshy: {error}")
