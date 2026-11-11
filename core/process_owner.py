"""Bounded disposable workers; own descendants even after the direct child exits."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys


async def run_json_worker(script,payload,*,cwd=None,timeout=60,max_output=16*1024*1024,isolated=True):
    encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(encoded)>32*1024*1024:raise ValueError('Worker input exceeds 32 MiB.')
    args=[sys.executable,*(['-I'] if isolated else []),str(Path(script).resolve())]
    process=await asyncio.create_subprocess_exec(*args,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
        cwd=cwd,start_new_session=os.name=='posix')
    owner=None;tasks=[]
    def stop_tree():
        if os.name=='posix':
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        elif owner is not None:owner.kill()
        elif process.returncode is None:process.kill()
    async def read(stream,limit):
        chunks=[];size=0
        while chunk:=await stream.read(65536):
            size+=len(chunk)
            if size>limit:raise ValueError('Worker output exceeds its size limit.')
            chunks.append(chunk)
        return b''.join(chunks)
    async def send():
        try:
            process.stdin.write(encoded);await process.stdin.drain()
        except (BrokenPipeError,ConnectionResetError):pass
        finally:process.stdin.close()
    async def finish():
        while process.returncode is None:await asyncio.sleep(.01)
        stop_tree()  # A successful parent may have left a child holding its pipes.
        await process.wait()
    try:
        if os.name=='nt':owner=WindowsJob(process.pid)
        tasks=[asyncio.create_task(send()),asyncio.create_task(read(process.stdout,max_output)),
               asyncio.create_task(read(process.stderr,128000)),asyncio.create_task(finish())]
        values=await asyncio.wait_for(asyncio.gather(*tasks),max(1,min(float(timeout),600)))
        return process.returncode,values[1],values[2]
    finally:
        stop_tree()
        try:
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await asyncio.wait_for(process.wait(),3)
        finally:
            if owner is not None:owner.close()


import ctypes


class WindowsJob:
    def __init__(self, pid):
        from ctypes import wintypes as w
        class BASIC(ctypes.Structure):
            _fields_ = [('ProcessTime', ctypes.c_int64), ('JobTime', ctypes.c_int64), ('Flags', w.DWORD),
                        ('Min', ctypes.c_size_t), ('Max', ctypes.c_size_t), ('Limit', w.DWORD),
                        ('Affinity', ctypes.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
        class EXTENDED(ctypes.Structure):
            _fields_ = [('Basic', BASIC), ('Io', IO), ('ProcessMemory', ctypes.c_size_t), ('JobMemory', ctypes.c_size_t),
                        ('PeakProcess', ctypes.c_size_t), ('PeakJob', ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.handle = self.api.CreateJobObjectW(None, None)
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
        info = EXTENDED(); info.Basic.Flags = 0x2000  # KILL_ON_JOB_CLOSE
        try:
            if not self.handle or not process or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not self.api.AssignProcessToJobObject(self.handle, process):
                raise OSError(ctypes.get_last_error(), 'Не удалось создать группу процессов Windows')
        except BaseException:
            self.close(); raise
        finally:
            if process:
                self.api.CloseHandle(process)

    def kill(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle); self.handle = None

