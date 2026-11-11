"""Stdlib-only logging before importing the application, Qt, or its splash."""
import ctypes
import faulthandler
import os
from pathlib import Path
import sys
import tempfile
import traceback


def main():
    root = Path(os.environ.get('TOSHY_INSTALL_ROOT') or Path(__file__).resolve().parents[2])
    log = Path(os.environ.get('TOSHY_STARTUP_LOG') or root / 'runtime/startup.log')
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        stream = log.open('a', encoding='utf-8', buffering=1)
    except OSError:
        log = Path(tempfile.gettempdir()) / ('toshy-startup-' + str(os.getpid()) + '.log')
        stream = log.open('a', encoding='utf-8', buffering=1)
    os.environ['TOSHY_STARTUP_LOG'] = str(log)
    sys.stdout = sys.stderr = stream
    faulthandler.enable(stream, all_threads=True)
    try:
        print('Starting Toshy GUI. Interpreter:', sys.executable, flush=True)
        inprocess = bool(sys.argv[1:] and sys.argv[1] == '--toshy-inprocess')
        if inprocess:
            del sys.argv[1]
        if inprocess or os.environ.get('TOSHY_WINDOWS_HOST_PROCESS') == str(os.getpid()):
            os.environ['TOSHY_GUI_PROCESS'] = str(os.getpid())
        sys.path.insert(0, str(root / 'launcher'))
        from release_tools.windows_shell import app_id
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id(root))
        from release_tools.cli import gui
        gui(root, sys.argv[1:])
        return 0
    except SystemExit as error:
        if error.code is not None and not isinstance(error.code, int):
            print(error.code, file=sys.stderr, flush=True)
        return error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    except BaseException:
        traceback.print_exc()
        stream.flush()
        if os.environ.get('TOSHY_WINDOWS_HOST_PROCESS') != str(os.getpid()):
            ctypes.windll.user32.MessageBoxW(None, 'Toshy could not start.\n\nDetails: ' + str(log), 'Toshy startup error', 0x10)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
