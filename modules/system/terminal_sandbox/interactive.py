"""Process input, bounded waits and VT screen access shared by all backends."""
import asyncio
import json
import os
import uuid

from .terminal_screen import TerminalScreen
from .store import write_json

FINISHED={'completed','failed','cancelled','timed_out','interrupted'}
KEYS={'enter':'\r','tab':'\t','escape':'\x1b','backspace':'\x7f','up':'\x1b[A','down':'\x1b[B',
      'right':'\x1b[C','left':'\x1b[D','home':'\x1b[H','end':'\x1b[F','delete':'\x1b[3~',
      'pageup':'\x1b[5~','pagedown':'\x1b[6~','ctrl+c':'\x03','ctrl+d':'\x04','ctrl+z':'\x1a'}
KEYS.update({'f1':'\x1bOP','f2':'\x1bOQ','f3':'\x1bOR','f4':'\x1bOS',
             **{'f'+str(n):f'\x1b[{code}~' for n,code in enumerate((15,17,18,19,20,21,23,24),5)}})


def dimensions(columns, rows):
    columns,rows=int(columns),int(rows)
    if not 20<=columns<=240 or not 5<=rows<=100:
        raise ValueError('Размер терминала: ширина 20–240, высота 5–100 символов.')
    return columns,rows


class InteractiveRuntime:
    def interactive_init(self):
        self.screens={}
        self.job_contexts={}
        self.input_locks={}
        self.input_review_buffers={}
        self.slot_releases={}

    async def answer_terminal_queries(self, job, process):
        screen=self.screens.get(job['id'])
        if screen is None or not screen.queries or not getattr(process,'stdin',None):return
        replies=screen.queries;screen.queries=[]
        try:
            process.stdin.write(b''.join(replies))
            await process.stdin.drain()
        except (OSError,ValueError,ConnectionError):
            pass

    def screen_snapshot(self, identity):
        if identity in self.screens:return self.screens[identity].snapshot()
        path=self.store.root/'jobs'/(identity+'.screen.json')
        if path.exists():return json.loads(path.read_text(encoding='utf-8'))
        job=self.jobs.get(identity)
        transcript=self.store.root/'jobs'/(identity+'.out')
        if job and job.get('tty') and transcript.exists():
            screen=TerminalScreen(job.get('columns',100),job.get('rows',30))
            with transcript.open('rb') as stream:
                stream.seek(max(0,transcript.stat().st_size-262144))
                screen.feed(stream.read(262144).decode('utf-8','ignore'))
            snapshot=screen.snapshot();write_json(path,snapshot)
            return snapshot
        return None

    async def wait_output(self, identity, offset=0, seconds=1):
        if identity not in self.jobs:raise ValueError('Задание не найдено.')
        seconds=min(30,max(0,float(seconds)))
        deadline=asyncio.get_running_loop().time()+seconds
        while self.jobs[identity]['status'] not in FINISHED:
            event=self._event
            job=self.jobs[identity]
            if job.get('terminal_shell') and self.terminal_sessions.get(job.get('terminal_session_id'),{}).get('state')=='ready':break
            if self.jobs[identity].get('bytes',0)>int(offset):break
            remaining=deadline-asyncio.get_running_loop().time()
            if remaining<=0:break
            try:await asyncio.wait_for(event.wait(),remaining)
            except asyncio.TimeoutError:break
        return self.output(identity,offset)

    async def write_input(self, identity, text='', *, key=None, close_stdin=False, source='agent', context=None, wait_seconds=0):
        job=self.jobs.get(identity)
        if not job or not job.get('interactive'):raise ValueError('Процесс не принимает ввод. Запустите его с interactive=true или tty=true.')
        if not isinstance(text,str) or len(text)>32768 or '\x00' in text:raise ValueError('Ввод: не более 32768 символов без нулевых символов.')
        if key is not None and key not in KEYS:raise ValueError('Неизвестная клавиша.')
        if not text and key is None and not close_stdin:raise ValueError('Укажите текст, клавишу или закрытие ввода.')
        async with self.input_locks.setdefault(identity,asyncio.Lock()):
            process=self.processes.get(identity)
            if job['status'] in FINISHED or process is None or job.get('stdin_closed'):
                raise ValueError('Процесс завершён или ввод закрыт.')
            if job['status']!='running':raise ValueError('Процесс ещё не готов принимать ввод.')
            if source=='manual':self.check_manual_input()
            profile=self.store.get(job['profile_id'])
            expected_command=self.terminal_sessions.get(job.get('terminal_session_id'),{}).get('command_id') if job.get('terminal_shell') else None
            close_stdin=bool(close_stdin or key=='ctrl+d' and not job.get('tty'))
            key_data=KEYS[key] if key else ''
            screen=self.screens.get(identity)
            if screen and screen.application_cursor and key in {'up','down','left','right','home','end'}:
                key_data=key_data.replace('\x1b[','\x1bO')
            if key=='enter' and not job.get('tty'):key_data='\n'
            if key=='ctrl+d' and not job.get('tty'):key_data=''
            data=text+key_data
            if profile['backend']!='docker' and (text or key not in {None,'ctrl+c'}):
                buffer_key=(identity,source)
                previous=self.input_review_buffers.get(buffer_key,'')
                # Review accumulated input, not independent fragments which could
                # otherwise assemble a forbidden command across several writes.
                combined=previous+data
                if len(combined)>131072:
                    raise ValueError('Лимит проверяемого ввода достигнут. Завершите процесс и запустите новый.')
                parent=self.terminal_sessions.get(job.get('terminal_session_id'),{}).get('program',job['command']) if job.get('terminal_shell') else job['command']
                check=dict(job,id=uuid.uuid4().hex,command=parent+'\n'+combined,
                           hidden=True,source=source,finished=None)
                approval=asyncio.create_task(self.policy.check(profile,check,context or self.job_contexts.get(identity)))
                try:
                    owner=self.tasks.get(identity)
                    if owner:
                        await asyncio.wait({approval,owner},return_when=asyncio.FIRST_COMPLETED)
                        if not approval.done():raise ValueError('Процесс завершился во время проверки ввода.')
                    await approval
                finally:
                    if not approval.done():approval.cancel()
                    await asyncio.gather(approval,return_exceptions=True)
                    for suffix in ('.json','.out'):
                        (self.store.root/'jobs'/(check['id']+suffix)).unlink(missing_ok=True)
                # Submitted lines are not resent as terminal history. Keep only
                # the unfinished input line for split-command checks. Manual
                # input is never copied into the model's review buffer.
                self.input_review_buffers[buffer_key]=combined.replace('\r','\n').rsplit('\n',1)[-1]
            if job['status'] in FINISHED or self.processes.get(identity) is not process:
                raise ValueError('Процесс завершился во время проверки ввода.')
            if source=='manual':self.check_manual_input()
            if job.get('terminal_shell') and (text or key not in {None,'ctrl+c'}) and expected_command!=self.terminal_sessions.get(job['terminal_session_id'],{}).get('command_id'):
                raise ValueError('Программа во вкладке завершилась или сменилась во время проверки. Ввод не отправлен.')
            offset=job.get('bytes',0)
            if key=='ctrl+c' and not job.get('tty'):
                return {'job':await self.cancel(identity),'text':'','offset':offset,'size':offset}
            if data:
                process.stdin.write(data.encode('utf-8'))
                await process.stdin.drain()
            if close_stdin:
                if job.get('tty'):
                    windows=profile['backend']=='local' and os.name=='nt' or profile['backend']=='ssh' and profile['target_os']=='windows'
                    process.stdin.write(b'\x1a\r' if windows else b'\x04')
                    await process.stdin.drain()
                else:
                    if hasattr(process.stdin,'can_write_eof') and not process.stdin.can_write_eof():process.stdin.close()
                    else:process.stdin.write_eof()
                    job['stdin_closed']=True
            self.changed()
        return await self.wait_output(identity,offset,wait_seconds)

    async def resize_process(self, identity, columns, rows):
        job=self.jobs.get(identity)
        if not job or not job.get('tty'):raise ValueError('Процесс запущен без PTY.')
        columns,rows=dimensions(columns,rows)
        process=self.processes.get(identity)
        if process is None or job['status']!='running':raise ValueError('Процесс не выполняется.')
        if job['backend']=='ssh':
            process.change_terminal_size(columns,rows)
        else:process.resize(columns,rows)
        self.screens[identity].resize(columns,rows)
        job.update(columns=columns,rows=rows);self._save_job(job)
        return self.screen_snapshot(identity)

    def detach_process(self, identity):
        job=self.jobs[identity]
        if job['status'] not in FINISHED:
            job['detached']=True
            release=self.slot_releases.get(identity)
            if release:release()
            self._save_job(job)
