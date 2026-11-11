"""Доставка файла в выбранное окружение без перемонтирования Docker."""
import asyncio
import time
import uuid
from core.attachments import resolve_attachment_path, AttachmentError


async def _docker_copy(manager, container, source, destination):
    # Пишет пользователь контейнера: UID хоста и Docker Desktop здесь не важны.
    process = await asyncio.create_subprocess_exec(manager.docker, 'exec', '-i', container,
        'sh', '-c', 'umask 077; cat > "$1"', 'upload', destination,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    async def send():
        with source.open('rb') as file:
            while chunk := await asyncio.to_thread(file.read, 256 * 1024):
                process.stdin.write(chunk)
                await process.stdin.drain()
        process.stdin.close()
        output = await process.stdout.read(16384)
        await process.wait()
        if process.returncode:raise RuntimeError(output.decode('utf-8','replace') or 'Не удалось доставить файл в Docker.')
    try:
        await asyncio.wait_for(send(), 300)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


async def prepare_attachment(manager, attachment, context):
    if attachment.get('memory_only') or attachment.get('temp_memory'):
        raise AttachmentError('Файл передан только во временную память.')
    source = await asyncio.to_thread(resolve_attachment_path, attachment, context.session_id)
    profile = manager.effective_profile(manager.route(context), context)
    if not profile['active']:raise ValueError('Выбранный терминал отключён.')
    if profile['backend'] == 'local':return str(source)
    if attachment.get('workspace_reference'):
        if profile['backend']=='docker':
            return manager.working_directory(profile,str(source),context)
        raise ValueError('Вложение ссылается на файл проекта. Для SSH требуется доступ к исходной Project Folder; '
                         'автоматическая копия такого файла не создаётся.')
    locks = getattr(manager, '_attachment_locks', None)
    if locks is None:manager._attachment_locks=locks={}
    key=(profile['id'], attachment['id'])
    async with locks.setdefault(key, asyncio.Lock()):
        job = {'id': uuid.uuid4().hex, 'profile_id':profile['id'], 'profile_name':profile['name'],
               'session_id':context.session_id or '', 'run_id':context.run_id, 'source':'agent',
               'command':'Доставка вложения: '+attachment['filename'], 'cwd':'', 'status':'running',
               'created':time.time(), 'bytes':0, 'exit_code':None, 'error':'', 'hidden':True,
               'backend':profile['backend'], 'background':False, 'interactive':False, 'tty':False,
               'app_session':manager.app_session}
        manager.jobs[job['id']]=job
        manager.job_contexts[job['id']]=context
        manager._save_job(job)
        try:
            if profile['backend']=='docker':
                container=await manager.ensure_running(profile,job)
                directory='/home/toshy/uploads/'+attachment['id']
                destination=directory+'/'+source.name
                exists,_=await manager.cli('exec',container,'test','-f',destination,check=False)
                if exists:
                    await manager.cli('exec',container,'mkdir','-p',directory)
                    temporary=directory+'/.upload-'+uuid.uuid4().hex
                    try:
                        await _docker_copy(manager,container,source,temporary)
                        await manager.cli('exec',container,'mv',temporary,destination)
                    finally:
                        await manager.cli('exec',container,'rm','-f',temporary,check=False)
                await manager.cli('exec',container,'test','-r',destination,'-a','-w',destination)
            elif profile['backend']=='ssh':
                connection=await manager.ssh_connection(profile,job)
                sftp=await connection.start_sftp_client()
                try:
                    home=await sftp.realpath('.')
                    directory=home.rstrip('/')+'/.toshy-uploads/'+attachment['id']
                    destination=directory+'/'+source.name
                    if not await sftp.exists(destination):
                        await sftp.makedirs(directory,exist_ok=True)
                        temporary=directory+'/.upload-'+uuid.uuid4().hex
                        try:
                            await sftp.put(str(source),temporary,preserve=False)
                            if profile.get('target_os')!='windows':await sftp.chmod(temporary,0o600)
                            await sftp.rename(temporary,destination)
                        finally:
                            if await sftp.exists(temporary):await sftp.remove(temporary)
                finally:
                    sftp.exit()
                if profile.get('target_os')=='windows' and destination.startswith('/') and destination[2:3]==':':
                    destination=destination[1:]
            else:
                raise ValueError('Неизвестное окружение терминала.')
            job.update(status='completed',exit_code=0)
            return destination
        except BaseException as error:
            job.update(status='cancelled' if isinstance(error,asyncio.CancelledError) else 'error',error=str(error))
            raise
        finally:
            job['finished']=time.time()
            manager._save_job(job)
