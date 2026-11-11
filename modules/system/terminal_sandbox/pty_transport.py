"""Async POSIX PTY and native Windows ConPTY, without optional packages."""
import asyncio
import errno
import os
import signal
import struct
import subprocess
import sys
from pathlib import Path


class PosixPty:
    @classmethod
    async def spawn(cls, args, cwd, env, columns, rows):
        import fcntl
        import termios
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
            env = dict(env, TERM='xterm-256color', COLORTERM='truecolor')
            child = str(Path(__file__).with_name('pty_child.py'))
            process = await asyncio.create_subprocess_exec(sys.executable, child, *args,
                stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        result = cls()
        result.process, result.fd = process, master
        result.pid = process.pid
        result.stdout = result.stdin = result
        result.closed = False
        result._writes = bytearray()
        os.set_blocking(master, False)
        return result

    @property
    def returncode(self):
        return self.process.returncode

    async def read(self, size):
        loop = asyncio.get_running_loop()
        while not self.closed:
            try:
                return os.read(self.fd, size)
            except BlockingIOError:
                ready = loop.create_future()
                loop.add_reader(self.fd, lambda: not ready.done() and ready.set_result(None))
                try:
                    await ready
                finally:
                    loop.remove_reader(self.fd)
            except OSError as error:
                if error.errno in {errno.EIO, errno.EBADF}:
                    return b''
                raise
        return b''

    def write(self, data):
        if self.closed or self.returncode is not None:
            raise ValueError('Процесс уже завершён.')
        self._writes.extend(data)

    async def drain(self):
        loop = asyncio.get_running_loop()
        while self._writes:
            try:
                count = os.write(self.fd, self._writes)
                del self._writes[:count]
            except BlockingIOError:
                ready = loop.create_future()
                loop.add_writer(self.fd, lambda: not ready.done() and ready.set_result(None))
                try:
                    await ready
                finally:
                    loop.remove_writer(self.fd)

    def resize(self, columns, rows):
        import fcntl
        import termios
        if not self.closed:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))

    async def wait(self):
        return await self.process.wait()

    def kill(self):
        # Interactive shells may give every child its own process group.
        # Kill groups belonging to our PTY session, never the application's group.
        groups={self.pid}
        try:groups.add(os.tcgetpgrp(self.fd))
        except OSError:pass
        for path in Path('/proc').glob('[0-9]*/stat'):
            try:
                fields=path.read_text().rsplit(')',1)[1].split()
                if int(fields[3])==self.pid:groups.add(int(fields[2]))
            except (OSError,ValueError,IndexError):pass
        for group in groups:
            if group<=1 or group==os.getpgrp():continue
            try:os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:pass
        if self.returncode is None:
            try:self.process.kill()
            except ProcessLookupError:pass

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.fd)


class WindowsPty:
    @classmethod
    async def spawn(cls, args, cwd, env, columns, rows):
        return await asyncio.to_thread(cls, args, cwd, env, columns, rows)

    def __init__(self, args, cwd, env, columns, rows):
        import ctypes as c
        from ctypes import wintypes as w
        import msvcrt
        from .host_process import WindowsJob
        class COORD(c.Structure):
            _fields_ = [('X', c.c_short), ('Y', c.c_short)]
        class STARTUP(c.Structure):
            _fields_ = [('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),('title',w.LPWSTR),
                ('x',w.DWORD),('y',w.DWORD),('width',w.DWORD),('height',w.DWORD),('charsX',w.DWORD),('charsY',w.DWORD),
                ('fill',w.DWORD),('flags',w.DWORD),('show',w.WORD),('reservedBytes',w.WORD),('reservedPtr',c.c_void_p),
                ('stdin',w.HANDLE),('stdout',w.HANDLE),('stderr',w.HANDLE)]
        class STARTUPEX(c.Structure):
            _fields_ = [('StartupInfo',STARTUP),('attributes',c.c_void_p)]
        class PROCESS(c.Structure):
            _fields_ = [('process',w.HANDLE),('thread',w.HANDLE),('pid',w.DWORD),('tid',w.DWORD)]
        api = self.api = c.WinDLL('kernel32',use_last_error=True)
        api.CreatePipe.argtypes = [c.POINTER(w.HANDLE),c.POINTER(w.HANDLE),c.c_void_p,w.DWORD]
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CreatePseudoConsole.argtypes = [COORD,w.HANDLE,w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]
        api.CreatePseudoConsole.restype = c.c_long
        api.ClosePseudoConsole.argtypes = [w.HANDLE]
        api.ResizePseudoConsole.argtypes = [w.HANDLE,COORD]
        api.ResizePseudoConsole.restype = c.c_long
        api.InitializeProcThreadAttributeList.argtypes = [c.c_void_p,w.DWORD,w.DWORD,c.POINTER(c.c_size_t)]
        api.UpdateProcThreadAttribute.argtypes = [c.c_void_p,w.DWORD,c.c_size_t,c.c_void_p,c.c_size_t,c.c_void_p,c.c_void_p]
        api.DeleteProcThreadAttributeList.argtypes = [c.c_void_p]
        api.CreateProcessW.argtypes = [w.LPCWSTR,w.LPWSTR,c.c_void_p,c.c_void_p,w.BOOL,w.DWORD,c.c_void_p,w.LPCWSTR,c.c_void_p,c.POINTER(PROCESS)]
        api.WaitForSingleObject.argtypes = [w.HANDLE,w.DWORD]
        api.GetExitCodeProcess.argtypes = [w.HANDLE,c.POINTER(w.DWORD)]
        api.ResumeThread.argtypes = [w.HANDLE]
        api.TerminateProcess.argtypes = [w.HANDLE,w.UINT]
        self.coord = COORD
        self.pc,self.process,self.owner = w.HANDLE(),None,None
        self.input_fd,self.output_fd = None,None
        self.returncode,self.closed = None,False
        self._waiting=None
        self.stdout = self.stdin = self
        self._writes = bytearray()
        read_in,write_in,read_out,write_out = (w.HANDLE() for _ in range(4))
        attrs,initialized,info = None,False,PROCESS()
        try:
            if not api.CreatePipe(c.byref(read_in),c.byref(write_in),None,0) or not api.CreatePipe(c.byref(read_out),c.byref(write_out),None,0):
                raise c.WinError(c.get_last_error())
            if api.CreatePseudoConsole(COORD(columns,rows),read_in,write_out,0,c.byref(self.pc)) < 0:
                raise OSError('Не удалось создать ConPTY. Требуется Windows 10 1809 или новее.')
            size=c.c_size_t()
            api.InitializeProcThreadAttributeList(None,1,0,c.byref(size))
            attrs=c.create_string_buffer(size.value)
            if not api.InitializeProcThreadAttributeList(attrs,1,0,c.byref(size)):
                raise c.WinError(c.get_last_error())
            initialized=True
            if not api.UpdateProcThreadAttribute(attrs,0,0x00020016,self.pc,c.sizeof(w.HANDLE),None,None):
                raise c.WinError(c.get_last_error())
            startup=STARTUPEX();startup.StartupInfo.cb=c.sizeof(startup);startup.attributes=c.cast(attrs,c.c_void_p)
            environment=c.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items(),key=lambda p:p[0].upper()))+'\0\0')
            command=c.create_unicode_buffer(subprocess.list2cmdline(args))
            if not api.CreateProcessW(None,command,None,None,False,0x00080000|0x00000400|0x00000004,
                                      environment,cwd,c.byref(startup),c.byref(info)):
                raise c.WinError(c.get_last_error())
            self.process,self.pid=info.process,info.pid
            self.owner=WindowsJob(self.pid)
            self.input_fd=msvcrt.open_osfhandle(write_in.value,os.O_WRONLY|os.O_BINARY);write_in=w.HANDLE()
            self.output_fd=msvcrt.open_osfhandle(read_out.value,os.O_RDONLY|os.O_BINARY);read_out=w.HANDLE()
            if api.ResumeThread(info.thread)==0xFFFFFFFF:
                raise c.WinError(c.get_last_error())
        except BaseException:
            if info.process:
                api.TerminateProcess(info.process,1)
            self.close()
            raise
        finally:
            if initialized:
                api.DeleteProcThreadAttributeList(attrs)
            for handle in (read_in,write_in,read_out,write_out,info.thread):
                if handle:
                    api.CloseHandle(handle)

    async def read(self, size):
        try:
            return await asyncio.to_thread(os.read,self.output_fd,size)
        except OSError:
            return b''

    def write(self, data):
        if self.closed or self.returncode is not None:
            raise ValueError('Процесс уже завершён.')
        self._writes.extend(data)

    async def drain(self):
        while self._writes:
            count=await asyncio.to_thread(os.write,self.input_fd,bytes(self._writes))
            del self._writes[:count]

    def resize(self, columns, rows):
        if not self.closed and self.api.ResizePseudoConsole(self.pc,self.coord(columns,rows)) < 0:
            raise OSError('Не удалось изменить размер ConPTY.')

    async def wait(self):
        if self._waiting is None:
            self._waiting=asyncio.create_task(self._wait())
        return await asyncio.shield(self._waiting)

    async def _wait(self):
        import ctypes as c
        from ctypes import wintypes as w
        if self.returncode is None:
            await asyncio.to_thread(self.api.WaitForSingleObject,self.process,0xFFFFFFFF)
            code=w.DWORD();self.api.GetExitCodeProcess(self.process,c.byref(code));self.returncode=code.value
            if self.pc:
                console,self.pc=self.pc,None
                await asyncio.to_thread(self.api.ClosePseudoConsole,console)
        return self.returncode

    def kill(self):
        if self.owner:
            self.owner.kill()

    def close(self):
        if self.closed:
            return
        self.closed=True
        if self.owner:
            self.owner.close();self.owner=None
        if self.pc:
            self.api.ClosePseudoConsole(self.pc);self.pc=None
        for fd in (self.input_fd,self.output_fd):
            if fd is not None:
                os.close(fd)
        if self.process:
            self.api.CloseHandle(self.process);self.process=None


async def spawn(args, *, cwd=None, env=None, columns=100, rows=30):
    cls = WindowsPty if os.name == 'nt' else PosixPty
    return await cls.spawn(args,cwd,env or dict(os.environ),columns,rows)
