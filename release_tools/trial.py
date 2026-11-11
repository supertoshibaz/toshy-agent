"""Stdlib-only ownership of a trial GUI and its detached agent core."""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time
from . import layout


def identity(pid):
    if os.name != 'nt':
        try:
            # Names may contain spaces and parentheses; fields follow the last ).
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            return None if fields[0] == 'Z' else fields[19]
        except (OSError, IndexError):
            return None
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    api.OpenProcess.restype = w.HANDLE
    api.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
    api.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [w.FILETIME() for _ in range(4)]
        code = w.DWORD()
        if (not api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259
                or not api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        api.CloseHandle(handle)


def register():
    directory = os.environ.get('TOSHY_UPDATE_TRIAL_DIR')
    token = os.environ.get('TOSHY_UPDATE_READY_TOKEN')
    if directory and token:
        pid = os.getpid()
        layout.write_json(Path(directory) / (str(pid) + '.json'),
                          {'pid': pid, 'birth': identity(pid), 'token': token})


class WindowsJob:
    """Children inherit the job even when the core starts detached.

    Closing this handle after a successful startup leaves the application alive.
    Only an explicit kill during failed activation terminates the owned tree.
    """
    def __init__(self, pid):
        from ctypes import wintypes as w
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.handle = self.api.CreateJobObjectW(None, None)
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            if not self.handle or not process or not self.api.AssignProcessToJobObject(self.handle, process):
                raise OSError(ctypes.get_last_error(), 'Could not supervise the trial application process tree.')
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                self.api.CloseHandle(process)

    def kill(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def stop(process, *, directory=None, token=None, job=None):
    """Never terminate a stale/reused PID from a previous application run."""
    owned = {}
    recorded = {}
    if directory:
        for path in Path(directory).glob('*.json'):
            try:
                record = layout.read_json(path)
                pid = int(record['pid'])
                if record.get('token') == token and record.get('birth'):
                    recorded[pid] = record['birth']
                    if identity(pid) == record['birth']:
                        owned[pid] = record['birth']
            except (OSError, ValueError, KeyError, TypeError):
                continue
    if job is not None:
        job.kill()
    if os.name != 'nt':
        groups = {process.pid}
        for pid in owned:
            try:
                if os.getpgid(pid) == pid:
                    groups.add(pid)
                else:
                    os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for group in groups:
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
    elif process.poll() is None:
        process.terminate()
    if os.name == 'nt' and job is None:
        # Fallback if assigning the GUI to a job failed before startup.
        from ctypes import wintypes as w
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; api.OpenProcess.restype = w.HANDLE
        api.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
        api.TerminateProcess.argtypes = [w.HANDLE, w.UINT]; api.CloseHandle.argtypes = [w.HANDLE]
        for pid, birth in owned.items():
            handle = api.OpenProcess(0x1000 | 0x0001, False, pid)
            if handle:
                try:
                    times = [w.FILETIME() for _ in range(4)]
                    if api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                        actual = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
                        if actual == birth:
                            api.TerminateProcess(handle, 1)
                finally:
                    api.CloseHandle(handle)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=5)
    deadline = time.monotonic() + 3
    while owned and time.monotonic() < deadline:
        owned = {pid: birth for pid, birth in owned.items() if identity(pid) == birth}
        if owned:
            time.sleep(0.1)
    if os.name != 'nt':
        for pid, birth in owned.items():
            if identity(pid) == birth:
                try:
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    if directory:
        for pid in recorded:
            if identity(pid) is None:
                # Registered detached cores may remain zombies until their OS
                # parent reaps them. They no longer block offline recovery.
                path = Path(directory).parent.parent / 'backup_processes' / (str(pid) + '.pid')
                try:
                    if path.read_text().strip() == str(pid):
                        path.unlink()
                except FileNotFoundError:
                    pass
