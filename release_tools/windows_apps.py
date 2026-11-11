"""Per-user Windows Installed Apps registration for an owned Toshy folder."""
from datetime import datetime
import hashlib
import os
from pathlib import Path
import subprocess


def _identity(root):
    location = os.path.normcase(os.path.abspath(str(root)))
    digest = hashlib.sha256(location.encode('utf-8')).hexdigest()[:24]
    return location, r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Toshy.AgentStudio.' + digest


def _check_owner(winreg, key, location):
    try:
        product = winreg.QueryValueEx(key, 'ToshyProduct')[0]
        root = winreg.QueryValueEx(key, 'ToshyRoot')[0]
    except FileNotFoundError as error:
        raise RuntimeError('Preserved an unrelated Windows application registry entry.') from error
    if product != 'Toshy' or os.path.normcase(os.path.abspath(str(root))) != location:
        raise RuntimeError('Preserved a Windows application registry entry owned by another installation.')


def register(root, state, python, uninstaller, icon):
    import winreg
    location, path = _identity(root)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            _check_owner(winreg, key, location)
    except FileNotFoundError:
        pass
    values = {
        'ToshyProduct': 'Toshy', 'ToshyRoot': location,
        'DisplayName': 'Toshy', 'Publisher': 'supertoshibaz',
        'DisplayVersion': str(state['current']['version']),
        'InstallLocation': str(Path(root).absolute()),
        'DisplayIcon': str(icon) + ',0',
        'UninstallString': subprocess.list2cmdline([str(python), str(uninstaller)]),
        'InstallDate': datetime.now().strftime('%Y%m%d'),
    }
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name in ('NoModify', 'NoRepair'):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)
    state['windows_app_key'] = path
    return path


def remove(root):
    import winreg
    location, path = _identity(root)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            _check_owner(winreg, key, location)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass
