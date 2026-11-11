from ui.i18n import tr
import os
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QFont, QFontDatabase

from ui.startup import StartupSplash, StartupTracker, set_active_startup_tracker
from ui.application_icon import prepare_application_identity, configure_application_icon
from elements.lsp import lsp_manager


def run_app():
    from ui.startup_process import start_startup_display, report_startup_phase, stop_startup_display
    display = start_startup_display()
    report_startup_phase(tr('ui.caption.1f3fb73b472f'))
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    prepare_application_identity()
    app = QApplication(sys.argv)
    app.aboutToQuit.connect(stop_startup_display)
    from ui.shutdown import shutdown_background
    app.aboutToQuit.connect(shutdown_background)
    configure_application_icon(app)
    if app.platformName().startswith('wayland'):
        # Диалоги Qt обходят ошибки KIO и нативного захвата Breeze на Wayland.
        app.setAttribute(Qt.AA_DontUseNativeDialogs, True)
    from ui.text_navigation import install_text_navigation
    install_text_navigation(app)
    # Stop language servers while QApplication and their QProcess wrappers are
    # still fully alive. Relying on QObject destruction is too late and causes
    # "QProcess: Destroyed while process ... is still running" on exit.
    app.aboutToQuit.connect(lsp_manager.shutdown_all)

    # 1. Загрузка встроенного файла эмодзи
    font_path = Path(__file__).parent / "fonts" / "NotoColorEmoji.ttf"
    emoji_family = None
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path.resolve()))
        loaded_families = QFontDatabase.applicationFontFamilies(font_id)
        if loaded_families:
            emoji_family = loaded_families[0]

    # 2. Настройка базового шрифта без засечек (Sans-Serif)
    if sys.platform == "win32":
        sans_families = ["Segoe UI", "Arial", "sans-serif"]
    else:
        sans_families = ["Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans", "sans-serif"]

    default_font = QFont()
    default_font.setPointSize(10)
    default_font.setFamilies(sans_families)
    default_font.setStyleHint(QFont.SansSerif)

    # 3. Подстановка эмодзи к шрифтам без засечек
    emoji_fallbacks = []
    if emoji_family:
        emoji_fallbacks.append(emoji_family)
    emoji_fallbacks.extend(["Segoe UI Emoji", "Noto Color Emoji", "Noto Emoji"])

    for fam in sans_families + ["Sans Serif", "DejaVu Sans"]:
        QFont.insertSubstitutions(fam, emoji_fallbacks)

    app.setFont(default_font)
    from ui.state_manager import StateManager
    from ui.themes.manager import theme_manager
    from ui.i18n import i18n
    appearance = StateManager()
    i18n.set_language(appearance.get('language', 'en'))
    theme_manager.select(appearance.get('theme', 'toshy'), application=app)

    tracker = StartupTracker()
    set_active_startup_tracker(tracker)
    construction_token = tracker.begin("ui:main-window-construction")

    splash = None
    if display is None or not display.is_running():
        splash = StartupSplash()
        tracker.phase_changed.connect(splash.set_phase)
        splash.show_centered()
    app.processEvents()

    finished = {"value": False}
    window_holder = {"window": None}

    def reveal(force: bool = False):
        if finished["value"]:
            return
        window = window_holder["window"]
        if window is None:
            return
        if not force and not tracker.is_ready:
            return
        finished["value"] = True
        # Startup tracking only covers the first complete UI load. Recurring polls
        # and user actions after reveal must never hold the application again.
        set_active_startup_tracker(None)
        from ui.startup_data import end_startup_data
        end_startup_data()
        window.show()
        window.restore_last_chat()
        from ui.core_bridge.process_manager import core_process
        core_process.status_changed.connect(lambda online,*_: window.restore_last_chat() if online else None)
        window.raise_()
        window.activateWindow()
        ready_file = os.environ.get('TOSHY_UPDATE_READY_FILE')
        if ready_file:
            from release_tools.layout import write_json
            write_json(ready_file, {'pid': os.getpid(), 'token': os.environ.get('TOSHY_UPDATE_READY_TOKEN', '')})
        stop_startup_display()
        if splash is not None:
            splash.close()
            splash.deleteLater()

    tracker.ready.connect(reveal)
    # Show the splash before importing the complete editor/widget graph.
    report_startup_phase(tr('ui.caption.1ffb76e11012'))
    from ui.main_window import MainWindow
    window_holder["window"] = MainWindow(startup_tracker=tracker)
    tracker.end(construction_token)
    tracker.seal()
    QTimer.singleShot(15000, lambda: reveal(True))

    try:
        exit_code = app.exec()
    finally:
        from ui.shutdown import finish_interactive
        finish_interactive(app)
    sys.exit(exit_code)
