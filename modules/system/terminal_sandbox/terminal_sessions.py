"""Persistent terminal tabs, with explicit ownership and private shell readiness events."""
import asyncio
import copy
import json
import os
import secrets
import shlex
import time
import uuid
from pathlib import Path

from .host_process import shell, powershell, ps_quote
from .store import write_json
from .interactive import FINISHED, KEYS


class TerminalSessions:
    def terminal_sessions_init(self):
        self.terminal_path=self.store.root/'terminal-sessions.json'
        self.terminal_sessions={}
        self.terminal_commands={}
        self.terminal_locks={}
        self.terminal_events={}
        self._terminal_tokens={}
        self._terminal_buffers={}
        if self.terminal_path.exists():
            self.terminal_sessions=json.loads(self.terminal_path.read_text(encoding='utf-8')).get('sessions',{})
        for tab in self.terminal_sessions.values():
            tab.update(state='stopped',command_id=None,job_id=None)
            try:
                if not self.store.get(tab['profile_id'])['save_history'] and tab.get('app_session')!=self.app_session:
                    tab['job_ids']=[]
            except ValueError:pass

    def _save_terminals(self):
        write_json(self.terminal_path,{'version':1,'sessions':self.terminal_sessions})
        self.changed()

    def _terminal_context(self, tab):
        workspace=tab['workspace']
        if tab['profile_id']=='shared' and self.store.get('shared')['backend']=='docker':
            workspace=str(self.shared_workspace())
        return self.project_context(tab.get('chat_session_id',''),tab.get('project_id',''),workspace)

    def check_manual_input(self):
        if any(self.reservations.values()):
            raise ValueError('Во время генерации ручной ввод в терминал заблокирован.')

    def terminal_public(self, tab):
        result={k:v for k,v in tab.items() if k not in {'workspace','run_id','job_ids'}}
        job=self.jobs.get(tab.get('job_id'))
        if job and job['status'] in FINISHED:
            result.update(state='stopped',exit_code=job.get('exit_code'),error=job.get('error',''))
        lock=self.terminal_locks.get(tab['id'])
        if result['state']=='ready' and lock and lock.locked():result['state']='checking'
        result['title']=tab.get('title') or tab.get('program') or 'shell'
        if tab['profile_id']=='shared' and (self.store.root/'shared-workspace.json').exists():
            profile=self.store.get('shared')
            if profile['backend']=='docker':result['mounted_folders']=self.effective_profile(profile,None)['mounts']
        return result

    def terminal_list(self, profile_id, context=None):
        result=[]
        for tab in self.terminal_sessions.values():
            if tab['profile_id']!=profile_id or tab.get('closed'):continue
            if context is not None:
                if tab['owner']!='agent' or tab.get('chat_session_id','')!=(context.session_id or ''):continue
                if not context.session_id and tab.get('run_id')!=context.run_id:continue
            result.append(self.terminal_public(tab))
        return sorted(result,key=lambda t:t['created'])

    def terminal_get(self, identity, context=None, profile_id=None):
        invocation=self.terminal_commands.get(str(identity))
        if invocation:identity=invocation['terminal_session_id']
        job=self.jobs.get(str(identity))
        if job and job.get('terminal_session_id'):identity=job['terminal_session_id']
        tab=self.terminal_sessions.get(str(identity))
        if not tab or tab.get('closed'):raise ValueError('Терминальная сессия закрыта или не найдена. Запросите список вкладок.')
        if profile_id is not None and tab['profile_id']!=profile_id:raise ValueError('Вкладка относится к другому окружению.')
        if context is not None:
            routed=self.route(context)['id']
            allowed_profiles = {routed} | self.resource_profiles.get(context.run_id, set())
            if tab['profile_id'] not in allowed_profiles or tab['owner']!='agent' or tab.get('chat_session_id','')!=(context.session_id or ''):
                raise ValueError('Терминальная сессия принадлежит другому чату или окружению.')
            if not context.session_id and tab.get('run_id')!=context.run_id:raise ValueError('Терминальная сессия принадлежит другому запуску.')
        return tab

    def terminal_open(self, profile, context, *, owner='agent', title='', project_id=''):
        if self._closed:raise ValueError('Терминал завершает работу.')
        identity=uuid.uuid4().hex
        tab={'id':identity,'terminal_session_id':identity,'profile_id':profile['id'],'owner':owner,
             'chat_session_id':context.session_id or '', 'project_id':project_id,
             'workspace':str(context.workspace_root),'run_id':context.run_id,'title':str(title)[:100],
             'program':Path(shell(profile)[0]).name,'state':'stopped','cwd':self.current_directory(profile,context),
             'job_id':None,'job_ids':[],'command_id':None,'created':time.time(),'closed':False,'app_session':self.app_session}
        self.terminal_sessions[identity]=tab;self._save_terminals()
        return self.terminal_public(tab)

    async def terminal_close(self, identity, context=None, profile_id=None):
        tab=self.terminal_get(identity,context,profile_id)
        # Mark closed before awaiting so another request cannot reopen it.
        invocation=self.terminal_commands.get(tab.get('command_id'))
        if invocation:
            invocation.update(status='cancelled',finished=time.time())
            timer=self.terminal_events.pop(invocation['id'],None)
            if timer:timer.cancel()
        tab['closed']=True;self._save_terminals()
        job=tab.get('job_id')
        if job in self.jobs:await self.cancel(job)
        self._terminal_tokens.pop(identity,None);self._terminal_buffers.pop(identity,None)
        self.terminal_locks.pop(identity,None)
        self._save_terminals()
        return {'terminal_session_id':identity,'state':'closed'}

    def _shell_bootstrap(self, profile, tab, token):
        executable,windows=shell(profile)
        profile=copy.deepcopy(profile)
        if windows:
            script=("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
                    "[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);"
                    "function global:prompt { $ok=$?; $code=0; if(!$ok){$code=1}; if($global:LASTEXITCODE){$code=$global:LASTEXITCODE};"
                    "[Console]::Write(([char]27)+']777;"+token+";ready;'+$code+';'+(Get-Location).Path+([char]7));"
                    "$global:LASTEXITCODE=0; return '' };$global:LASTEXITCODE=0")
            args=powershell(script,executable);args.remove('-NonInteractive');args.insert(1,'-NoExit')
            command='& '+ps_quote(args[0])+' '+' '.join(args[1:])
        else:
            if 'bash' not in Path(executable).name:
                raise ValueError('Для постоянных вкладок Linux выберите Bash в настройке оболочки.')
            prompt='__toshy_code=$?; printf "\\033]777;'+token+';ready;%s;%s\\007" "$__toshy_code" "$PWD"'
            # Per-tab bootstrap values belong to this exec, not the container's
            # configuration/fingerprint shared by all other tabs.
            command=shlex.join(['env','PS1=','PS2=… ','PROMPT_COMMAND='+prompt,executable,'--noprofile','--norc','-i'])
        return profile,command

    async def _terminal_start(self, tab, context):
        job=self.jobs.get(tab.get('job_id'))
        if job and job['status'] not in FINISHED:return job
        if tab['profile_id']=='shared':await self.prepare_shared_workspace(starting=True)
        profile=self.effective_profile(self.store.get(tab['profile_id']),self._terminal_context(tab))
        if tab['profile_id']=='shared' and profile['backend']=='docker':
            workspace_changed=Path(tab['workspace']).expanduser().resolve()!=Path(profile['_workspace'])
            tab['workspace']=profile['_workspace']
            if workspace_changed or not tab.get('job_ids'):
                tab['cwd']=self.working_directory(profile,None,self._terminal_context(tab))
        if not profile['active']:raise ValueError('Терминал отключён. Включите «Активен» в настройках окружения.')
        token=secrets.token_hex(24)
        runtime_profile,command=self._shell_bootstrap(profile,tab,token)
        self._terminal_tokens[tab['id']]=token
        self._terminal_buffers[tab['id']]=''
        # Only the fixed shell bootstrap is trusted. Every submitted command and
        # subsequent application input passes policy before reaching its stdin.
        result=self.submit(runtime_profile,command,cwd=tab['cwd'],source='agent',context=context,tty=True)
        job=self.jobs[result['id']]
        job.update(terminal_shell=True,terminal_session_id=tab['id'],display_command=Path(shell(profile)[0]).name,
                   source='manual' if tab['owner']=='user' else 'agent')
        tab.update(job_id=job['id'],state='starting',app_session=self.app_session)
        tab['job_ids']=(tab.get('job_ids',[])+[job['id']])[-30:]
        self._save_job(job);self._save_terminals()
        return job

    def terminal_filter(self, job, text):
        identity=job.get('terminal_session_id')
        if identity not in self._terminal_tokens:return text
        start='\x1b]777;'+self._terminal_tokens[identity]+';'
        data=self._terminal_buffers.get(identity,'')+text
        output=''
        while data:
            index=data.find(start)
            if index<0:
                keep=0
                for n in range(1,min(len(start),len(data))+1):
                    if data.endswith(start[:n]):keep=n
                output+=data[:-keep] if keep else data
                data=data[-keep:] if keep else ''
                break
            output+=data[:index];data=data[index:]
            end=data.find('\x07',len(start))
            if end<0:
                if len(data)>8192:output+=data;data=''
                break
            event=data[len(start):end];data=data[end+1:]
            parts=event.split(';',2)
            if len(parts)==3 and parts[0]=='ready':
                tab=self.terminal_sessions[identity]
                try:code=int(parts[1])
                except ValueError:continue
                cwd=parts[2]
                if not cwd or any(ord(c)<32 for c in cwd):continue
                invocation=self.terminal_commands.get(tab.get('command_id'))
                if invocation:
                    if invocation['status']=='running':invocation['status']='completed' if code==0 else 'failed'
                    invocation.update(exit_code=code,finished=time.time())
                    invocation['end_offset']=job['bytes']+len(output.encode('utf-8'))
                    from .file_history import finish_command
                    finish_command(self,invocation)
                    timer=self.terminal_events.pop(invocation['id'],None)
                    if timer:timer.cancel()
                tab.update(state='ready',cwd=cwd,program=Path(shell(self.store.get(tab['profile_id']))[0]).name,command_id=None)
                self._save_terminals()
        self._terminal_buffers[identity]=data
        return output

    async def _terminal_ready(self, tab, seconds):
        deadline=asyncio.get_running_loop().time()+seconds
        while tab['state']!='ready':
            event=self._event
            job=self.jobs.get(tab.get('job_id'))
            if job and job['status'] in FINISHED:raise ValueError(job.get('error') or 'Оболочка завершилась.')
            remaining=deadline-asyncio.get_running_loop().time()
            if remaining<=0:return False
            try:await asyncio.wait_for(event.wait(),remaining)
            except asyncio.TimeoutError:return False
        return True

    async def terminal_execute(self, command, context, *, identity=None, new_session=False, title='', timeout=None, yield_seconds=1, owner='agent', profile_id=None):
        if not isinstance(command,str) or not command.strip() or '\x00' in command or len(command)>32768:
            raise ValueError('Введите команду длиной до 32768 символов, без нулевых символов.')
        if timeout is not None:
            timeout=int(timeout)
            if not 1<=timeout<=86400:raise ValueError('Тайм-аут: от 1 до 86400 секунд.')
        if identity and owner == 'agent' and not profile_id:
            profile_id = self.terminal_get(identity, context)['profile_id']
        profile=self.store.get(profile_id) if profile_id else self.route(context)
        if profile['id']=='shared':await self.prepare_shared_workspace(starting=True)
        if owner=='user':self.check_manual_input()
        access=context if owner=='agent' else None
        if identity:tab=self.terminal_get(identity,access,profile['id'])
        else:
            candidates=[] if new_session else self.terminal_list(profile['id'],context if owner=='agent' else None)
            candidates=[t for t in candidates if t['owner']==owner and t['state'] in {'ready','stopped'}
                        and not (self.terminal_locks.get(t['id']) and self.terminal_locks[t['id']].locked())]
            selected=next(iter(candidates),None)
            if selected is None:selected=self.terminal_open(profile,context,owner=owner,title=title)
            tab=self.terminal_get(selected['id'],access,profile['id'])
        async with self.terminal_locks.setdefault(tab['id'],asyncio.Lock()):
            if owner=='user':self.check_manual_input()
            if tab.get('closed'):raise ValueError('Терминальная сессия закрыта.')
            if not self.store.get(tab['profile_id'])['active']:raise ValueError('Терминал отключён.')
            await self._terminal_start(tab,context)
            if tab['state']=='starting' and not await self._terminal_ready(tab,30):
                return {'terminal_session_id':tab['id'],'status':'starting','command_sent':False,
                        'error':'Оболочка ещё запускается; команда не отправлена. Проверьте состояние этой вкладки.',
                        'terminal':self.terminal_public(tab)}
            if tab['state']!='ready':
                raise ValueError('Вкладка занята программой. Используйте terminal_write_input или откройте новую сессию.')
            if owner=='user':self.check_manual_input()
            job=self.jobs[tab['job_id']]
            check=dict(job,id=uuid.uuid4().hex,command=command,source='agent' if owner=='agent' else 'manual',hidden=True,finished=None)
            approval=asyncio.create_task(self.policy.check(profile,check,context))
            try:
                await asyncio.wait({approval,self.tasks[job['id']]},return_when=asyncio.FIRST_COMPLETED)
                if not approval.done():raise ValueError('Оболочка завершилась во время проверки.')
                await approval
            finally:
                if not approval.done():approval.cancel()
                await asyncio.gather(approval,return_exceptions=True)
                for suffix in ('.json','.out'):(self.store.root/'jobs'/(check['id']+suffix)).unlink(missing_ok=True)
            if tab.get('closed') or job['status'] in FINISHED:raise ValueError('Оболочка завершилась во время проверки.')
            if owner=='user':self.check_manual_input()
            invocation={'id':uuid.uuid4().hex,'terminal_session_id':tab['id'],'root_job_id':job['id'],
                        'command':command,'status':'running','offset':job['bytes'],'exit_code':None,'created':time.time(),
                        'command_sent':False}
            self.terminal_commands[invocation['id']]=invocation
            from .file_history import begin_command, finish_command, wait_command
            try:
                await begin_command(self,invocation,context,owner)
            except BaseException:
                invocation.update(status='cancelled',finished=time.time())
                raise
            if len(self.terminal_commands)>1000:
                for old,entry in list(self.terminal_commands.items()):
                    if entry['status']!='running':self.terminal_commands.pop(old)
                    if len(self.terminal_commands)<=1000:break
            try:program=shlex.split(command)[0]
            except ValueError:program='shell'
            tab.update(state='busy',command_id=invocation['id'],program=program)
            if title:tab['title']=str(title)[:100]
            self._save_terminals()
            sender='toshy' if owner=='agent' else 'user'
            target=profile.get('ssh_host') if profile['backend']=='ssh' else profile['backend']
            self._append(job,f'{sender}@{target}:{tab["cwd"]}$ ')
            process=self.processes.get(job['id'])
            try:
                if process is None:raise ValueError('Оболочка завершилась.')
                process.stdin.write((command+'\r').encode('utf-8'))
                invocation['command_sent'] = True
                await process.stdin.drain()
            except Exception:
                invocation.update(status='failed',finished=time.time())
                finish_command(self,invocation)
                await wait_command(self,invocation)
                raise
        if timeout is not None:
            async def expire():
                await asyncio.sleep(timeout)
                if invocation['status']=='running' and tab.get('command_id')==invocation['id']:
                    invocation.update(status='timed_out')
                    await self.write_input(tab['job_id'],key='ctrl+c',source='agent',context=context)
            task=asyncio.create_task(expire());self.terminal_events[invocation['id']]=task
        await self._terminal_ready(tab,min(30,max(0,float(yield_seconds))))
        await wait_command(self,invocation)
        return self.terminal_command_result(invocation)

    def terminal_command_result(self, invocation):
        job=self.jobs[invocation['root_job_id']]
        page=self.output(job['id'],invocation['offset'])
        if invocation.get('end_offset') is not None:
            page['text']=page['text'].encode('utf-8')[:max(0,invocation['end_offset']-invocation['offset'])].decode('utf-8','ignore')
            page['offset']=invocation['offset']+len(page['text'].encode('utf-8'))
        return {'job_id':invocation['id'],'terminal_session_id':invocation['terminal_session_id'],
                'command_sent':bool(invocation.get('command_sent', False)),
                'status':invocation['status'],'exit_code':invocation['exit_code'],'output':page['text'],
                'offset':page['offset'],'output_truncated':page['offset']<invocation.get('end_offset',job['bytes']),
                'screen':page.get('screen'),'terminal':self.terminal_public(self.terminal_sessions[invocation['terminal_session_id']])}

    async def terminal_input(self, identity, text='', *, key=None, context=None, profile_id=None, wait_seconds=0, close_stdin=False):
        tab=self.terminal_get(identity,context,profile_id)
        if context is None:
            self.check_manual_input()
            if text or key!='ctrl+c' or close_stdin:
                raise ValueError('Во время работы программы ручной ввод отключён. Доступна остановка Ctrl+C.')
        identity=tab['id']
        if tab['state']=='stopped':raise ValueError('Оболочка остановлена. Выполните команду, чтобы запустить её.')
        if tab['state']=='ready' and text:
            raise ValueError('Вкладка ожидает shell-команду. Используйте terminal_execute.')
        lock=self.terminal_locks.get(identity)
        if lock and lock.locked():raise ValueError('Дождитесь проверки или отправки команды в этой вкладке.')
        result=await self.write_input(tab['job_id'],text,key=key,close_stdin=close_stdin,
                                      source='agent' if context else 'manual',context=context,wait_seconds=wait_seconds)
        result['terminal_session_id']=identity;result['terminal']=self.terminal_public(tab)
        result['input_sent']=True
        return result

    async def terminal_read(self, identity, *, context=None, offset=0, wait_seconds=0, view='output'):
        invocation=self.terminal_commands.get(identity)
        terminal_id=invocation['terminal_session_id'] if invocation else identity
        tab=self.terminal_get(terminal_id,context)
        root=invocation['root_job_id'] if invocation else tab.get('job_id')
        if root not in self.jobs:return {'terminal':self.terminal_public(tab),'text':'','offset':0,'size':0}
        if invocation:
            offset=max(int(offset),invocation['offset'])
            if invocation['status']!='running':wait_seconds=0
        result=await self.wait_output(root,offset,wait_seconds)
        if invocation:
            from .file_history import wait_command
            await wait_command(self,invocation)
        if invocation and invocation.get('end_offset') is not None:
            result['text']=result['text'].encode('utf-8')[:max(0,invocation['end_offset']-offset)].decode('utf-8','ignore')
            result['offset']=offset+len(result['text'].encode('utf-8'));result['size']=invocation['end_offset']
        result['terminal_session_id']=tab['id'];result['terminal']=self.terminal_public(tab)
        if invocation:result['command']=dict(invocation)
        if view=='screen':result.pop('text',None)
        return result

    def terminal_feed(self, profile_id, identity=None, cursors=None):
        result={'tabs':self.terminal_list(profile_id),'revision':self.revision,'epoch':self.app_session,'records':[],
                'agent_busy':any(self.reservations.values()),
                'pending':[{k:v for k,v in entry.items() if k!='future'} for entry in self.policy.pending.values()]}
        if not identity:return result
        try:tab=self.terminal_get(identity,profile_id=profile_id)
        except ValueError:return result
        ids=[root for root in tab.get('job_ids',[]) if root in self.jobs]
        sizes={root:(self.store.root/'jobs'/(root+'.out')).stat().st_size if (self.store.root/'jobs'/(root+'.out')).exists() else 0 for root in ids}
        reset=cursors is not None and any(root not in ids for root in cursors)
        if cursors is None or reset:
            offsets={};remaining=2*1024*1024
            for root in reversed(ids):
                keep=min(remaining,sizes[root]);offsets[root]=sizes[root]-keep;remaining-=keep
        else:offsets={root:max(0,min(int(cursors.get(root,0)),sizes[root])) for root in ids}
        budget=262144
        for root in ids:
            while offsets[root]<sizes[root] and budget>0:
                page=self.output(root,offsets[root],min(budget,131072));page.pop('screen',None)
                if page['offset']<=offsets[root]:break
                result['records'].append(page);budget-=len(page['text'].encode('utf-8'));offsets[root]=page['offset']
            if budget<=0:break
        result.update(terminal=self.terminal_public(tab),cursors=offsets,reset=reset,
                      backlog=any(offsets[root]<sizes[root] for root in ids))
        return result
