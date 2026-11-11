"""Native known folders and matching shortcut/taskbar application identity."""
import ctypes
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path
import uuid


def app_id(root):
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(str(root))).encode()).hexdigest()[:16]
    return 'org.toshy.AgentStudio.' + digest


def _guid(text):
    return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)


def known_folder(name):
    ids = {'DesktopDirectory': 'B4BFCC3A-DB2C-424C-B029-7FE99A87C641',
           'Programs': 'A77F5D77-2E2B-44C3-A6A2-ABA601054A51'}
    api = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32')
    api.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, w.DWORD, w.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    api.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    result = ctypes.c_void_p()
    status = api.SHGetKnownFolderPath(ctypes.byref(_guid(ids[name])), 0x8000, None, ctypes.byref(result))
    if status < 0:
        raise OSError(f'Windows could not resolve {name}: HRESULT 0x{status & 0xffffffff:08X}')
    try:
        path = Path(ctypes.wstring_at(result))
        if not path.is_absolute():
            raise OSError('Windows returned a non-absolute known-folder path.')
        return path
    finally:
        ole.CoTaskMemFree(result)


def set_shortcut_app_id(path, identity):
    class PropertyKey(ctypes.Structure):
        _fields_ = [('guid', ctypes.c_ubyte * 16), ('pid', w.DWORD)]

    class ValueData(ctypes.Union):
        _fields_ = [('text', w.LPWSTR), ('padding', ctypes.c_ubyte * 16)]

    class PropVariant(ctypes.Structure):
        _fields_ = [('vt', w.WORD), ('r1', w.WORD), ('r2', w.WORD), ('r3', w.WORD), ('data', ValueData)]

    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)
    if initialized < 0 and (initialized & 0xffffffff) != 0x80010106:
        raise OSError(f'COM initialization failed: 0x{initialized & 0xffffffff:08X}')
    store = ctypes.c_void_p()
    shell = ctypes.WinDLL('shell32')
    shell.SHGetPropertyStoreFromParsingName.argtypes = [w.LPCWSTR, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetPropertyStoreFromParsingName.restype = ctypes.c_long
    try:
        status = shell.SHGetPropertyStoreFromParsingName(str(path), None, 2,
            ctypes.byref(_guid('886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99')), ctypes.byref(store))
        if status < 0:
            raise OSError(f'Could not open shortcut properties: 0x{status & 0xffffffff:08X}')
        table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))(table[6])
        commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(table[7])
        key = PropertyKey(_guid('9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3'), 5)
        value = PropVariant(); value.vt = 31; value.data.text = identity
        for status in (set_value(store, ctypes.byref(key), ctypes.byref(value)), commit(store)):
            if status < 0:
                raise OSError(f'Could not save shortcut application identity: 0x{status & 0xffffffff:08X}')
    finally:
        if store:
            table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(w.ULONG, ctypes.c_void_p)(table[2])(store)
        if initialized >= 0:
            ole.CoUninitialize()
