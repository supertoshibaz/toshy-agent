"""Project I/O using pinned directories and non-following file handles."""
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path


class ProjectFiles:
    def __init__(self,root):self.root=Path(root).resolve()
    def relative(self,path):
        path=Path(path);path=path if path.is_absolute() else self.root/path
        if '..' in path.parts or not path.is_relative_to(self.root):
            raise PermissionError('Security Error: путь выходит за пределы Project Folder.')
        return path.relative_to(self.root)

    @contextmanager
    def directory(self,path='.',*,create=False):
        relative=self.relative(path)
        if os.name=='nt':
            with self._windows_directories(relative.parts,create=create) as directory:yield directory
            return
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,'O_CLOEXEC',0)
        handles=[os.open(self.root,flags)]
        try:
            for part in relative.parts:
                if create:
                    try:os.mkdir(part,dir_fd=handles[-1])
                    except FileExistsError:pass
                handles.append(os.open(part,flags,dir_fd=handles[-1]))
            yield handles[-1]
        finally:
            for handle in reversed(handles):os.close(handle)

    @contextmanager
    def open(self,path,*,write=False,create=False):
        relative=self.relative(path)
        if not relative.name:raise IsADirectoryError(path)
        with self.directory(relative.parent,create=create) as directory:
            if os.name=='nt':
                handle=_windows_open(directory/relative.name,write=write,create=create)
                import msvcrt
                try:fd=msvcrt.open_osfhandle(handle,(os.O_RDWR if write else os.O_RDONLY)|os.O_BINARY)
                except BaseException:
                    _windows_api().CloseHandle(handle);raise
            else:
                flags=(os.O_RDWR if write else os.O_RDONLY)|os.O_NOFOLLOW|os.O_NONBLOCK|getattr(os,'O_CLOEXEC',0)
                if create:flags|=os.O_CREAT
                fd=os.open(relative.name,flags,0o600,dir_fd=directory)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):raise PermissionError('Security Error: only regular files are allowed.')
                if info.st_nlink != 1:raise PermissionError('Security Error: hard-linked files are not allowed.')
                stream=os.fdopen(fd,'r+b' if write else 'rb')
            except BaseException:
                os.close(fd);raise
            with stream:yield stream

    def read_bytes(self,path,limit=None):
        with self.open(path) as stream:return stream.read() if limit is None else stream.read(limit)
    def write_text(self,path,text):
        relative=self.relative(path)
        if not relative.name:raise IsADirectoryError(path)
        with self.directory(relative.parent,create=True) as directory:
            temporary='.toshy-write-'+uuid.uuid4().hex+'.tmp'
            def entry(name):return directory/name if os.name=='nt' else name
            def attributes(name):
                try:return os.stat(entry(name),follow_symlinks=False,**({} if os.name=='nt' else {'dir_fd':directory}))
                except FileNotFoundError:return None
            def validate(info):
                if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                        or getattr(info,'st_file_attributes',0)&0x400):
                    raise PermissionError('Security Error: only unlinked regular project files are writable.')
            previous=attributes(relative.name);validate(previous)
            from core.text_files import decode_text,TextFormat
            text_format=decode_text(self.read_bytes(relative))[1] if previous is not None else TextFormat()
            payload=text_format.encode(text)
            flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
            fd=os.open(entry(temporary),flags,0o600,**({} if os.name=='nt' else {'dir_fd':directory}))
            try:
                with os.fdopen(fd,'wb') as stream:
                    if previous is not None:os.chmod(entry(temporary),stat.S_IMODE(previous.st_mode),**({} if os.name=='nt' else {'dir_fd':directory}))
                    if stream.write(payload)!=len(payload):raise OSError('Incomplete project file write.')
                    stream.flush();os.fsync(stream.fileno())
                current=attributes(relative.name);validate(current)
                version=lambda info:None if info is None else (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
                if version(current)!=version(previous):raise OSError('Project file changed before commit; read it again.')
                if os.name=='nt':os.replace(directory/temporary,directory/relative.name)
                else:
                    os.replace(temporary,relative.name,src_dir_fd=directory,dst_dir_fd=directory)
                    try:os.fsync(directory)
                    except OSError:pass  # Some filesystems cannot fsync directory handles.
            finally:
                try:os.unlink(entry(temporary),**({} if os.name=='nt' else {'dir_fd':directory}))
                except FileNotFoundError:pass
    def entries(self,path='.'):
        result=[]
        with self.directory(path) as directory:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:info=entry.stat(follow_symlinks=False)
                    except FileNotFoundError:continue
                    if (entry.is_symlink() or getattr(info,'st_file_attributes',0)&0x400
                            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):continue
                    result.append((entry.name,stat.S_ISDIR(info.st_mode),info.st_size))
        return sorted(result,key=lambda row:row[0])

    @contextmanager
    def _windows_directories(self,parts,*,create):
        # Deny delete/write sharing while each ancestor is pinned. Reject
        # junctions/reparse points instead of following a replaceable path.
        handles=[];directory=self.root
        try:
            handles.append(_windows_open(directory,directory=True))
            for part in parts:
                directory=directory/part
                if create:
                    try:directory.mkdir()
                    except FileExistsError:pass
                handles.append(_windows_open(directory,directory=True))
            yield directory
        finally:
            for handle in reversed(handles):_windows_api().CloseHandle(handle)


def _windows_api():
    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE;api.CloseHandle.argtypes=[wintypes.HANDLE]
    api.GetFileInformationByHandleEx.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD]
    return api


def _windows_open(path,*,write=False,create=False,directory=False):
    import ctypes
    from ctypes import wintypes
    api=_windows_api();text=str(path)
    if not text.startswith('\\\\?\\'):
        text='\\\\?\\UNC\\'+text[2:] if text.startswith('\\\\') else '\\\\?\\'+text
    handle=api.CreateFileW(text,0x80 if directory else 0x80000000|(0x40000000 if write else 0),
                           1 if directory else 3,None,4 if create else 3,0x00200000|(0x02000000 if directory else 0),None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    class Attributes(ctypes.Structure):_fields_=[('attributes',wintypes.DWORD),('tag',wintypes.DWORD)]
    info=Attributes()
    try:
        if not api.GetFileInformationByHandleEx(handle,9,ctypes.byref(info),ctypes.sizeof(info)):raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes&0x400 or bool(info.attributes&0x10)!=bool(directory):raise PermissionError('Security Error: ссылки и точки повторного разбора недоступны.')
        return handle
    except BaseException:
        api.CloseHandle(handle);raise
