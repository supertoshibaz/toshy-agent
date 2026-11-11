"""Local and SSH runners behind the terminal's existing job API."""
import asyncio
import codecs
import json
import os
import shlex
import signal
import subprocess
from pathlib import Path

from . import host_process as hp


class HostRuntime:
    def host_marker(self):
        from .store import write_json
        path = self.store.root / 'host-runtime.json'
        if self.host_handles or self.ssh_connections:
            write_json(path, {'app_session': self.app_session})
        else:
            path.unlink(missing_ok=True)

    async def ssh_connection(self, profile, job):
        from .ssh_support import connect
        current = self.ssh_connections.get(profile['id'])
        if current is not None and not current.is_closed():
            return current
        self._append(job, 'Подключение к SSH…\n')
        connection = await connect(self, profile, job)
        self.ssh_connections[profile['id']] = connection
        self.container_errors.pop(profile['id'],None)
        self.host_marker()
        return connection

    async def _signal_host(self, job):
        handle = self.host_handles.get(job['id'])
        if not handle:
            return
        process, control, profile, owner = handle
        if job.get('tty') and profile['backend'] == 'local':
            if os.name!='nt':
                try:os.kill(process.pid,signal.SIGHUP)
                except ProcessLookupError:pass
                await asyncio.sleep(.1)
            process.kill()
            return
        if profile['backend'] == 'local':
            if os.name == 'nt':
                owner.kill()
            else:
                try:
                    for _ in range(40):
                        if Path(control + '.pid').exists() or process.returncode is not None: break
                        await asyncio.sleep(.025)
                    group = int(Path(control + '.pid').read_text().strip())
                    if group > 1:
                        os.killpg(group, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        else:
            connection, sftp = owner
            try:
                pid = None
                for _ in range(40):
                    try:
                        async with sftp.open(control + '.pid', 'r') as stream:
                            pid = int((await stream.read()).strip())
                        break
                    except Exception:
                        if process.exit_status is not None: break
                        await asyncio.sleep(.025)
                if pid is None and process.exit_status is None:
                    raise RuntimeError('Не удалось подтвердить остановку процесса на SSH-сервере.')
                if pid is not None and pid > 1:
                    if profile['target_os'] == 'windows':
                        command = f'taskkill /PID {pid} /T /F'
                    else:
                        command = f'kill -KILL -- -{pid}'
                    await asyncio.wait_for(connection.run(command, check=False), 8)
            finally:
                process.close()

    async def _run_host(self, job, profile, context=None):
        process, control, sftp, windows_job = None, None, None, None
        try:
            async with self._command_slot(profile['id'], job) as release_background:
                if job.get('cancel_requested'):
                    job['status'] = 'cancelled'; return
                if job['inherit_cwd']:
                    job['cwd'] = self.directories.get(job['directory_key'], job['base_cwd'])
                if not job.get('terminal_shell'):await self.policy.check(profile, job, context)
                job.update(status='starting', started=__import__('time').time()); self._save_job(job)
                if profile['backend'] == 'local':
                    directory = self.store.root / 'process-control'
                    directory.mkdir(parents=True, exist_ok=True)
                    control = str(directory / job['id'])
                    if job['inherit_cwd'] and not Path(job['cwd']).is_dir():
                        job['cwd'] = job['base_cwd']
                    args = hp.wrapper(profile, job, control, gate=os.name == 'nt' and not job.get('tty'))
                    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
                    if job.get('tty'):
                        from .pty_transport import spawn
                        process = await spawn(args, cwd=job['base_cwd'], env=hp.clean_environment(profile['environment']),
                                              columns=job['columns'], rows=job['rows'])
                    else:
                        process = await asyncio.create_subprocess_exec(*args, cwd=job['base_cwd'], env=hp.clean_environment(profile['environment']),
                            stdin=asyncio.subprocess.PIPE if job.get('interactive') else asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **options)
                    if os.name == 'nt' and not job.get('tty'):
                        try:
                            windows_job = hp.WindowsJob(process.pid)
                            Path(control + '.gate').touch()
                        except BaseException:
                            process.kill(); await process.wait(); raise
                    self.host_handles[job['id']] = (process, control, profile, windows_job)
                else:
                    connection = await self.ssh_connection(profile, job)
                    sftp = await connection.start_sftp_client()
                    home = await sftp.realpath('.')
                    directory = home.rstrip('/') + '/.toshy-terminal/' + self.store.owner
                    await sftp.makedirs(directory, exist_ok=True)
                    control = directory + '/' + job['id']
                    if profile['target_os'] == 'windows':
                        # Windows OpenSSH SFTP paths use /C:/..., PowerShell uses C:/...
                        control = control[1:] if len(control) > 3 and control[0] == '/' and control[2] == ':' else control
                        home = home[1:] if len(home) > 3 and home[0] == '/' and home[2] == ':' else home
                    if job['cwd'] in {'~', '.'}:
                        job['cwd'] = home
                    job['base_cwd'] = profile['ssh_root'] or home
                    args = hp.wrapper(profile, job, control)
                    import asyncssh
                    options = {'term_type':'xterm-256color','term_size':(job['columns'],job['rows'])} if job.get('tty') else {}
                    process = await connection.create_process(hp.remote_command(args), encoding=None, stderr=asyncssh.STDOUT, **options)
                    if not job.get('interactive'):
                        process.stdin.write_eof()
                    self.host_handles[job['id']] = (process, control, profile, (connection, sftp))
                self.processes[job['id']] = process
                if job.get('tty'):
                    from .terminal_screen import TerminalScreen
                    self.screens[job['id']]=TerminalScreen(job['columns'],job['rows'])
                self.host_marker()
                job['status'] = 'running'; self._save_job(job); release_background()
                decoder = codecs.getincrementaldecoder('utf-8')('replace')
                async def consume():
                    waiter = asyncio.create_task(process.wait())
                    try:
                        while chunk := await process.stdout.read(16384):
                            self._append(job, decoder.decode(chunk))
                            await self.answer_terminal_queries(job, process)
                        self._append(job, decoder.decode(b'', final=True))
                        await waiter
                    finally:
                        if not waiter.done():waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)
                reader = asyncio.create_task(consume())
                try:
                    await asyncio.wait_for(asyncio.shield(reader), job['timeout'])
                except asyncio.TimeoutError:
                    job['status'] = 'timed_out'; await self._signal_host(job)
                    await asyncio.wait_for(asyncio.shield(reader), 8)
                finally:
                    if not reader.done():
                        # Keep draining ConPTY while it shuts down; closing a
                        # full output pipe before that can deadlock Windows.
                        await self._signal_host(job)
                        try:await asyncio.wait_for(asyncio.shield(reader),3)
                        except (asyncio.TimeoutError,Exception):pass
                        if not reader.done():reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                code = process.returncode if profile['backend'] == 'local' else process.exit_status
                job['exit_code'] = code
                if job.get('cancel_requested'):
                    job['status'] = 'cancelled'
                elif job['status'] != 'timed_out':
                    job['status'] = 'completed' if code == 0 else 'failed'
                result_status = job['status']
                job['status'] = 'finishing'
                try:
                    if sftp:
                        async def read_directory():
                            async with sftp.open(control + '.cwd', 'r', encoding='utf-8') as stream:
                                return await stream.read()
                        directory = await asyncio.wait_for(read_directory(),5)
                    else:
                        directory = Path(control + '.cwd').read_text(encoding='utf-8')
                    directory = directory.rstrip('\r\n')
                    if directory and '\x00' not in directory:
                        job['final_cwd'] = directory
                        if not job.get('terminal_shell') and not job['background'] and not job.get('detached') and not job.get('cancel_requested') and result_status in {'completed', 'failed'}:
                            self.directories[job['directory_key']] = directory
                except Exception:
                    pass
                job['status'] = 'cancelled' if job.get('cancel_requested') else result_status
        except asyncio.CancelledError:
            job['status'] = 'cancelled'
            await self._signal_host(job)
            if process:
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except Exception:
                    pass
        except Exception as error:
            if job.get('cancel_requested'):
                job['status'] = 'cancelled'
            else:
                job.update(status='failed', error=str(error)); self._append(job, str(error) + '\n')
                if profile['backend'] == 'ssh':
                    self.container_errors[profile['id']] = str(error)
        finally:
            self.host_handles.pop(job['id'], None); self.processes.pop(job['id'], None)
            if job.get('tty') and profile['backend'] == 'local' and process:
                process.close()
            elif process and getattr(process,'stdin',None) and profile['backend']=='local':
                process.stdin.close()
            self.host_marker()
            if windows_job:
                windows_job.close()
            if control:
                for suffix in ('.cwd', '.pid', '.gate'):
                    try:
                        if sftp:
                            await asyncio.wait_for(sftp.remove(control + suffix),2)
                        else:
                            Path(control + suffix).unlink(missing_ok=True)
                    except Exception:
                        pass
            if sftp:
                sftp.exit()
            job['finished'] = __import__('time').time(); self._save_job(job)

    async def complete(self, profile, line, position, context):
        identity = profile['id']
        if (identity in self.maintenance or identity in self.completion_profiles or self.reservations.get(identity) or
                any(j['profile_id']==identity and not (j.get('background') or j.get('interactive') or j.get('detached'))
                    and j['status'] not in {'completed','failed','cancelled','timed_out','interrupted'} for j in self.jobs.values())):
            raise ValueError('Дождитесь завершения текущей команды терминала.')
        if self._closed:
            raise ValueError('Терминал завершает работу.')
        task = asyncio.current_task()
        import time,uuid
        job = {'id':uuid.uuid4().hex,'profile_id':profile['id'],'profile_name':profile['name'],
               'session_id':context.session_id or '', 'run_id':context.run_id, 'hidden':True,
               'command':'Tab — дополнение', 'cwd':self.current_directory(profile,context),
               'source':'manual','status':'starting','created':time.time(),'bytes':0,'exit_code':None,'error':''}
        self.completion_tasks.add(task);self.completion_profiles.add(profile['id'])
        try:
            return await self._complete(profile,line,position,context,job)
        finally:
            self.completion_tasks.discard(task);self.completion_profiles.discard(profile['id'])
            for suffix in ('.json','.out'):
                (self.store.root/'jobs'/(job['id']+suffix)).unlink(missing_ok=True)
            self.changed()

    async def _complete(self, profile, line, position, context, job):
        if len(line) > 131072 or not 0 <= position <= len(line):
            raise ValueError('Некорректный текст для дополнения.')
        if not profile['active']:
            raise ValueError('Терминал отключён.')
        profile = self.effective_profile(profile, context)
        cwd = job.get('completion_cwd') or self.current_directory(profile, context)
        generated = hp.completion_script(profile, line, position, cwd)
        windows = hp.shell(profile)[1]
        args, info = (generated, None) if windows else generated
        if profile['backend'] == 'local':
            process = await asyncio.create_subprocess_exec(*args, env=hp.clean_environment(profile['environment']),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                data, _ = await asyncio.wait_for(process.communicate(), 5)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait(); raise
            text = data.decode('utf-8', 'replace')
        elif profile['backend'] == 'docker':
            name = await self.ensure_running(profile,job)
            from .sandbox_security import container_user
            user = ['--user',container_user(profile)]
            _, text = await self.cli('exec', *user, name, *args, check=False, timeout=5)
        else:
            connection = await self.ssh_connection(profile,job)
            if cwd in {'~','.'}:
                sftp = await connection.start_sftp_client()
                try:
                    cwd = await sftp.realpath('.')
                finally:
                    sftp.exit()
                if profile['target_os'] == 'windows' and len(cwd)>3 and cwd[0]=='/' and cwd[2]==':': cwd=cwd[1:]
                generated = hp.completion_script(profile,line,position,cwd)
                args,info = (generated,None) if windows else generated
                self.directories[self.directory_key(profile,context)] = cwd
            result = await asyncio.wait_for(connection.run(hp.remote_command(args), check=False), 5)
            text = result.stdout
        if windows:
            result = json.loads(text.lstrip('\ufeff').strip())
            result['items'] = result.get('items') or []
            encoded = line.encode('utf-16-le')
            start, length = int(result['start']), int(result['length'])
            result['start'] = len(encoded[:start*2].decode('utf-16-le'))
            result['length'] = len(encoded[start*2:(start+length)*2].decode('utf-16-le'))
            return result
        items = []
        for value in text.replace('\n', '\0').split('\0'):
            if not value:
                continue
            if info['prefix'].startswith('~/') and value.startswith(os.path.expanduser('~/')) and profile['backend'] == 'local':
                value = '~/' + value[len(os.path.expanduser('~/')):]
            if info['quote']:
                value = info['quote'] + value.replace(info['quote'], '\\' + info['quote']) + info['quote']
            else:
                value = shlex.quote(value)
            items.append(value)
        return {'start': info['start'], 'length': info['length'], 'items': sorted(set(items))[:100]}
