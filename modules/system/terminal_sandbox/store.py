"""Module-owned container definitions; reading configuration never starts Docker."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import posixpath
import re
import threading
import uuid
from pathlib import Path

from core.paths import APP_PATHS

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGE = 'agent-sandbox:latest'


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def container_path(value):
    value = str(value or '').strip()
    if not value.startswith('/') or '\x00' in value or '\n' in value or '..' in value.split('/'):
        raise ValueError('Путь внутри контейнера должен быть абсолютным Linux-путём без «..».')
    return posixpath.normpath(value)


class ContainerStore:
    def __init__(self, root=None, workspace=None):
        self.root = Path(root or APP_PATHS.modules / 'system' / 'terminal_sandbox' / 'data')
        self.workspace = Path(workspace or APP_PATHS.workspace).resolve()
        self.path = self.root / 'containers.json'
        self.lock = threading.RLock()
        self.owner = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]

    def default(self, identity='shared'):
        return {'id': identity, 'name': 'This computer' if identity == 'local' else ('Shared terminal' if identity == 'shared' else 'New terminal'),
                'backend': 'local' if identity == 'local' else 'docker', 'save_history': True,
                'approval_mode': 'smart', 'approval_model': '', 'deny_rules': [], 'allow_commands': [],
                'shell': '', 'ssh_host': '', 'ssh_port': 22, 'ssh_user': '', 'ssh_key': '',
                'target_os': 'linux', 'ssh_root': '',
                'active': False, 'image': DEFAULT_IMAGE, 'network': False, 'cpus': 2.0,
                'memory_mb': 2048, 'pids': 256, 'timeout': 60, 'preserve': True,
                'run_as_root': True, 'restrict_writes': False, 'environment': {}, 'bindings': [],
                'mounts': []}

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {'version': 1, 'profiles': [self.default()]}
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or not isinstance(data.get('profiles'), list):
                raise ValueError('Неподдерживаемый формат настроек терминала.')
            return data

    def profiles(self):
        profiles = [dict(self.default(p['id']), **copy.deepcopy(p)) for p in self.read()['profiles']]
        for profile in profiles:
            legacy = {'shared': ('Общий терминал', 'Shared terminal'),
                      'local': ('Этот компьютер', 'This computer')}.get(profile['id'])
            if legacy and profile.get('name') == legacy[0]:
                profile['name'] = legacy[1]
        if not any(p['id'] == 'local' for p in profiles):
            profiles.append(self.default('local'))
        return profiles

    def get(self, identity):
        for profile in self.profiles():
            if profile['id'] == identity:
                return profile
        raise ValueError('Контейнер не найден.')

    def normalize(self, value):
        identity = str(value.get('id') or uuid.uuid4().hex)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', identity):
            raise ValueError('Некорректный ID контейнера.')
        result = self.default(identity)
        result.update({key: value[key] for key in result if key in value})
        if result['backend'] not in {'docker', 'local', 'ssh'} or (identity == 'local' and result['backend'] != 'local'):
            raise ValueError('Неизвестный тип терминала.')
        if result['approval_mode'] not in {'manual', 'smart', 'deny', 'off'}:
            raise ValueError('Неизвестный режим проверки команд.')
        if result['target_os'] not in {'linux', 'windows'}:
            raise ValueError('Выберите Linux или Windows для SSH.')
        for key in ('deny_rules', 'allow_commands'):
            if not isinstance(result[key], list) or any(not isinstance(v, str) or len(v) > 131072 for v in result[key]):
                raise ValueError('Правила команд должны быть списком строк.')
        for key in ('shell', 'ssh_host', 'ssh_user', 'ssh_key', 'ssh_root', 'approval_model'):
            result[key] = str(result[key]).strip()
            if any(c in result[key] for c in '\x00\n\r'):
                raise ValueError('Настройки подключения должны занимать одну строку.')
        result['ssh_port'] = int(result['ssh_port'])
        if not 1 <= result['ssh_port'] <= 65535:
            raise ValueError('Порт SSH: от 1 до 65535.')
        if result['backend'] == 'ssh' and (not result['ssh_host'] or not result['ssh_user']):
            raise ValueError('Укажите адрес SSH и пользователя.')
        result['name'] = str(result['name']).strip()[:120]
        result['image'] = str(result['image']).strip()
        if not result['name'] or not result['image'] or result['image'].startswith('-') or any(c.isspace() for c in result['image']):
            raise ValueError('Укажите имя и корректное имя Docker-образа.')
        for key in ('active', 'network', 'preserve', 'run_as_root', 'restrict_writes', 'save_history'):
            if not isinstance(result[key], bool):
                raise ValueError(f'{key}: требуется логическое значение.')
        result['cpus'] = float(result['cpus'])
        if not math.isfinite(result['cpus']) or not 0.1 <= result['cpus'] <= 256:
            raise ValueError('CPU: допустимо от 0.1 до 256.')
        for key, minimum, maximum in [('memory_mb', 64, 1048576), ('pids', 16, 65536), ('timeout', 1, 86400)]:
            result[key] = int(result[key])
            if not minimum <= result[key] <= maximum:
                raise ValueError(f'{key}: допустимо от {minimum} до {maximum}.')
        environment = result['environment']
        if not isinstance(environment, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) for k in environment):
            raise ValueError('Переменные окружения задаются как NAME=value.')
        result['environment'] = {key: str(item) for key, item in environment.items()}
        if any('\x00' in item for item in result['environment'].values()):
            raise ValueError('Переменная окружения содержит нулевой символ.')
        mounts, destinations = [], set()
        for mount in result['mounts']:
            host = str(Path(mount['host']).expanduser().resolve())
            target = container_path(mount['container'])
            if target in destinations or target == '/' or ',' in host or ',' in target:
                raise ValueError('Точки подключения должны быть уникальны, без запятых и не в корень контейнера.')
            if target == '/home/toshy' or target.startswith('/home/toshy/') or target == '/tmp':
                raise ValueError('/home/toshy и /tmp зарезервированы терминалом.')
            destinations.add(target)
            mounts.append({'host': host, 'container': target, 'readonly': bool(mount.get('readonly', False))})
        if result['backend']=='docker':
            from .sandbox_security import validate_mount
            mounts=[validate_mount(mount) for mount in mounts]
        result['mounts'] = mounts
        bindings, seen = [], set()
        for binding in result['bindings']:
            kind, target = str(binding.get('kind', '')), str(binding.get('target', '')).strip()
            if kind not in {'project', 'folder', 'session'} or not target:
                raise ValueError('Привязка должна указывать проект, папку или сессию.')
            if (kind, target) not in seen:
                bindings.append({'kind': kind, 'target': target})
                seen.add((kind, target))
        if identity == 'shared' and bindings:
            raise ValueError('Общий терминал используется как запасной и не имеет привязок.')
        result['bindings'] = bindings
        return result

    def save(self, value, catalog=None):
        profile = self.normalize(value)
        with self.lock:
            data = self.read()
            if catalog is not None:
                from .bindings import subtract
                for other in data['profiles']:
                    if other['id'] != profile['id']:
                        other['bindings'] = subtract(other['bindings'],profile['bindings'],catalog)
            claimed = {(b['kind'], b['target']) for p in data['profiles'] if p['id'] != profile['id'] for b in p['bindings']}
            if any((b['kind'], b['target']) in claimed for b in profile['bindings']):
                raise ValueError('На эту область уже назначен другой контейнер, в том числе неактивный.')
            data['profiles'] = [p for p in data['profiles'] if p['id'] != profile['id']] + [profile]
            write_json(self.path, data)
        return profile

    def delete(self, identity):
        if identity in {'shared', 'local'}:
            raise ValueError('Встроенный терминал можно отключить, но нельзя удалить.')
        with self.lock:
            data = self.read()
            data['profiles'] = [p for p in data['profiles'] if p['id'] != identity]
            write_json(self.path, data)

    def resolve(self, session_id='', folder_chain=(), project_id=''):
        profiles = self.profiles()
        # Include inactive assignments: they deny access rather than changing the route.
        candidates = [('session', str(session_id))] if session_id else []
        candidates += [('folder', str(item['id'])) for item in reversed(folder_chain)]
        if project_id:
            candidates.append(('project', str(project_id)))
        for kind, target in candidates:
            for profile in profiles:
                if {'kind': kind, 'target': target} in profile['bindings']:
                    return profile
        return next(p for p in profiles if p['id'] == 'shared')

    def rename_session(self, old, new):
        with self.lock:
            data = self.read()
            for profile in data['profiles']:
                for binding in profile['bindings']:
                    if binding == {'kind': 'session', 'target': old}:
                        binding['target'] = new
            write_json(self.path, data)
