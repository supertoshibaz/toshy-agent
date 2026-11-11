"""Mandatory Docker isolation, independent of optional root-filesystem limits."""
import os
from pathlib import Path

from core.paths import APP_PATHS

SECURITY_REVISION=2

# Package managers change file ownership and drop downloads to a service user.
# These capabilities do not grant mount, device, kernel or network administration.
ROOT_CAPABILITIES=frozenset({'DAC_OVERRIDE','CHOWN','FOWNER','SETUID','SETGID'})


def sandbox_user():
    uid=os.getuid() if os.name!='nt' else 1000
    gid=os.getgid() if os.name!='nt' else 1000
    # Running Toshy as root must never make agent commands container-root.
    return f'{uid if uid>0 else 65532}:{gid if gid>0 else 65532}'


def container_user(profile):
    return '0:0' if profile.get('run_as_root',True) else sandbox_user()


def container_capabilities(profile):
    return ROOT_CAPABILITIES if profile and profile.get('run_as_root',True) else frozenset()


def validate_host_directory(value):
    path=Path(value).expanduser().resolve()
    text=str(path)
    if any(c in text for c in ('\x00','\n','\r',',')) or (os.name!='nt' and ':' in text):
        raise ValueError('Недопустимый путь подключаемой папки.')
    if path==Path(path.anchor) or path==Path.home() or Path.home().is_relative_to(path):
        raise ValueError('Корень диска и домашнюю папку целиком нельзя подключать к контейнеру.')
    protected=[APP_PATHS.config,APP_PATHS.runtime,APP_PATHS.sessions,
               Path(__file__).resolve().parent/'data']
    protected += [Path.home()/name for name in ('.ssh','.gnupg','.aws','.azure','.docker','.kube','.config')]
    if os.name=='nt':
        protected += [Path(os.environ[name]) for name in ('SystemRoot','ProgramFiles','ProgramFiles(x86)','ProgramData') if os.environ.get(name)]
    else:
        protected += [Path(name) for name in ('/etc','/proc','/sys','/dev','/run','/var/run','/boot','/usr','/bin','/sbin','/lib','/lib64','/var/lib/docker','/var/lib/containers')]
    for item in protected:
        item=Path(item).resolve()
        if path.is_relative_to(item) or item.is_relative_to(path):
            raise ValueError('Системные папки, настройки Toshy и хранилища ключей нельзя подключать к контейнеру.')
    return path


def validate_mount(mount,workspace=None):
    host=validate_host_directory(mount['host'])
    target=str(mount['container'])
    if any(c in target for c in (':',',','\x00','\n','\r')) or not target.startswith('/') or '..' in target.split('/'):
        raise ValueError('Недопустимый путь точки подключения контейнера.')
    target=target.rstrip('/') or '/'
    reserved=('/proc','/sys','/dev','/etc','/usr','/bin','/sbin','/lib','/lib64','/run','/var','/tmp','/home/toshy')
    if target=='/' or any(target==p or target.startswith(p+'/') or p.startswith(target+'/') for p in reserved):
        raise ValueError('Нельзя подключать папки поверх системных каталогов контейнера.')
    if workspace is not None and not host.is_relative_to(Path(workspace).resolve()):
        raise ValueError(f'Подключаемая папка «{host}» находится вне Project Folder текущего проекта «{Path(workspace).resolve()}». Удалите или исправьте эту запись в настройках терминала.')
    return {'host':str(host),'container':target,'readonly':bool(mount.get('readonly',False))}


def isolation_options(profile=None):
    # Never accept additional Docker arguments from a model or an image.
    options=['--cap-drop','ALL','--security-opt','no-new-privileges:true',
            '--ipc','private','--cgroupns','private','--restart','no',
            '--ulimit','core=0:0','--ulimit','nofile=4096:4096',
            '--log-driver','local','--log-opt','max-size=10m','--log-opt','max-file=2']
    for capability in sorted(container_capabilities(profile)):
        options.extend(['--cap-add',capability])
    return options


def validate_container(info,profile,user,home_volume):
    host=info.get('HostConfig') or {}
    config=info.get('Config') or {}
    options=[str(v).lower() for v in host.get('SecurityOpt') or []]
    capabilities={str(v).upper().removeprefix('CAP_') for v in host.get('CapAdd') or []}
    insecure=(host.get('Privileged') or host.get('Devices') or host.get('DeviceRequests') or
              host.get('DeviceCgroupRules') or capabilities!=container_capabilities(profile) or
              'ALL' not in {str(v).upper() for v in host.get('CapDrop') or []} or
              not any(v in {'no-new-privileges','no-new-privileges:true','no-new-privileges=true'} for v in options) or
              any('unconfined' in v or v.startswith('label=disable') or v.startswith('label:disable') for v in options) or
              host.get('PidMode') not in (None,'','private') or host.get('IpcMode')!='private' or
              host.get('CgroupnsMode')!='private' or host.get('UsernsMode')=='host' or
              host.get('NetworkMode') not in ({'bridge','default'} if profile['network'] else {'none'}) or
              config.get('User')!=user or bool(host.get('ReadonlyRootfs'))!=bool(profile.get('restrict_writes')) or
              host.get('PortBindings') or host.get('PublishAllPorts'))
    allowed={(str(Path(m['host']).resolve()),m['container'],not m['readonly']) for m in profile['mounts']}
    actual=set()
    for mount in info.get('Mounts') or []:
        if mount.get('Type')=='bind':actual.add((str(Path(mount['Source']).resolve()),mount['Destination'],bool(mount.get('RW'))))
        elif mount.get('Type')=='volume' and (mount.get('Destination')!='/home/toshy' or mount.get('Name')!=home_volume):insecure=True
    if insecure or actual!=allowed:
        raise ValueError('Небезопасные параметры существующего контейнера. Остановите и пересоздайте его в настройках терминала.')
