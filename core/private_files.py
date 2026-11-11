"""Private temporary files and cross-process locks on Windows and POSIX."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import uuid


def _windows_file(path, disposition):
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    kernel=c.WinDLL('kernel32',use_last_error=True)
    advapi=c.WinDLL('advapi32',use_last_error=True)
    class Attributes(c.Structure):
        _fields_=[('length',w.DWORD),('descriptor',c.c_void_p),('inherit',w.BOOL)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[w.LPCWSTR,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=w.BOOL
    descriptor=c.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;;FA;;;OW)(A;;FA;;;SY)',1,c.byref(descriptor),None):raise c.WinError(c.get_last_error())
    kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,c.POINTER(Attributes),w.DWORD,w.DWORD,w.HANDLE]
    kernel.CreateFileW.restype=w.HANDLE
    kernel.LocalFree.argtypes=[c.c_void_p]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    try:
        attributes=Attributes(c.sizeof(Attributes),descriptor,False)
        handle=kernel.CreateFileW(str(Path(path).absolute()),0xC0000000,3,c.byref(attributes),disposition,0x00200000,None)
        if handle==c.c_void_p(-1).value:raise c.WinError(c.get_last_error())
        try:return msvcrt.open_osfhandle(handle,os.O_RDWR|os.O_BINARY)
        except BaseException:kernel.CloseHandle(handle);raise
    finally:kernel.LocalFree(descriptor)


def create_private(path):
    path=Path(path)
    if os.name=='nt':return _windows_file(path,1)  # CREATE_NEW with a protected DACL.
    return os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)


def protect_private(path):
    path=Path(path)
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or getattr(info,'st_file_attributes',0)&0x400:
        raise PermissionError('Private storage must be a regular file without links.')
    if os.name!='nt':path.chmod(0o600);return
    _protect_windows(path)


def _protect_windows(path):
    import ctypes as c
    from ctypes import wintypes as w
    api=c.WinDLL('advapi32',use_last_error=True);kernel=c.WinDLL('kernel32',use_last_error=True)
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[w.LPCWSTR,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p]
    api.SetFileSecurityW.argtypes=[w.LPCWSTR,w.DWORD,c.c_void_p]
    descriptor=c.c_void_p()
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;;FA;;;OW)(A;;FA;;;SY)',1,c.byref(descriptor),None):raise c.WinError(c.get_last_error())
    try:
        if not api.SetFileSecurityW(str(path.absolute()),0x80000004,descriptor):raise c.WinError(c.get_last_error())
    finally:
        kernel.LocalFree.argtypes=[c.c_void_p];kernel.LocalFree(descriptor)


def private_directory(path):
    path=Path(path);path.mkdir(mode=0o700,parents=True,exist_ok=False)
    if os.name=='nt':_protect_windows(path)
    return path


def protect_directory(path):
    path=Path(path);info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:
        raise PermissionError('Private directories cannot be links or reparse points.')
    if os.name=='nt':_protect_windows(path)
    else:path.chmod(0o700)


def atomic_private(path,payload):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with os.fdopen(create_private(temporary),'wb') as stream:
            if stream.write(payload)!=len(payload):raise OSError('Incomplete private file write.')
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        if os.name!='nt':
            fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
    finally:temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    try:fd=create_private(path)
    except FileExistsError:
        fd=_windows_file(path,3) if os.name=='nt' else os.open(path,os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'r+b') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise PermissionError('Invalid lock file.')
        if os.name=='nt':
            import msvcrt
            if not info.st_size:stream.write(b'0');stream.flush()
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
        try:yield
        finally:
            if os.name=='nt':stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
