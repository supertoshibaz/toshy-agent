"""Independent update window, running from a snapshot in the OLD environment."""
import argparse
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
from . import layout
from .engine import Installer
from .environments import launch
from .network import ReleaseError


def stop_started_process(process, *, directory=None, token=None, job=None):
    from .trial import stop
    stop(process, directory=directory, token=token, job=job)


def apply_and_start(root, progress, *, wait_seconds=180, startup_seconds=90):
    """No GUI APIs in this function; usable by both the window and tests."""
    root = Path(root)
    deadline = time.monotonic() + wait_seconds
    progress('Waiting for Toshy and its core to close…')
    while layout.running(root):
        if time.monotonic() >= deadline:
            raise ReleaseError('Toshy or its core is still running. Stop it and retry. No active files were changed.')
        time.sleep(0.2)
    engine = Installer(root, progress=progress, wait=False)
    # Never overwrite an interrupted transaction before activate() recovers it.
    engine.journal = layout.read_json(root / 'transaction.json')
    process = None
    job = None
    trial_dir = None
    token = None
    changed = False
    committed = False
    try:
        progress('Applying the prepared update…')
        engine.activate(wait_for_startup=True)
        changed = True
        state = layout.owned(root)
        ready = root / 'runtime/installer' / ('startup-' + uuid.uuid4().hex + '.json')
        token = uuid.uuid4().hex
        command, env = launch(root, state, ready_file=ready, native_host=True)
        env['TOSHY_UPDATE_READY_TOKEN'] = token
        trial_dir = ready.with_suffix('.processes')
        env['TOSHY_UPDATE_TRIAL_DIR'] = str(trial_dir)
        engine.record(ready_file=str(ready), ready_token=token)
        progress('Starting Toshy and waiting for its ready signal…')
        with (root / 'update-handoff.log').open('ab') as output:
            process = subprocess.Popen(command, env=env, cwd=root, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=os.name != 'nt',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if os.name == 'nt':
            from .trial import WindowsJob
            job = WindowsJob(process.pid)
        engine.record(trial_pid=process.pid)
        deadline = time.monotonic() + startup_seconds
        while time.monotonic() < deadline:
            try:
                acknowledgement = layout.read_json(ready)
            except (OSError, ValueError):
                acknowledgement = {}
            if acknowledgement.get('token') == token and acknowledgement.get('pid') == process.pid and process.poll() is None:
                # Once the GUI is ready it can accept user changes. A metadata
                # cleanup failure must never roll those changes back.
                committed = True
                from .portable import complete_activation
                with layout.lock(root):
                    complete_activation(engine)
                try:
                    ready.unlink(missing_ok=True)
                except OSError:
                    pass
                progress('Update complete. Toshy is ready.')
                if job is not None:
                    job.close()
                threading.Thread(target=process.wait, name='toshy-launched-app', daemon=True).start()
                return
            if process.poll() is not None:
                raise ReleaseError('The updated application exited before becoming ready. See update-handoff.log.')
            time.sleep(0.1)
        raise ReleaseError('The updated application did not become ready in time. See update-handoff.log.')
    except BaseException:
        if changed and not committed:
            if process is not None:
                stop_started_process(process, directory=trial_dir, token=token, job=job)
            layout.require_stopped(root)
            from .portable import undo_activation
            with layout.lock(root):
                undo_activation(engine)
            progress('Update failed. The previous version has been restored.')
        raise
    finally:
        if job is not None:
            job.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--language', default='en', choices=['en', 'ru'])
    parser.add_argument('--colors', default='')
    args = parser.parse_args(argv)
    # Kept outside import time so transaction tests require no Qt dependency.
    from PySide6.QtCore import QObject, Signal, Qt
    from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QProgressBar, QPushButton, QPlainTextEdit
    app = QApplication([])
    russian = args.language == 'ru'
    text = ({'title': 'Обновление Toshy', 'working': 'Установка обновления…', 'retry': 'Повторить',
             'open': 'Открыть Toshy', 'failed': 'Обновление не завершено. Подробности ниже.', 'close': 'Закрыть'} if russian else
            {'title': 'Updating Toshy', 'working': 'Installing update…', 'retry': 'Retry',
             'open': 'Open Toshy', 'failed': 'The update did not finish. Details are shown below.', 'close': 'Close'})

    class Relay(QObject):
        progress = Signal(str)
        finished = Signal(object)

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.busy = False
            self.relay = Relay(self)
            self.relay.progress.connect(self.show_progress)
            self.relay.finished.connect(self.done)
            self.setWindowTitle(text['title'])
            self.setFixedSize(520, 270)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            box = QVBoxLayout(self)
            self.label = QLabel(text['working'], self)
            self.label.setWordWrap(True)
            box.addWidget(self.label)
            self.bar = QProgressBar(self)
            self.bar.setRange(0, 0)
            box.addWidget(self.bar)
            self.details = QPlainTextEdit(self)
            self.details.setReadOnly(True)
            box.addWidget(self.details)
            self.retry = QPushButton(text['retry'], self)
            self.retry.clicked.connect(self.start)
            self.retry.hide()
            box.addWidget(self.retry)
            self.open = QPushButton(text['open'], self)
            self.open.clicked.connect(self.open_toshy)
            self.open.hide()
            box.addWidget(self.open)
            self.close_button = QPushButton(text['close'], self)
            self.close_button.clicked.connect(self.close)
            self.close_button.hide()
            box.addWidget(self.close_button)
            if args.colors:
                import json, re
                colors = json.loads(args.colors)
                if len(colors) == 3 and all(re.fullmatch(r'#[0-9a-fA-F]{6}', c) for c in colors):
                    self.setStyleSheet('QWidget {background:' + colors[0] + ';color:' + colors[1] + ';}'
                        'QProgressBar::chunk {background:' + colors[2] + ';} QPushButton {padding:5px;}')

        def show_progress(self, value):
            # One current line instead of flooding the UI with command output.
            self.details.setPlainText(value[-3000:])
            with (args.root / 'update-handoff.log').open('a', encoding='utf-8') as log:
                log.write(value + '\n')

        def start(self):
            if self.busy:
                return
            self.busy = True
            self.retry.hide(); self.open.hide(); self.close_button.hide()
            self.label.setText(text['working'])
            self.bar.setRange(0, 0)
            def work():
                error = None
                try:
                    apply_and_start(args.root, self.relay.progress.emit)
                except BaseException as caught:
                    error = str(caught)
                self.relay.finished.emit(error)
            threading.Thread(target=work, name='toshy-updater', daemon=False).start()

        def done(self, error):
            self.busy = False
            if error is None:
                self.close()
                return
            self.label.setText(text['failed'])
            self.details.setPlainText(error)
            self.bar.setRange(0, 1); self.bar.setValue(0)
            self.retry.show(); self.open.show(); self.close_button.show()

        def open_toshy(self):
            if layout.running(args.root):
                self.close()
                return
            try:
                from .portable import recover
                with layout.lock(args.root):
                    recover(Installer(args.root), layout.owned(args.root))
                command, env = launch(args.root, layout.owned(args.root), native_host=True)
                subprocess.Popen(command, env=env, cwd=args.root, stdin=subprocess.DEVNULL,
                    start_new_session=os.name != 'nt', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except Exception as error:
                self.details.setPlainText(str(error))
                return
            self.close()

        def closeEvent(self, event):
            if self.busy:
                event.ignore()
            else:
                event.accept()

    window = Window()
    window.show()
    window.start()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
