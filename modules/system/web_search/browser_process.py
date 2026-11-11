"""Find a browser and supervise its process tree independently of the core."""

import ctypes
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


class BrowserUnavailable(ValueError):
    pass


def browser_directory():
    elements = os.environ.get('TOSHY_MANAGED_ELEMENTS')
    return (Path(elements) if elements else Path(os.environ.get("TOSHY_DATA_DIR") or Path(__file__).resolve().parents[3]) / 'elements') / 'browsers'


def _usable_browser(candidate):
    return candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK))


def _local_browser(root=None):
    root = root or browser_directory()
    try:
        record = json.loads((root / 'installed.json').read_text(encoding='utf-8'))
        directory = record.get('directory', '') if isinstance(record, dict) else ''
        import re
        if re.fullmatch(r'chromium-\d+(?:\.\d+){3}', directory):
            candidate = root / directory / ('chrome-headless-shell.exe' if os.name == 'nt' else 'chrome-headless-shell')
            if not (root / directory).is_symlink() and not candidate.is_symlink() and _usable_browser(candidate):
                return str(candidate.resolve())
    except (OSError, ValueError, TypeError):
        pass
    names = ("chrome-headless-shell", "chromium", "chrome", "msedge")
    if os.name == "nt":
        names = tuple(name + ".exe" for name in names)
    directories = [root]
    level = directories[:]
    # Cover extracted archives and one platform/version directory, without
    # searching profiles, following directory symlinks or scanning the app.
    for _ in range(2):
        children = []
        for directory in level:
            try:
                children.extend(
                    sorted(
                        (
                            path
                            for path in directory.iterdir()
                            if not path.name.startswith(".")
                            and not path.is_symlink()
                            and path.is_dir()
                        ),
                        key=lambda path: path.name.casefold(),
                    )
                )
            except OSError:
                continue
        level = children[: max(0, 128 - len(directories))]
        directories.extend(level)
    for name in names:
        for directory in directories:
            candidate = directory / name
            if _usable_browser(candidate):
                return str(candidate.resolve())
    return None


def find_browser(configured=""):
    if configured:
        candidate = Path(configured).expanduser()
        app_root = Path(os.environ.get('TOSHY_DATA_DIR') or Path(__file__).resolve().parents[3])
        if not candidate.is_absolute():
            candidate = app_root / candidate
        if not candidate.exists():
            try:
                relative = candidate.relative_to(app_root / "browsers")
            except ValueError:
                pass
            else:
                candidate = browser_directory() / relative
        if not _usable_browser(candidate):
            raise BrowserUnavailable(
                "Browser executable is unavailable. Check its path in Startup → Web Search."
            )
        return str(candidate.resolve())
    candidate = _local_browser()
    if not candidate and os.environ.get('TOSHY_MANAGED_ELEMENTS'):
        legacy = Path(os.environ.get('TOSHY_DATA_DIR') or Path(__file__).resolve().parents[3]) / 'elements/browsers'
        candidate = _local_browser(legacy)
    if candidate:
        return candidate
    for name in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome-headless-shell",
        "chrome",
        "msedge",
    ):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    if os.name == "nt":
        for root in (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if root:
                for tail in (
                    "Google/Chrome/Application/chrome.exe",
                    "Chromium/Application/chrome.exe",
                    "Microsoft/Edge/Application/msedge.exe",
                ):
                    candidate = Path(root) / tail
                    if candidate.is_file():
                        return str(candidate)
    raise BrowserUnavailable(
        "Chromium/Chrome not found. Install the private browser in elements/browsers "
        "or set its path in Startup → Web Search."
    )


class _WindowsJob:
    def __init__(self, process):
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_int64),
                ("JobTime", ctypes.c_int64),
                ("Flags", w.DWORD),
                ("Min", ctypes.c_size_t),
                ("Max", ctypes.c_size_t),
                ("Limit", w.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("Priority", w.DWORD),
                ("Scheduling", w.DWORD),
            ]

        class Io(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_uint64)
                for n in (
                    "ReadOps",
                    "WriteOps",
                    "OtherOps",
                    "ReadBytes",
                    "WriteBytes",
                    "OtherBytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("Basic", Basic),
                ("Io", Io),
                ("ProcessMemory", ctypes.c_size_t),
                ("JobMemory", ctypes.c_size_t),
                ("PeakProcess", ctypes.c_size_t),
                ("PeakJob", ctypes.c_size_t),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
        ]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.Flags = 0x2000
        if (
            not self.handle
            or not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            )
            or not self.api.AssignProcessToJobObject(self.handle, int(process._handle))
        ):
            self.close()
            raise OSError("Не удалось создать группу процессов браузера Windows.")

        # The browser is created suspended: no child can escape before job assignment.
        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("size", w.DWORD),
                ("usage", w.DWORD),
                ("tid", w.DWORD),
                ("pid", w.DWORD),
                ("base_priority", w.LONG),
                ("delta_priority", w.LONG),
                ("flags", w.DWORD),
            ]

        self.api.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = w.HANDLE
        self.api.Thread32First.argtypes = [w.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.api.Thread32Next.argtypes = [w.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.api.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenThread.restype = w.HANDLE
        self.api.ResumeThread.argtypes = [w.HANDLE]
        self.api.ResumeThread.restype = w.DWORD
        snapshot = self.api.CreateToolhelp32Snapshot(4, 0)
        resumed = False
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            more = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.pid == process.pid:
                    handle = self.api.OpenThread(2, False, entry.tid)
                    if handle:
                        try:
                            resumed = (
                                self.api.ResumeThread(handle) != 0xFFFFFFFF or resumed
                            )
                        finally:
                            self.api.CloseHandle(handle)
                more = self.api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        if not resumed:
            self.close()
            raise OSError("Не удалось запустить поток браузера Windows.")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _kill_tree(process, job):
    if job:
        job.close()
    elif os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def worker():
    stop = threading.Event()
    process, job = None, None

    def watch_parent():
        # EOF also arrives when the core crashes, not only on a normal close.
        sys.stdin.buffer.read(1)
        stop.set()

    try:
        raw = sys.stdin.buffer.readline(65536)
        config = json.loads(raw)
        executable = find_browser(config["path"])
        threading.Thread(target=watch_parent, daemon=True).start()
        with tempfile.TemporaryDirectory(prefix="toshy-web-") as profile:
            arguments = [
                executable,
                "--headless",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--user-data-dir=" + profile,
                "--proxy-server=http://127.0.0.1:" + str(int(config["block_port"])),
                "--proxy-bypass-list=<-loopback>",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--disable-extensions",
                "--disable-default-apps",
                "--disable-domain-reliability",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-features=MediaRouter,OptimizationHints",
                "about:blank",
            ]
            environment = {
                key: value
                for key, value in os.environ.items()
                if not any(
                    part in key.upper()
                    for part in (
                        "API_KEY",
                        "TOKEN",
                        "PASSWORD",
                        "SECRET",
                        "OPENAI_",
                        "ANTHROPIC_",
                    )
                )
            }
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(os.name == "posix"),
                env=environment,
                creationflags=(
                    (subprocess.CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW)
                    if os.name == "nt"
                    else 0
                ),
            )
            try:
                if os.name == "nt":
                    job = _WindowsJob(process)
                print(json.dumps({"pid": process.pid}), flush=True)
                deadline = time.monotonic() + 12
                while not stop.wait(0.05):
                    if process.poll() is not None:
                        raise BrowserUnavailable(
                            "Браузер завершился при запуске. Проверьте его установку и системные зависимости."
                        )
                    path = Path(profile) / "DevToolsActivePort"
                    if path.is_file():
                        lines = path.read_text().splitlines()
                        if len(lines) >= 2:
                            port = int(lines[0])
                            endpoint = lines[1]
                            if not 1 <= port <= 65535 or not endpoint.startswith(
                                "/devtools/browser/"
                            ):
                                raise BrowserUnavailable(
                                    "Браузер вернул некорректный адрес управления."
                                )
                            print(
                                json.dumps(
                                    {"websocket": f"ws://127.0.0.1:{port}{endpoint}"}
                                ),
                                flush=True,
                            )
                            break
                    if time.monotonic() > deadline:
                        raise BrowserUnavailable("Истёк таймаут запуска браузера.")
                while not stop.wait(0.1):
                    if process.poll() is not None:
                        break
            finally:
                _kill_tree(process, job)
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": (
                        str(error)
                        if isinstance(error, BrowserUnavailable)
                        else "Could not start the local browser."
                    )
                },
                ensure_ascii=True,
            ),
            flush=True,
        )


if __name__ == "__main__":
    worker()
