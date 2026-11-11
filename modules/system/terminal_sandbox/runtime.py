"""Lazy terminal environments and streamed, independently managed process jobs."""
from __future__ import annotations

import asyncio
import codecs
import copy
import hashlib
import json
import os
import posixpath
import shlex
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from core.context import get_execution_context
from core.paths import APP_PATHS
from core.log_session import session_log_path
from .store import ContainerStore, DEFAULT_IMAGE, MODULE_DIR, container_path, write_json
from .host_runtime import HostRuntime
from .policy import CommandPolicy
from .interactive import InteractiveRuntime, dimensions
from .terminal_screen import TerminalScreen
from .terminal_sessions import TerminalSessions
from .container_config import configuration_differences
from .sandbox_security import SECURITY_REVISION,container_user,validate_host_directory,validate_mount,isolation_options,validate_container

TERMINAL_STATES = {'completed', 'failed', 'cancelled', 'timed_out', 'interrupted'}


class TerminalManager(TerminalSessions, InteractiveRuntime, HostRuntime):
    def __init__(self, store=None, sessions=None, docker='docker', app_session=None):
        self.store = store or ContainerStore()
        if sessions is None:
            from core.session_manager import SessionManager
            sessions = SessionManager()
        self.sessions = sessions
        self.docker = docker
        self.app_session = app_session or str(session_log_path())
        self._follow_app_session = app_session is None
        self.locks, self.lifecycle_locks = {}, {}
        self.reservations, self.frozen = {}, {}
        self.resource_profiles, self.resource_locks = {}, {}
        self.jobs, self.tasks, self.processes = {}, {}, {}
        self.interactive_init()
        self.directories = {}
        self.ssh_connections, self.host_handles = {}, {}
        self.policy = CommandPolicy(self)
        self.revision = 0
        self._logged_jobs = set()
        self._event = asyncio.Event()
        self._ready = {}
        self.maintenance = set()
        self.image_operations = set()
        self.completion_tasks = set()
        self.completion_profiles = set()
        self.container_errors = {}
        self._last_probe = 0
        self._probing = False
        self._probe_task = None
        self._build_lock = asyncio.Lock()
        self._closed = False
        from .transcripts import TranscriptStore
        self.transcripts = TranscriptStore(self.store.root / 'jobs')
        self._load_jobs()
        self.terminal_sessions_init()

    def _load_jobs(self):
        directory = self.store.root / 'jobs'
        if not directory.exists():
            return
        for path in sorted(directory.glob('*.json'), key=lambda p: p.stat().st_mtime)[-300:]:
            try:
                job = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(job, dict) or job.get('id') != path.stem:
                    continue
                if job.get('hidden'):
                    path.unlink(missing_ok=True);path.with_suffix('.out').unlink(missing_ok=True)
                    continue
                try:
                    profile = self.store.get(job['profile_id'])
                except ValueError:
                    profile = None
                if profile and not profile['save_history'] and job.get('app_session') != self.app_session:
                    path.unlink(missing_ok=True)
                    path.with_suffix('.out').unlink(missing_ok=True)
                    path.with_suffix('.screen.json').unlink(missing_ok=True)
                    continue
                if job.get('status') not in TERMINAL_STATES:
                    job.update(status='interrupted', finished=time.time(), error='Ядро завершилось во время выполнения. Команда автоматически не повторяется.')
                    write_json(path, job)
                retained=self.transcripts.sizes.get(job['id'],0)
                if retained<job.get('bytes',0):job['output_truncated']=True
                job['bytes']=retained
                self.jobs[job['id']] = job
            except (OSError, ValueError, KeyError):
                continue

    def _save_job(self, job):
        if job.get('finished') and job.get('terminal_session_id') and hasattr(self,'terminal_sessions'):
            tab=self.terminal_sessions.get(job['terminal_session_id'])
            if tab and tab.get('job_id')==job['id']:
                invocation=self.terminal_commands.get(tab.get('command_id'))
                if invocation and invocation['status']=='running':invocation.update(status='interrupted',finished=time.time())
                if invocation:
                    from .file_history import finish_command
                    finish_command(self,invocation)
                tab.update(state='stopped',command_id=None)
                self._save_terminals()
        if job.get('finished') and job.get('tty') and job['id'] in self.screens:
            write_json(self.store.root/'jobs'/(job['id']+'.screen.json'), self.screens[job['id']].snapshot())
            self.screens.pop(job['id'],None)
        if job.get('finished'):
            self.input_locks.pop(job['id'],None)
            self.input_review_buffers.pop(job['id'],None)
            for source in ('agent','manual'):self.input_review_buffers.pop((job['id'],source),None)
            self.job_contexts.pop(job['id'],None)
        write_json(self.store.root / 'jobs' / (job['id'] + '.json'), job)
        if job.get('finished') and job['id'] not in self._logged_jobs:
            self._logged_jobs.add(job['id'])
            from core.logger import core_logger
            core_logger.event('tools', 'TERMINAL_FINISH', 'Command execution finished', details={
                'job_id':job['id'], 'terminal':job['profile_id'], 'status':job['status'], 'exit_code':job.get('exit_code')})
        if job.get('finished'):
            active={key for key,value in self.jobs.items() if value['status'] not in TERMINAL_STATES}
            for identity in self.transcripts.prune(active):
                self.jobs.pop(identity,None);self._logged_jobs.discard(identity)
                task=self.tasks.get(identity)
                if task is not None and task.done():self.tasks.pop(identity,None)
        self.changed()

    def changed(self):
        self.revision += 1
        self._event.set()
        self._event = asyncio.Event()

    def sync_app_session(self):
        current = str(session_log_path()) if self._follow_app_session else self.app_session
        if current == self.app_session:
            return
        self.app_session = current
        for identity, job in list(self.jobs.items()):
            try:
                profile = self.store.get(job['profile_id'])
            except ValueError:
                continue
            if not profile['save_history'] and job['status'] in TERMINAL_STATES and job.get('app_session') != current:
                self.jobs.pop(identity,None)
                for suffix in ('.json','.out','.screen.json'):
                    (self.store.root/'jobs'/(identity+suffix)).unlink(missing_ok=True)
        self.changed()

    async def wait_feed(self, revision, timeout=20):
        if revision is not None and int(revision) == self.revision and not self._closed:
            try:
                await asyncio.wait_for(self._event.wait(), min(20, max(0, float(timeout))))
            except asyncio.TimeoutError:
                pass

    async def save_profile(self, value):
        identity = value.get('id')
        if identity:
            if not self.reservations.get(identity):
                for tab in list(self.terminal_sessions.values()):
                    if tab['profile_id']==identity and tab['state']=='ready' and tab.get('job_id') in self.jobs:
                        await self.cancel(tab['job_id'])
            self.check_idle(identity)
        profile = self.store.save(value, self.project_catalog())
        connection = self.ssh_connections.pop(profile['id'], None)
        if connection:
            connection.close(); await connection.wait_closed()
        self._ready.pop(profile['id'], None)
        self.changed()
        return profile

    def _instances(self):
        path = self.store.root / 'instances.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def _set_instance(self, identity, value):
        data = self._instances()
        if value is None:
            data.pop(identity, None)
        else:
            data[identity] = value
        write_json(self.store.root / 'instances.json', data)

    def _name(self, identity):
        return f'toshy-{self.store.owner}-{identity.lower()}'

    async def cli(self, *args, timeout=30, check=True, output=None):
        try:
            proc = await asyncio.create_subprocess_exec(self.docker, *map(str, args),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError as error:
            raise RuntimeError('Docker не найден. Установите и запустите Docker (в Windows — Docker Desktop с Linux-контейнерами).') from error
        chunks, total = [], 0
        async def consume():
            nonlocal total
            while chunk := await proc.stdout.read(16384):
                if output:
                    output(chunk.decode('utf-8', 'replace'))
                chunks.append(chunk)
                total += len(chunk)
                while total > 262144 and len(chunks) > 1:
                    total -= len(chunks.pop(0))
            await proc.wait()
        try:
            await asyncio.wait_for(consume(), timeout)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        text = b''.join(chunks).decode('utf-8', 'replace')
        if check and proc.returncode:
            raise RuntimeError(text.strip()[-6000:] or f'Docker завершился с кодом {proc.returncode}.')
        return proc.returncode, text

    async def inspect(self, name):
        code, text = await self.cli('container', 'inspect', name, check=False)
        if code:
            # A missing container is normal. Daemon, permission and connection failures are not.
            if 'no such' in text.lower() or 'not found' in text.lower():
                return None
            raise RuntimeError(text.strip() or 'Docker недоступен.')
        values = json.loads(text)
        if not values:
            return None
        info = values[0]
        if (info.get('Config', {}).get('Labels') or {}).get('toshy.terminal.owner') != self.store.owner:
            raise RuntimeError('Имя занято контейнером, который не принадлежит этому модулю.')
        return info

    async def home_volume(self, identity, remove=False):
        name = self._name(identity) + '-home'
        code, text = await self.cli('volume', 'inspect', name, check=False)
        if code:
            if 'no such' not in text.lower() and 'not found' not in text.lower():
                raise RuntimeError(text)
            if not remove:
                await self.cli('volume', 'create', '--label', f'toshy.terminal.owner={self.store.owner}', name)
            return
        info=json.loads(text)[0]
        if ((info.get('Labels') or {}).get('toshy.terminal.owner') != self.store.owner
                or info.get('Driver')!='local' or info.get('Options')):
            raise ValueError('Имя домашнего тома занято чужим Docker-томом.')
        if remove:
            await self.cli('volume', 'rm', name)

    async def refresh_states(self):
        if self._probing or time.monotonic() - self._last_probe < 4:
            return
        self._last_probe, self._probing = time.monotonic(), True
        try:
            for identity, instance in self._instances().items():
                # A saved environment is not a live one. Do not contact Docker
                # (which can socket-activate its daemon) just to display history.
                if instance.get('running') is False and not instance.get('initializer'):
                    continue
                if any(j['profile_id'] == identity and j['status'] in {'queued', 'starting'} for j in self.jobs.values()):
                    continue
                try:
                    info = await asyncio.wait_for(self.inspect(instance['name']), 2)
                    state = info.get('State', {}) if info else {}
                    running = bool(state.get('Running'))
                    error = str(state.get('Error') or '')
                    if state.get('OOMKilled') or (not running and state.get('ExitCode', 0)):
                        error = error or f"Контейнер завершился с кодом {state.get('ExitCode')}."
                    if error:
                        self.container_errors[identity] = error
                    elif running or not instance.get('running'):
                        self.container_errors.pop(identity, None)
                    if not info and instance.get('running'):
                        self.container_errors[identity] = 'Запущенный контейнер больше не найден.'
                    self._set_instance(identity, dict(instance, running=running))
                except (RuntimeError, OSError, asyncio.TimeoutError) as error:
                    self.container_errors[identity] = str(error) or 'Docker не отвечает.'
        finally:
            self._probing = False

    def schedule_state_refresh(self):
        if not self._closed and (self._probe_task is None or self._probe_task.done()):
            self._probe_task = asyncio.create_task(self.refresh_states())

    def route(self, context=None, *, session_id='', project_id=''):
        if context is not None:
            if context.run_id in self.frozen:
                return copy.deepcopy(self.frozen[context.run_id])
            session_id = context.session_id or ''
        catalog = self.project_catalog()
        folder = catalog.get('session_folders', {}).get(session_id) if session_id else project_id
        chain = self.sessions._folder_chain(folder, catalog)
        root = chain[0]['id'] if chain else project_id
        return self.store.resolve(session_id, chain, root)

    def project_catalog(self):
        # No message/history reads and no opening every session database on UI polling.
        path = Path(self.sessions.projects_catalog_path)
        if not path.exists():
            return {'folders': {}, 'session_folders': {}}
        return json.loads(path.read_text(encoding='utf-8'))

    @asynccontextmanager
    async def execution_scope(self, context):
        if self.route(context)['id']=='shared':
            try:await self.prepare_shared_workspace()
            except (RuntimeError,OSError,ValueError) as error:
                self.container_errors['shared']=str(error) or 'Docker не отвечает.'
        profile = self.effective_profile(self.route(context), context)
        run_id, identity = context.run_id, profile['id']
        if identity in self.maintenance or identity in self.completion_profiles:
            raise ValueError('Терминал обслуживается. Дождитесь завершения операции.')
        # No await between checking manual activity and reserving the route.
        if any(j['profile_id'] == identity and j.get('source') == 'manual' and j['status'] not in TERMINAL_STATES
               and not (j.get('background') or j.get('interactive') or j.get('detached')) for j in self.jobs.values()):
            raise ValueError('Этот терминал выполняет ручную команду. Дождитесь её завершения или отмените её перед запуском агента.')
        self.frozen[run_id] = profile
        self.reservations.setdefault(identity, set()).add(run_id)
        self.changed()
        try:
            yield
        finally:
            self.frozen.pop(run_id, None)
            self.reservations.get(identity, set()).discard(run_id)
            for resource_profile in self.resource_profiles.pop(run_id, set()):
                self.reservations.get(resource_profile, set()).discard(run_id)
            self.changed()

    async def prepare_resources(self, files, version, context, profile_id='', origin=None):
        from .resources import prepare
        return await prepare(self, files, version, context, profile_id, origin)

    @staticmethod
    def fingerprint(profile):
        keys = ('image', 'network', 'cpus', 'memory_mb', 'pids', 'mounts', 'environment')
        values = {k: profile[k] for k in keys}
        values.update(security_revision=SECURITY_REVISION,user=container_user(profile))
        if profile.get('restrict_writes'):
            values['restrict_writes'] = True
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def project_context(self, session_id='', project_id='', workspace=None):
        from core.context import ExecutionContext
        catalog = self.project_catalog()
        folder = catalog.get('session_folders', {}).get(session_id) if session_id else project_id
        chain = self.sessions._folder_chain(folder, catalog)
        workspace = workspace or (chain[0].get('settings', {}).get('workspace_path') if chain else '') or str(self.store.workspace)
        return ExecutionContext.create(session_id=session_id or None, workspace_root=workspace)

    def _shared_workspace_root(self, value):
        root=Path(value['root']).expanduser().resolve()
        old_app=Path(value.get('app_root') or APP_PATHS.root).resolve()
        if old_app!=APP_PATHS.root.resolve() and root.is_relative_to(old_app):
            root=APP_PATHS.root/root.relative_to(old_app)
        return root.resolve()

    def shared_workspace(self):
        path=self.store.root/'shared-workspace.json'
        root=(self._shared_workspace_root(json.loads(path.read_text(encoding='utf-8')))
              if path.exists() else self.store.workspace.resolve())
        validate_host_directory(root)
        if root.is_dir():return root
        # This is an implicit, migrated mount cache, not the selected project's
        # workspace. A removed legacy directory must not poison every new tab.
        fallback=validate_host_directory(self.store.workspace)
        if not fallback.is_dir():
            raise ValueError(f'Папка общего терминала «{root}» недоступна; '
                             f'стандартная папка «{fallback}» тоже не существует. '
                             'Проверьте расположение приложения и настройки терминала.')
        return fallback

    async def prepare_shared_workspace(self, *, starting=False):
        """Recover stale implicit roots; keep valid shared mounts across projects."""
        path=self.store.root/'shared-workspace.json'
        profile=self.store.get('shared')
        if profile['backend']!='docker' or not profile['active']:return
        if path.exists():
            saved=self._shared_workspace_root(json.loads(path.read_text(encoding='utf-8')))
            if saved==self.shared_workspace():return
        instance=self._instances().get('shared')
        # Displaying saved/stopped settings must not start a Docker daemon.
        if not path.exists() and instance and instance.get('running') is False and not starting:return
        if not hasattr(self,'_shared_workspace_lock'):self._shared_workspace_lock=asyncio.Lock()
        async with self._shared_workspace_lock:
            previous=None
            if path.exists():
                value=json.loads(path.read_text(encoding='utf-8'))
                previous=self._shared_workspace_root(value)
                root=self.shared_workspace()
                if previous==root:return
            else:
                root=self.store.workspace.resolve()
                instance=self._instances().get('shared')
                if instance:
                    info=await asyncio.wait_for(self.inspect(self._name('shared')),3)
                    # Extra mounts may contain a different project or a tool's
                    # resources. Only /workspace identifies the shared root.
                    mount=next((m for m in (info or {}).get('Mounts',[])
                                if m.get('Type')=='bind' and m.get('Destination')=='/workspace'),None)
                    if mount is not None:
                        candidate=validate_host_directory(mount['Source'])
                        if candidate.is_dir():root=candidate
            validate_host_directory(root)
            if not root.is_dir():
                raise ValueError(f'Стандартная папка общего терминала не существует: {root}')
            record={'root':str(root),'app_root':str(APP_PATHS.root)}
            if previous is not None:record['recovered_from']=str(previous)
            write_json(path,record)
            self._ready.pop('shared',None)
            self.directories={k:v for k,v in self.directories.items() if not k.startswith('shared:')}
            self.changed()

    def effective_profile(self, profile, context):
        result = copy.deepcopy(profile)
        workspace = Path(context.workspace_root if context else self.store.workspace).resolve()
        if profile['backend'] != 'docker':
            result['_workspace'] = str(workspace)
            return result
        shared=profile['id']=='shared'
        if shared:workspace=self.shared_workspace()
        validate_host_directory(workspace)
        mounts=[]
        workspace_readonly = False
        for mount in result['mounts']:
            # /workspace in the built-in fallback is its stable shared root.
            # Legacy settings serialized its absolute host path. Never use that
            # obsolete location after relocating Toshy. Project selection does
            # not change the shared environment's root or fingerprint.
            if profile['id'] == 'shared' and str(mount['container']).rstrip('/') == '/workspace':
                workspace_readonly |= bool(mount.get('readonly', False))
                continue
            # Older shared profiles stored the application's default workspace.
            # It is an implicit mapping, not permission to expose other projects.
            if (Path(mount['host']).resolve()==self.store.workspace.resolve()
                    and mount['container']=='/workspace' and workspace!=self.store.workspace.resolve()):continue
            mounts.append(validate_mount(mount,None if shared else workspace))
        if not any(workspace.is_relative_to(Path(m['host'])) for m in mounts):
            if any(m['container']=='/workspace' for m in mounts):
                raise ValueError('Точка /workspace занята другой папкой. Удалите её из настроек терминала.')
            mounts.append({'host':str(workspace),'container':'/workspace','readonly':workspace_readonly})
        result['mounts'] = mounts
        result['_workspace'] = str(workspace)
        return result

    async def _ensure_image(self, profile, job):
        async with self._build_lock:
            code, text = await self.cli('image', 'inspect', profile['image'], check=False)
            if not code:
                info = json.loads(text)[0]
                if info.get('Os', 'linux') != 'linux':
                    raise ValueError('Для терминала нужен Linux-образ, в том числе при запуске Toshy в Windows.')
                return
            if profile['image'] == DEFAULT_IMAGE:
                self._append(job, 'Подготовка стандартного образа…\n')
                await self.cli('build', '--label', f'toshy.terminal.owner={self.store.owner}', '-t', DEFAULT_IMAGE, str(MODULE_DIR), timeout=1800,
                               output=lambda value: self._append(job, value))
            else:
                self._append(job, 'Загрузка образа…\n')
                await self.cli('pull', profile['image'], timeout=1800, output=lambda value: self._append(job, value))

    async def ensure_running(self, profile, job):
        identity = profile['id']
        async with self.lifecycle_locks.setdefault(identity, asyncio.Lock()):
            name, fingerprint = self._name(identity), self.fingerprint(profile)
            instance = self._instances().get(identity)
            if (self._ready.get(identity) == fingerprint and instance and instance.get('running')
                    and identity not in self.container_errors):
                return name
            info = await self.inspect(name)
            old_ephemeral = instance and not instance.get('preserve', True) and instance.get('app_session') != self.app_session
            if old_ephemeral:
                if info:
                    await self.cli('rm', '-f', name)
                if instance.get('home_volume'):
                    await self.home_volume(identity, remove=True)
                info = None
            if info:
                labels = info.get('Config', {}).get('Labels') or {}
                if labels.get('toshy.terminal.config') != fingerprint:
                    differences = configuration_differences(info, profile)
                    if not differences:
                        # A different hash can describe equivalent settings
                        # (legacy serialization, mount order, 2 versus 2.0).
                        # Verify inherited image variables before reusing it.
                        _, image_text = await self.cli('image', 'inspect', info.get('Image') or profile['image'])
                        image = json.loads(image_text)[0]
                        differences = configuration_differences(info, profile, (image.get('Config') or {}).get('Env') or [])
                    if differences:
                        raise ValueError('Параметры существующего контейнера изменились. Отличаются: '
                            + ', '.join(differences) + '. Окружение не удалено. '
                            'Если изменения намеренные, нажмите «Пересоздать»: это удалит установленные внутри пакеты и файлы, '
                            'но сохранит подключённые папки. Если переключили проект, используйте назначенное ему окружение '
                            'или вернитесь к прежней Project Folder.')
                try:
                    validate_container(info,profile,container_user(profile),self._name(identity)+'-home')
                    await self.home_volume(identity)
                except ValueError:
                    if info.get('State',{}).get('Running'):await self.cli('stop','-t','1',name,timeout=15)
                    self._set_instance(identity,dict(instance or {},name=name,running=False))
                    raise
            if not info:
                await self._ensure_image(profile, job)
                self.store.workspace.mkdir(parents=True, exist_ok=True)
                args = ['create', '--name', name, '--label', f'toshy.terminal.owner={self.store.owner}',
                        '--label', f'toshy.terminal.config={fingerprint}', '--init', '--stop-timeout', '2',
                        '--memory', f"{profile['memory_mb']}m", '--cpus', str(profile['cpus']),
                        '--memory-swap',f"{profile['memory_mb']}m",'--pids-limit', str(profile['pids']),
                        '--user',container_user(profile),*isolation_options(profile),
                        '--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777','--env', 'HOME=/home/toshy']
                if not profile['network']:
                    args += ['--network', 'none']
                volume = self._name(identity) + '-home'
                initializer=name+'-init'
                self._set_instance(identity, {'name': name, 'preserve': profile['preserve'], 'app_session': self.app_session,
                                             'home_volume': True,'initializer':initializer})
                await self.home_volume(identity)
                # The fixed initializer never inherits the agent's capabilities
                # or host folders, and has no network in either execution mode.
                try:
                    await self.cli('run','--rm','--name',initializer,'--label',f'toshy.terminal.owner={self.store.owner}',
                        '--read-only','--network','none','--user','0:0',*isolation_options(),
                        '--memory','64m','--memory-swap','64m','--cpus','0.5','--pids-limit','32',
                        '--mount',f'type=volume,source={volume},target=/home/toshy,volume-nocopy',
                        '--entrypoint','/bin/sh',profile['image'],'-c','chmod 1777 /home/toshy',timeout=30)
                finally:
                    await self.cli('rm','-f',initializer,timeout=15,check=False)
                args += ['--mount',f'type=volume,source={volume},target=/home/toshy,volume-nocopy']
                if profile.get('restrict_writes'):args += ['--read-only']
                for mount in profile['mounts']:
                    mount=validate_mount(mount,None if identity=='shared' else profile.get('_workspace',self.store.workspace))
                    host = Path(mount['host'])
                    if not host.is_dir():
                        raise ValueError(f'Подключаемая папка не существует: {host}')
                    suffix = 'ro' if mount['readonly'] else 'rw'
                    if os.name != 'nt':
                        suffix += ',z'  # Shared SELinux label: Fedora; accepted by Docker on other Linuxes.
                    args += ['--volume', f"{host}:{mount['container']}:{suffix},rprivate"]
                env_path = self.store.root / f'.environment-{identity}-{uuid.uuid4().hex}'
                try:
                    # Avoid exposing secrets in the Docker command line/process list.
                    if profile['environment']:
                        if any('\n' in v or '\r' in v for v in profile['environment'].values()):
                            raise ValueError('Переменные окружения должны занимать одну строку.')
                        env_path.parent.mkdir(parents=True, exist_ok=True)
                        fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                            stream.write('\n'.join(f'{k}={v}' for k, v in profile['environment'].items()))
                        args += ['--env-file', str(env_path)]
                    args += ['--env', 'HOME=/home/toshy', '--env', 'TMPDIR=/home/toshy/.cache/tmp',
                             '--env', 'PIP_CACHE_DIR=/home/toshy/.cache/pip', '--env', 'npm_config_cache=/home/toshy/.cache/npm',
                             '--entrypoint', '/bin/sh', profile['image'], '-c',
                             'mkdir -p /home/toshy/.cache/tmp /home/toshy/.terminal-jobs; trap "exit 0" TERM INT; while :; do sleep 3600 & wait $!; done']
                    self._set_instance(identity, {'name': name, 'preserve': profile['preserve'], 'app_session': self.app_session,
                                                'home_volume': True})
                    await self.cli(*args)
                finally:
                    env_path.unlink(missing_ok=True)
            if not info or not info.get('State', {}).get('Running'):
                self._append(job, f"Запуск: {profile['name']}\n")
                await self.cli('start', name)
            # All commands use Bash job control for cancellable process groups.
            try:
                await self.cli('exec', name, '/bin/bash', '-c', 'command -v bash >/dev/null')
            except RuntimeError as error:
                await self.cli('stop', '-t', '1', name, timeout=10, check=False)
                raise ValueError('Образ должен содержать /bin/sh и /bin/bash. ' + str(error)) from error
            self._set_instance(identity, {'name': name, 'preserve': profile['preserve'], 'app_session': self.app_session,
                                         'running': True, 'home_volume': True})
            self.container_errors.pop(identity, None)
            self._ready[identity] = fingerprint
            return name

    def working_directory(self, profile, cwd=None, context=None):
        if profile['backend'] == 'local':
            return str(Path(cwd or (context.workspace_root if context else self.store.workspace)).expanduser().resolve())
        if profile['backend'] == 'ssh':
            return str(cwd or profile.get('ssh_root') or '~')
        if cwd:
            raw = str(cwd)
        elif profile['id']=='shared':
            raw=profile.get('_workspace') or str(self.shared_workspace())
        elif context is not None:
            raw = str(context.workspace_root)
        else:
            raw = str(self.store.workspace)
        for mount in sorted(profile['mounts'], key=lambda m: len(m['host']), reverse=True):
            host = Path(mount['host'])
            try:
                relative = Path(raw).resolve().relative_to(host.resolve())
            except (ValueError, OSError):
                relative = None
            if relative is not None:
                return posixpath.normpath(posixpath.join(mount['container'], relative.as_posix()))
        if cwd is None and context is not None:
            # Context workspace must not silently resolve to an unrelated project.
            raise ValueError('Project Folder проекта не подключена к контейнеру. Добавьте её в «Папки» настроек терминала.')
        if cwd and context is not None and Path(raw).resolve().is_relative_to(context.workspace_root):
            raise ValueError('Эта папка проекта не подключена к общему терминалу. Подключения общего терминала не меняются при выборе проекта; добавьте нужную папку в его настройках или используйте файловые инструменты.')
        return container_path(raw)

    def submit(self, profile, command, *, cwd=None, timeout=None, background=False, source='manual', context=None,
               interactive=False, tty=False, columns=100, rows=30):
        if self._closed:
            raise ValueError('Терминал завершает работу.')
        if profile['id'] in self.maintenance:
            raise ValueError('Терминал обслуживается. Дождитесь завершения операции.')
        if profile['id'] in self.completion_profiles:
            raise ValueError('Дождитесь подготовки дополнения терминала.')
        if not profile['active']:
            raise ValueError(f"Терминал «{profile['name']}» отключён. Включите «Активен» в его настройках.")
        if not str(command).strip() or '\x00' in command or len(command) > 131072:
            raise ValueError('Введите команду длиной не более 131072 символов, без нулевых символов.')
        if source == 'manual':self.check_manual_input()
        columns,rows=dimensions(columns,rows)
        interactive=bool(interactive or tty)
        timeout = (None if background or interactive else profile['timeout']) if timeout is None else int(timeout)
        if timeout is not None and not 1 <= timeout <= 86400:
            raise ValueError('Тайм-аут должен быть от 1 до 86400 секунд.')
        profile = profile if profile.get('_workspace') else self.effective_profile(profile, context)
        base_cwd = self.working_directory(profile, None, context)
        directory_key = self.directory_key(profile, context)
        job = {'id': uuid.uuid4().hex, 'profile_id': profile['id'], 'profile_name': profile['name'],
               'session_id': (context.session_id or '') if context else '', 'run_id': context.run_id if context else '',
               'command': command, 'cwd': self.working_directory(profile, cwd, context) if cwd else self.directories.get(directory_key, base_cwd),
               'base_cwd': base_cwd, 'directory_key': directory_key, 'inherit_cwd': not bool(cwd),
               'timeout': timeout, 'background': bool(background), 'source': source,
               'interactive':interactive, 'tty':bool(tty), 'columns':columns, 'rows':rows,
               'backend': profile['backend'], 'target': profile['ssh_host'] if profile['backend'] == 'ssh' else profile['backend'],
               'app_session': self.app_session,
               'status': 'queued', 'created': time.time(), 'exit_code': None, 'error': '', 'bytes': 0}
        if profile['backend']=='docker':job['docker_user']=container_user(profile)
        self.jobs[job['id']] = job
        self.job_contexts[job['id']]=context
        if tty:self.screens[job['id']]=TerminalScreen(columns,rows)
        self._save_job(job)
        from core.logger import core_logger
        core_logger.event('modules', 'TERMINAL_COMMAND', 'Command queued', details={
            'job_id': job['id'], 'terminal': profile['name'], 'backend': profile['backend'], 'source': source})
        runner = self._run(job, copy.deepcopy(profile)) if profile['backend'] == 'docker' else self._run_host(job, copy.deepcopy(profile), context)
        from .file_history import run_job
        self.tasks[job['id']] = asyncio.create_task(run_job(self,job,profile,context,runner))
        return self.public_job(job)

    def directory_key(self, profile, context=None):
        # One shared container must not mix the working directories of its sessions.
        scope = [(context.session_id or '') if context else '',
                 str(context.workspace_root if context else self.store.workspace)]
        return profile['id'] + ':' + hashlib.sha256(json.dumps(scope).encode()).hexdigest()

    def current_directory(self, profile, context=None):
        profile = profile if profile.get('_workspace') else self.effective_profile(profile, context)
        return self.directories.get(self.directory_key(profile, context), self.working_directory(profile, None, context))

    async def _read_directory(self, name, job):
        path = '/home/toshy/.terminal-jobs/' + job['id'] + '.cwd'
        options = ['exec'] + (['--user', job['docker_user']] if job.get('docker_user') else [])
        code, text = await self.cli(*options, name, '/bin/bash', '-c',
            'if test -f ' + path + '; then cat -- ' + path + '; rm -f -- ' + path + '; else exit 1; fi',
            timeout=5, check=False)
        if not code and text.endswith('\n'):
            directory = text[:-1]
            if directory.startswith('/') and not any(c in directory for c in '\x00\n\r'):
                return directory
        return None

    @asynccontextmanager
    async def _command_slot(self, identity, job):
        lock = self.locks.setdefault(identity, asyncio.Lock())
        await lock.acquire()
        released = False
        def release_background():
            nonlocal released
            if (job['background'] or job.get('detached') or job.get('interactive')) and not released:
                lock.release()
                released = True
        self.slot_releases[job['id']]=release_background
        try:
            yield release_background
        finally:
            self.slot_releases.pop(job['id'],None)
            if not released:
                lock.release()

    @staticmethod
    def public_job(job):
        result = dict(job)
        if result.get('terminal_shell'):result['command']=result.get('display_command','shell')
        for key in ('base_cwd', 'directory_key', 'inherit_cwd'):
            result.pop(key, None)
        if len(result.get('command', '')) > 4096:
            result['command'] = result['command'][:4096] + '\n…'
            result['command_truncated'] = True
        return result

    def _append(self, job, text):
        if job.get('terminal_shell'):text=self.terminal_filter(job,text)
        if job['id'] in self.screens:
            self.screens[job['id']].feed(text)
        self.transcripts.append(job,text)
        if text:self.changed()

    async def _signal(self, name, job_id):
        # The pidfile is written by our wrapper, never supplied by the model.
        script = ('p=/home/toshy/.terminal-jobs/' + job_id + '.pid; '
                  'for ((i=0;i<40;i++)); do test -f "$p" && break; sleep 0.05; done; '
                  'test -f "$p" || exit 0; read -r group < "$p"; '
                  'case "$group" in ""|*[!0-9]*) exit 1;; esac; '
                  'kill -INT -- -"$group" 2>/dev/null; sleep 0.4; '
                  'kill -TERM -- -"$group" 2>/dev/null; sleep 0.4; '
                  'kill -KILL -- -"$group" 2>/dev/null; rm -f "$p"; exit 0')
        if self.jobs.get(job_id,{}).get('terminal_shell'):
            script=script.replace('kill -INT --', 'kill -HUP -- -"$group" 2>/dev/null; sleep 0.1; kill -INT --',1)
        user = self.jobs.get(job_id, {}).get('docker_user')
        args = ['exec'] + (['--user', user] if user else [])
        await self.cli(*args, name, '/bin/bash', '-c', script, timeout=8)

    async def _run(self, job, profile):
        proc = None
        try:
            async with self._command_slot(profile['id'], job) as release_background:
                job.update(status='starting', started=time.time())
                self._save_job(job)
                name = await self.ensure_running(profile, job)
                if job.get('cancel_requested'):
                    job['status'] = 'cancelled'
                    return
                # Resolve after acquiring the queue slot: the preceding `cd`
                # may not have finished when this command was submitted.
                if job['inherit_cwd']:
                    job['cwd'] = self.directories.get(job['directory_key'], job['base_cwd'])
                cwd_file = '/home/toshy/.terminal-jobs/' + job['id'] + '.cwd'
                enter = 'builtin cd -- ' + shlex.quote(job['cwd'])
                if job['inherit_cwd']:
                    enter += ' || builtin cd -- ' + shlex.quote(job['base_cwd'])
                # A private control file keeps directory metadata out of stdout.
                # EXIT also covers `exit N` and a command failing under `set -e`.
                capture = 'builtin pwd -P > ' + cwd_file
                # Bash job control gives the command its own process group without a PTY/setsid dependency.
                wrapper = ('set -m; p=/home/toshy/.terminal-jobs/' + job['id'] + '.pid; '
                           '( ' + enter + ' || exit $?; trap ' + shlex.quote(capture) + ' EXIT; '
                           + job['command'] + '\n) & group=$!; printf "%s\\n" "$group" > "$p"; '
                           'set +m; wait "$group"; code=$?; rm -f "$p"; exit "$code"')
                options = ['exec'] + (['--user', job['docker_user']] if job.get('docker_user') else [])
                if job.get('interactive'):
                    options += ['-i'] + (['-t','-e','TERM=xterm-256color'] if job.get('tty') else [])
                    wrapper = (enter + ' || exit $?; p=/home/toshy/.terminal-jobs/' + job['id'] + '.pid; '
                               'printf "%s\\n" "$$" > "$p"; trap ' + shlex.quote(capture + '; rm -f "$p"') + ' EXIT;\n')
                    if job.get('tty'):
                        wrapper += f'stty cols {job["columns"]} rows {job["rows"]} 2>/dev/null || true;\n'
                    wrapper += job['command']
                    if job.get('terminal_shell'):
                        wrapper=(enter+' || exit $?; p=/home/toshy/.terminal-jobs/'+job['id']+'.pid; '
                                 'printf "%s\\n" "$$" > "$p"; '+capture+'; exec '+job['command'])
                    if not job.get('tty'):
                        # Backgrounding with job control provides a cancellable
                        # process group; explicitly preserve stdin for the child.
                        wrapper=('set -m; exec 3<&0; p=/home/toshy/.terminal-jobs/'+job['id']+'.pid; '
                                 '( '+enter+' || exit $?; trap '+shlex.quote(capture)+' EXIT;\n'+job['command']+
                                 '\n) <&3 & group=$!; printf "%s\\n" "$group" > "$p"; '
                                 'exec 3<&-; set +m; wait "$group"; code=$?; rm -f "$p"; exit "$code"')
                args=[self.docker,*options,'-w',job['base_cwd'],name,'/bin/bash','-c',wrapper]
                if job.get('tty'):
                    from .pty_transport import spawn
                    from .host_process import clean_environment
                    proc=await spawn(args,env=clean_environment(),columns=job['columns'],rows=job['rows'])
                else:
                    proc = await asyncio.create_subprocess_exec(*args,
                        stdin=asyncio.subprocess.PIPE if job.get('interactive') else asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                self.processes[job['id']] = proc
                if job.get('tty'):self.screens[job['id']]=TerminalScreen(job['columns'],job['rows'])
                job['status'] = 'running'
                self._save_job(job)
                release_background()
                decoder = codecs.getincrementaldecoder('utf-8')('replace')
                async def consume():
                    waiter=asyncio.create_task(proc.wait())
                    try:
                        while chunk := await proc.stdout.read(16384):
                            self._append(job, decoder.decode(chunk))
                            await self.answer_terminal_queries(job,proc)
                        self._append(job, decoder.decode(b'', final=True))
                        return await waiter
                    finally:
                        if not waiter.done():waiter.cancel()
                        await asyncio.gather(waiter,return_exceptions=True)
                reader = asyncio.create_task(consume())
                try:
                    await asyncio.wait_for(asyncio.shield(reader), job['timeout'])
                except asyncio.TimeoutError:
                    job['status'] = 'timed_out'
                    await self._signal(name, job['id'])
                    try:
                        await asyncio.wait_for(asyncio.shield(reader), 4)
                    except asyncio.TimeoutError:
                        await self.cli('stop', '-t', '1', name, timeout=8)
                        proc.kill()
                        await proc.wait()
                finally:
                    if not reader.done():
                        await self._signal(name,job['id'])
                        try:await asyncio.wait_for(asyncio.shield(reader),3)
                        except (asyncio.TimeoutError,Exception):pass
                        if not reader.done():reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                job['exit_code'] = proc.returncode
                if job.get('cancel_requested'):
                    job['status'] = 'cancelled'
                elif job['status'] != 'timed_out':
                    job['status'] = 'completed' if proc.returncode == 0 else 'failed'
                result_status = job['status']
                job['status'] = 'finishing'
                try:
                    directory = await self._read_directory(name, job)
                    if directory:
                        job['final_cwd'] = directory
                        if not job.get('terminal_shell') and not job['background'] and not job.get('detached') and not job.get('cancel_requested') and result_status in {'completed', 'failed'}:
                            self.directories[job['directory_key']] = directory
                except (OSError, RuntimeError, asyncio.TimeoutError):
                    # Capturing the directory must not replace a command's result.
                    pass
                job['status'] = 'cancelled' if job.get('cancel_requested') else result_status
        except asyncio.CancelledError:
            job['status'] = 'cancelled'
            if proc is not None and proc.returncode is None:
                try:
                    await self._signal(self._name(profile['id']), job['id'])
                finally:
                    if proc.returncode is None:
                        proc.kill()
                    await proc.wait()
        except Exception as error:
            if job['status'] == 'starting':
                self.container_errors[profile['id']] = str(error)
            job.update(status='failed', error=str(error))
            self._append(job, '\n' + str(error) + '\n')
        finally:
            self.processes.pop(job['id'], None)
            if proc and job.get('tty'):
                proc.close()
            elif proc and proc.stdin:
                proc.stdin.close()
            job['finished'] = time.time()
            self._save_job(job)
            if job.get('exit_code') in (125, 126, 127):
                self._ready.pop(profile['id'], None)

    async def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            raise ValueError('Задание не найдено.')
        if job['status'] in TERMINAL_STATES:
            return self.public_job(job)
        job['cancel_requested'] = True
        task = self.tasks.get(job_id)
        if job_id in self.host_handles:
            await self._signal_host(job)
        elif job['status'] == 'approval' and task:
            task.cancel()
        elif job_id in self.processes:
            await self._signal(self._name(job['profile_id']), job_id)
        elif task:
            task.cancel()
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), 10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if job['status'] not in TERMINAL_STATES:  # task cancelled before its first instruction
            job.update(status='cancelled', finished=time.time())
            self._save_job(job)
        return self.public_job(job)

    def output(self, job_id, offset=0, limit=32768):
        job = self.jobs.get(job_id)
        if not job:
            raise ValueError('Задание не найдено.')
        path = self.store.root / 'jobs' / (job_id + '.out')
        size = path.stat().st_size if path.exists() else 0
        offset = min(max(0, int(offset)), size)
        data = b''
        if size:
            with path.open('rb') as stream:
                stream.seek(offset)
                data = stream.read(min(max(128, int(limit)), 131072))
            # Leave an incomplete trailing UTF-8 character for the next page.
            text = data.decode('utf-8', 'ignore')
            consumed = len(text.encode('utf-8'))
        else:
            text, consumed = '', 0
        result={'job':self.public_job(job),'text':text,'offset':offset+consumed,'size':size}
        if job.get('tty'):result['screen']=self.screen_snapshot(job_id)
        return result

    def feed(self, identity, cursors=None, lines=10000, states=None):
        self.sync_app_session()
        profile = self.store.get(identity)
        related = sorted((j for j in self.jobs.values() if j['profile_id'] == identity and
            (profile['save_history'] or j.get('app_session') == self.app_session or j['status'] not in TERMINAL_STATES)), key=lambda j: j['created'])
        recent_ids = {j['id'] for j in related[-300:]}
        recent = [j for j in related if j['id'] in recent_ids or j['status'] not in TERMINAL_STATES]
        records = []
        if cursors is None:
            remaining, budget = max(100, min(100000, int(lines))), 2 * 1024 * 1024
            for job in reversed(recent):
                if remaining <= 0 or budget <= 0:
                    break
                path = self.store.root / 'jobs' / (job['id'] + '.out')
                size = path.stat().st_size if path.exists() else 0
                payload = b''
                if size:
                    with path.open('rb') as stream:
                        stream.seek(max(0, size - budget))
                        payload = stream.read(budget)
                text = payload.decode('utf-8', 'ignore')
                pieces = text.splitlines(keepends=True)
                text = ''.join(pieces[-max(0, remaining - 2):]) if remaining > 2 else ''
                if size > len(text.encode('utf-8')):
                    text = '[Ранний вывод сохранён в логе]\n' + text
                remaining -= text.count('\n') + job['command'].count('\n') + 2
                budget -= len(text.encode('utf-8')) + len(job['command'][:4096].encode())
                record={'job':self.public_job(job),'text':text,'offset':size,'size':size}
                if job.get('tty'):record['screen']=self.screen_snapshot(job['id'])
                records.append(record)
            records.reverse()
        else:
            budget = 1024 * 1024
            for job in recent:
                offset = max(0, int(cursors.get(job['id'], 0)))
                size = (self.store.root / 'jobs' / (job['id'] + '.out')).stat().st_size if (self.store.root / 'jobs' / (job['id'] + '.out')).exists() else 0
                if job['id'] not in cursors or offset < size or job['status'] != (states or {}).get(job['id']):
                    page = self.output(job['id'], offset, min(131072, budget))
                    records.append(page)
                    budget -= len(page['text'].encode('utf-8'))
                    if budget <= 0:
                        break
        result = {'records': records, 'job_ids': [j['id'] for j in recent], 'revision': self.revision, 'epoch':self.app_session,
                  'pending': [{k:v for k,v in e.items() if k != 'future'} for e in self.policy.pending.values()]}
        busy = any(j['status'] not in TERMINAL_STATES and (not j.get('terminal_shell') or
                   self.terminal_sessions.get(j.get('terminal_session_id'),{}).get('state')!='ready') for j in related)
        connection = self.ssh_connections.get(identity)
        instance = self._instances().get(identity, {})
        ready = profile['backend'] == 'local' or bool(instance.get('running')) or (connection is not None and not connection.is_closed())
        result['state'] = 'error' if identity in self.container_errors else ('busy' if busy else ('running' if ready else 'stopped'))
        result['agent_busy'] = bool(self.reservations.get(identity))
        if cursors is None:
            result['cursors'] = {j['id']: (self.store.root / 'jobs' / (j['id'] + '.out')).stat().st_size
                if (self.store.root / 'jobs' / (j['id'] + '.out')).exists() else 0 for j in recent}
            result['states'] = {j['id']: j['status'] for j in recent}
        return result

    async def execute_command(self, command, timeout=None, is_background=False, cwd=None, *, interactive=False, tty=False,
                              yield_seconds=1, columns=100, rows=30):
        context = get_execution_context()
        profile = self.route(context)
        job = self.submit(profile,command,timeout=timeout,background=is_background,cwd=cwd,source='agent',context=context,
                          interactive=interactive,tty=tty,columns=columns,rows=rows)
        if is_background:
            return json.dumps(dict(job,job_id=job['id']), ensure_ascii=False)
        try:
            await asyncio.wait({self.tasks[job['id']]},timeout=min(30,max(0,float(yield_seconds))))
        except asyncio.CancelledError:
            await self.cancel(job['id'])
            raise
        self.detach_process(job['id'])
        result = self.public_job(self.jobs[job['id']])
        result['job_id']=job['id']
        page = self.output(job['id'], limit=65536)
        result['output'] = page['text']
        result['output_truncated'] = bool(result.get('output_truncated') or page['offset'] < page['size'])
        result['offset']=page['offset']
        if page.get('screen') is not None:result['screen']=page['screen']
        return json.dumps(result, ensure_ascii=False)

    def check_idle(self, identity):
        if identity in self.maintenance or identity in self.completion_profiles or self.reservations.get(identity) or any(j['profile_id'] == identity and j['status'] not in TERMINAL_STATES for j in self.jobs.values()):
            raise ValueError('Сначала завершите запуск агента и задания этого терминала.')

    async def control(self, identity, action):
        self.check_idle(identity)
        if action not in {'stop', 'reset', 'delete'}:
            raise ValueError('Неизвестное действие.')
        profile = self.store.get(identity)
        if identity in {'shared', 'local'} and action == 'delete':
            raise ValueError('Встроенный терминал нельзя удалить.')
        self._ready.pop(identity, None)
        if profile['backend'] != 'docker':
            connection = self.ssh_connections.pop(identity, None)
            if connection:
                connection.close(); await connection.wait_closed()
            if action == 'delete':
                self.store.delete(identity)
            if action != 'stop':
                self.directories = {k:v for k,v in self.directories.items() if not k.startswith(identity + ':')}
            self.changed()
            return
        self.maintenance.add(identity)
        try:
            async with self.lifecycle_locks.setdefault(identity, asyncio.Lock()):
                name = self._name(identity)
                # Unused definitions do not need Docker even to be deleted/reset.
                instance = self._instances().get(identity)
                info = await self.inspect(name) if instance else None
                if info:
                    if action == 'stop':
                        await self.cli('stop', '-t', '1', name, timeout=15)
                        instance = self._instances().get(identity, {})
                        self._set_instance(identity, dict(instance, running=False))
                    else:
                        await self.cli('rm', '-f', name, timeout=15)
                if action != 'stop':
                    if instance and instance.get('home_volume'):
                        await self.home_volume(identity, remove=True)
                    self._set_instance(identity, None)
                    self.directories = {key: value for key, value in self.directories.items() if not key.startswith(identity + ':')}
                elif instance and not info:
                    self._set_instance(identity, None)
                if action == 'delete':
                    self.store.delete(identity)
        finally:
            self.maintenance.discard(identity)

    async def images(self):
        _, text = await self.cli('image', 'ls', '--format', '{{json .}}')
        result = []
        for line in text.splitlines():
            item = json.loads(line)
            if item.get('Repository') != '<none>':
                result.append(item['Repository'] + ':' + item['Tag'])
        return sorted(set(result))

    async def import_image(self, path):
        if self._closed:
            raise ValueError('Терминал завершает работу.')
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise ValueError('Архив образа не найден на компьютере, где запущено ядро.')
        task = asyncio.current_task()
        self.image_operations.add(task)
        try:
            _, output = await self.cli('load', '--input', str(path), timeout=1800)
            return {'output': output, 'images': await self.images()}
        except asyncio.CancelledError:
            raise RuntimeError('Импорт образа прерван при завершении приложения.') from None
        finally:
            self.image_operations.discard(task)

    def catalog(self, session_id='', project_id='', workspace=None):
        self.sync_app_session()
        instances = self._instances()
        profiles = self.store.profiles()
        try:
            context = self.project_context(session_id, project_id, workspace)
        except (ValueError, OSError):
            context = None
        for profile in profiles:
            identity = profile['id']
            related = [j for j in self.jobs.values() if j['profile_id'] == identity]
            running = any(j['status'] not in TERMINAL_STATES and (not j.get('terminal_shell') or
                          self.terminal_sessions.get(j.get('terminal_session_id'),{}).get('state')!='ready') for j in related)
            instance = instances.get(identity)
            connected = (profile['backend'] == 'local' or (profile['backend'] == 'ssh' and identity in self.ssh_connections and not self.ssh_connections[identity].is_closed()))
            profile['state'] = 'busy' if running else ('running' if connected else (('running' if instance.get('running') else 'stopped') if instance else 'not_created'))
            if identity in self.container_errors:
                profile['state'], profile['state_error'] = 'error', self.container_errors[identity]
            profile['agent_busy'] = bool(self.reservations.get(identity))
            try:
                profile['cwd'] = self.current_directory(profile, context)
            except (ValueError, OSError):
                profile['cwd'] = ''
            profile['jobs'] = [self.public_job(j) for j in sorted(related, key=lambda j: j['created'], reverse=True)[:80]]
            # Values are only returned through the authenticated local endpoint.
        folders = [dict(value, id=key) for key, value in self.project_catalog().get('folders', {}).items()]
        return {'profiles': profiles, 'revision':self.revision, 'epoch':self.app_session,
                'resolved_id': self.route(session_id=session_id, project_id=project_id)['id'],
                'session_folders': self.project_catalog().get('session_folders', {}),
                'folders': folders, 'sessions': sorted(p.name for p in Path(self.sessions.sessions_dir).iterdir()
                    if p.is_dir() and (p / 'session.sqlite').is_file())}

    async def shutdown(self, app_close=False):
        self._closed = True
        if self._probe_task is not None and not self._probe_task.done():
            self._probe_task.cancel()
            await asyncio.gather(self._probe_task, return_exceptions=True)
        operations = [task for task in self.image_operations | self.completion_tasks | set(self.terminal_events.values()) if task is not asyncio.current_task()]
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        await asyncio.gather(*(self.cancel(j['id']) for j in list(self.jobs.values()) if j['status'] not in TERMINAL_STATES), return_exceptions=True)
        from .file_history import flush
        await flush(self)
        for connection in self.ssh_connections.values():
            connection.close()
        await asyncio.gather(*(c.wait_closed() for c in self.ssh_connections.values()), return_exceptions=True)
        self.ssh_connections.clear(); self._ready.clear()
        self.host_marker()
        self.changed()
        errors = []
        preservation = {p['id']: p['preserve'] for p in self.store.profiles()}
        async def close(identity, instance):
            try:
                async with self.lifecycle_locks.setdefault(identity, asyncio.Lock()):
                    initializer=instance.get('initializer')
                    preserve=preservation.get(identity,instance.get('preserve',True))
                    if instance.get('running') is False and not initializer and (not app_close or preserve):
                        return
                    if initializer:
                        if await self.inspect(initializer):await self.cli('rm','-f',initializer,timeout=15)
                    info = await self.inspect(instance['name'])
                    if info:
                        if app_close and not preservation.get(identity, instance.get('preserve', True)):
                            await self.cli('rm', '-f', instance['name'], timeout=15)
                            if instance.get('home_volume'):
                                await self.home_volume(identity, remove=True)
                            self._set_instance(identity, None)
                        else:
                            await self.cli('stop', '-t', '1', instance['name'], timeout=15)
                            self._set_instance(identity, dict(instance, running=False,
                                preserve=preservation.get(identity, instance.get('preserve', True))))
                    else:
                        if app_close and not preservation.get(identity, instance.get('preserve', True)) and instance.get('home_volume'):
                            await self.home_volume(identity, remove=True)
                        if instance.get('home_volume') and not (app_close and not preservation.get(identity, instance.get('preserve', True))):
                            self._set_instance(identity, dict(instance, running=False,
                                preserve=preservation.get(identity, instance.get('preserve', True))))
                        else:
                            self._set_instance(identity, None)
            except Exception as error:
                errors.append(str(error))
        await asyncio.gather(*(close(k, v) for k, v in self._instances().items()))
        if errors:
            raise RuntimeError('\n'.join(errors))
        if app_close:
            # A core intentionally left running remains usable by external clients.
            self._closed = False
