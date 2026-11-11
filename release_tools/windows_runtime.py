"""Required Windows runtime checks and readable native process failures."""
import os
from pathlib import Path

PACKAGE = 'Microsoft.VCRedist.2015+.x64'
DLLS = ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll')


def missing():
    if os.name != 'nt':
        return []
    directory = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    return [name for name in DLLS if not (directory / name).is_file()]


def installed():
    return not missing()


def install(engine):
    from .windows_packages import setup
    from .network import ReleaseError
    engine.progress('Required: Microsoft Visual C++ v14 x64 Runtime. Missing: ' + ', '.join(missing()))
    target = engine.root / 'cache/windows-installers/vc_redist.x64.exe'
    engine.network.download('https://aka.ms/vc14/vc_redist.x64.exe', target, limit=128 * 1024**2)
    # The official Microsoft signature is verified by setup() before execution.
    setup(engine, PACKAGE, target, '/install /passive /norestart')
    if missing():
        raise ReleaseError('Visual C++ x64 Runtime is still unavailable: ' + ', '.join(missing()) +
                           '. Complete the Microsoft setup, restart Windows if requested, and rerun install.ps1.')
    engine.progress('Microsoft Visual C++ x64 Runtime ready.')


def explain_exit(executable, code):
    status = code & 0xffffffff
    descriptions = {
        0xc0000135: 'Windows could not load a required DLL.',
        0xc000007b: 'A required executable or DLL has an invalid format or wrong architecture.',
        0xc0000139: 'A required DLL does not provide the expected entry point; update its runtime.',
        0xc0000142: 'Windows could not initialize a required DLL.',
        0xc0000005: 'The process crashed with an access violation.',
        0xc000001d: 'The process used an instruction unsupported by this CPU or virtual machine.',
    }
    if status not in descriptions:
        return None
    detail = Path(executable).name + f' failed (0x{status:08X}): ' + descriptions[status]
    if status == 0xc0000135:
        absent = missing()
        detail += (' Missing system Visual C++ Runtime files: ' + ', '.join(absent) +
                   '. Rerun install.ps1 to install Microsoft Visual C++ x64 Runtime.' if absent else
                   ' The exit code does not identify the DLL. Launch this executable from Explorer to see its name.')
    return detail
