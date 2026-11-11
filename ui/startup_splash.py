"""Минимальный GUI-процесс заставки; EOF закрывает его и при сбое родителя."""
import json
import sys
import threading

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication
from ui.startup import StartupSplash


class StartupPipe(QObject):
    phase = Signal(str)
    finished = Signal()

    def read(self):
        try:
            for line in sys.stdin:
                try:
                    message = json.loads(line)
                    if isinstance(message.get('phase'), str):
                        self.phase.emit(message['phase'])
                except (ValueError, TypeError, AttributeError):
                    continue
        finally:
            self.finished.emit()


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    from ui.application_icon import prepare_application_identity, configure_application_icon
    prepare_application_identity()
    app = QApplication([])
    configure_application_icon(app)
    from ui.state_manager import StateManager
    from ui.i18n import i18n
    from ui.themes.manager import theme_manager
    preferences = StateManager()
    i18n.set_language(preferences.get('language', 'en'))
    theme_manager.select(preferences.get('theme', 'toshy'), application=app)
    splash = StartupSplash()
    pipe = StartupPipe()
    pipe.phase.connect(splash.set_phase, Qt.QueuedConnection)
    pipe.finished.connect(app.quit, Qt.QueuedConnection)
    splash.show_centered()
    threading.Thread(target=pipe.read, name='toshy-startup-status', daemon=True).start()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
