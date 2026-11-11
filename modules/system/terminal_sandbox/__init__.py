"""Agent-first Docker terminals. UI and lifecycle stay inside this module."""
import asyncio
import json
from pathlib import Path
from contextlib import asynccontextmanager
from core.context import get_execution_context
from core.registry import BaseModule, BaseTool
from .runtime import TerminalManager
from .host_process import shell


class TerminalTool(BaseTool):
    # Границы команды живут дольше вызова execute/read_output.
    history_managed = True
    def __init__(self, manager, name, description, properties, required=()):
        super().__init__(name, description, {'type': 'object', 'properties': properties, 'required': list(required)})
        self.manager = manager

    def job(self, identity):
        job = self.manager.jobs.get(identity)
        context = get_execution_context()
        profile = self.manager.effective_profile(self.manager.route(context), context)
        profiles = {profile['id']} | self.manager.resource_profiles.get(context.run_id, set())
        if (not job or job['profile_id'] not in profiles or job.get('session_id') != (context.session_id or '')
                or (not context.session_id and job.get('run_id') != context.run_id)):
            raise ValueError('Задание не принадлежит терминалу и сессии этого запуска.')
        return job


class ExecuteCommandTool(TerminalTool):
    def __init__(self, manager):
        super().__init__(manager,'terminal_execute',
            'Run a shell command in a persistent terminal tab. Returns terminal_session_id and job_id. '
            'Variables and cwd persist in that tab. Use its terminal_session_id explicitly for follow-up commands. '
            'An explicitly selected busy tab rejects shell commands: send application input with terminal_write_input instead. '
            'Without an ID, reuse your idle tab or open a new one. new_session=true opens an independent tab. '
            'Do not create a new terminal session for every command or user request. Reuse the same '
            'terminal_session_id for related sequential commands, including across turns in this chat. '
            'Only create another session when independent parallel work or a separate shell is necessary. '
            'Long commands yield while the process continues; read new output using its ID and offset. '
            'The user-selected UI tab never changes routing. Commands pass configured safety checks.',
            {'command':{'type':'string'},'terminal_session_id':{'type':'string'},'new_session':{'type':'boolean'},
             'title':{'type':'string'},'timeout':{'type':'integer','minimum':1,'maximum':86400},
             'yield_seconds':{'type':'number','minimum':0,'maximum':30}},['command'])

    async def execute(self, command, terminal_session_id=None, new_session=False, title='', timeout=None, yield_seconds=1, **kwargs):
        try:
            result=await self.manager.terminal_execute(command,get_execution_context(),identity=terminal_session_id,
                new_session=new_session,title=title,timeout=timeout,yield_seconds=yield_seconds)
            return json.dumps(result,ensure_ascii=False)
        except Exception as error:return json.dumps({'error':str(error)},ensure_ascii=False)


class ReadOutputTool(TerminalTool):
    def __init__(self, manager):
        super().__init__(manager,'terminal_read_output',
            'Read a terminal session or command job by ID. Pass the last offset for new output only; '
            'view=screen gives the current interactive screen. wait_seconds waits for output/state changes.',
            {'terminal_session_id':{'type':'string'},'job_id':{'type':'string'},'offset':{'type':'integer','minimum':0},
             'wait_seconds':{'type':'number','minimum':0,'maximum':30},'view':{'type':'string','enum':['output','screen']}})

    async def execute(self, terminal_session_id=None, job_id=None, offset=0, wait_seconds=0, view='output', **kwargs):
        try:
            return json.dumps(await self.manager.terminal_read(terminal_session_id or job_id,context=get_execution_context(),
                offset=offset,wait_seconds=wait_seconds,view=view),ensure_ascii=False)
        except Exception as error:return json.dumps({'error':str(error)},ensure_ascii=False)


class WriteInputTool(TerminalTool):
    def __init__(self, manager):
        from .interactive import KEYS
        super().__init__(manager,'terminal_write_input',
            'Send exact text or a key to the application occupying a terminal tab. Use key=enter to submit a line. '
            'Example: text="print(value)", key="enter" sends AND submits that line in one call; do not send a second Enter. '
            'A successful input_sent result only confirms delivery. Verify actual output with terminal_read_output before '
            'claiming a value was printed or an operation succeeded; echoed input is not execution evidence. '
            'For a ready shell use terminal_execute instead. Never infer the target from the active UI tab. '
            'Input obeys host safety checks. Do not transmit passwords through model context.',
            {'terminal_session_id':{'type':'string'},'job_id':{'type':'string'},'text':{'type':'string','maxLength':32768},
             'key':{'type':'string','enum':list(KEYS)},'close_stdin':{'type':'boolean'},
             'wait_seconds':{'type':'number','minimum':0,'maximum':30}})

    async def execute(self, terminal_session_id=None, job_id=None, text='', key=None, close_stdin=False, wait_seconds=1, **kwargs):
        try:
            identity=terminal_session_id or job_id
            command=self.manager.terminal_commands.get(identity)
            if command:identity=command['terminal_session_id']
            return json.dumps(await self.manager.terminal_input(identity,text,key=key,close_stdin=close_stdin,
                context=get_execution_context(),wait_seconds=wait_seconds),ensure_ascii=False)
        except Exception as error:return json.dumps({'error':str(error)},ensure_ascii=False)


class ListProcessesTool(TerminalTool):
    def __init__(self, manager):
        super().__init__(manager,'terminal_list_processes',
            'List your terminal tabs: stable ID, environment, owning chat, cwd, program, ready/busy/stopped state. '
            'Tabs from other chats and user-owned tabs are not available to this agent.',{})

    async def execute(self, **kwargs):
        context=get_execution_context()
        return json.dumps({'sessions':self.manager.terminal_list(self.manager.route(context)['id'],context)},ensure_ascii=False)


class SessionTool(TerminalTool):
    def __init__(self, manager):
        super().__init__(manager,'terminal_session',
            'Open an independent persistent terminal tab, or close your tab and all its processes. '
            'Opening is lazy: no container/process starts until the first command. Closed IDs are not reused.',
            {'action':{'type':'string','enum':['open','close']},'terminal_session_id':{'type':'string'},'title':{'type':'string'}},['action'])

    async def execute(self, action, terminal_session_id=None, title='', **kwargs):
        try:
            context=get_execution_context()
            if action=='open':result=self.manager.terminal_open(self.manager.route(context),context,title=title)
            elif action=='close':result=await self.manager.terminal_close(terminal_session_id,context)
            else:raise ValueError('Неизвестное действие.')
            return json.dumps(result,ensure_ascii=False)
        except Exception as error:return json.dumps({'error':str(error)},ensure_ascii=False)


class ResizeProcessTool(TerminalTool):
    def __init__(self, manager):
        super().__init__(manager,'terminal_resize','Resize a terminal tab in character cells.',
            {'terminal_session_id':{'type':'string'},'columns':{'type':'integer','minimum':20,'maximum':240},
             'rows':{'type':'integer','minimum':5,'maximum':100}},['terminal_session_id','columns','rows'])

    async def execute(self, terminal_session_id, columns, rows, **kwargs):
        try:
            tab=self.manager.terminal_get(terminal_session_id,get_execution_context())
            return json.dumps(await self.manager.resize_process(tab['job_id'],columns,rows),ensure_ascii=False)
        except Exception as error:return json.dumps({'error':str(error)},ensure_ascii=False)


class KillProcessTool(TerminalTool):
    def __init__(self, manager):
        super().__init__(manager,'terminal_kill_process','Close a terminal session and terminate all its processes. Accepts a command job_id as well.',
                         {'terminal_session_id':{'type':'string'},'job_id':{'type':'string'}})

    async def execute(self, terminal_session_id=None, job_id=None, **kwargs):
        try:
            identity=terminal_session_id or job_id
            command=self.manager.terminal_commands.get(identity)
            if command:identity=command['terminal_session_id']
            return json.dumps(await self.manager.terminal_close(identity,get_execution_context()),ensure_ascii=False)
        except Exception as error:return json.dumps({'error':str(error)},ensure_ascii=False)


class ResetSandboxTool(TerminalTool):
    def __init__(self, manager):
        super().__init__(manager, 'terminal_reset',
            'Stop this terminal without erasing packages/files. Next command starts it. Cancel its jobs first; other agent runs must finish. '
            'Destructive recreation is available only in user settings.', {})

    async def execute(self, **kwargs):
        try:
            context = get_execution_context()
            identity = self.manager.route(context)['id']
            if self.manager.reservations.get(identity, set()) - {context.run_id}:
                raise ValueError('Контейнер используется другими запусками агента.')
            from .runtime import TERMINAL_STATES
            if any(j['profile_id'] == identity and j['status'] not in TERMINAL_STATES for j in self.manager.jobs.values()):
                raise ValueError('Сначала отмените задания терминала.')
            name = self.manager._name(identity)
            profile = self.manager.route(context)
            if profile['backend'] != 'docker':
                connection = self.manager.ssh_connections.pop(identity, None)
                if connection:
                    connection.close(); await connection.wait_closed()
                return 'Задания завершены; SSH-подключение закрыто. Файлы сохранены.'
            self.manager.maintenance.add(identity)
            try:
                instance = self.manager._instances().get(identity)
                if instance and await self.manager.inspect(name):
                    await self.manager.cli('stop', '-t', '1', name)
                    self.manager._set_instance(identity, dict(instance, running=False))
            finally:
                self.manager.maintenance.discard(identity)
            return 'Терминал остановлен. Файлы сохранены. Следующая команда запустит его снова.'
        except Exception as error:
            return json.dumps({'error': str(error)}, ensure_ascii=False)


class Module(BaseModule):
    def __init__(self):
        self.manager = TerminalManager()
        self.tools = [ExecuteCommandTool(self.manager), ReadOutputTool(self.manager), WriteInputTool(self.manager),
                      ListProcessesTool(self.manager), SessionTool(self.manager), ResizeProcessTool(self.manager), KillProcessTool(self.manager), ResetSandboxTool(self.manager)]

    def get_tools(self):
        return self.tools

    async def get_model_context(self, context, messages, tools, *, preview=False):
        if not any(name.startswith('terminal_') for name in tools):return []
        from .attachments import prepare_attachment
        from langchain_core.messages import HumanMessage
        from modules.system.file_manager.documents import DOCUMENT_SUFFIXES
        import json
        files = {}
        for message in messages:
            if isinstance(message, HumanMessage):
                for item in message.additional_kwargs.get('attachments') or []:
                    document = Path(str(item.get('filename') or '')).suffix.lower() in DOCUMENT_SUFFIXES
                    if (item.get('media_type') == 'file' or (item.get('media_type') == 'text' and document)) and not (item.get('memory_only') or item.get('temp_memory')):
                        files[item.get('id')]=item
        if not files:return []
        if preview:return ['Uploaded files will be mapped to the selected Terminal environment.']
        rows=[]
        for item in files.values():
            try:
                path=await prepare_attachment(self.manager,item,context)
                rows.append({'filename':item['filename'],'project_path':item.get('workspace_path'),
                             'document_path':'attachment://'+str(item.get('id','')), 'terminal_path':path})
            except Exception as error:
                rows.append({'filename':item.get('filename'),'error':str(error)})
        return ['User-uploaded file mapping for this terminal. Use terminal_path ONLY in shell commands. '
                'Use document_path with inspect_document/read_document/search_document/read_spreadsheet/read_file. '
                'Do not assume a host path exists inside Docker or SSH.\n'+json.dumps(rows,ensure_ascii=False)]

    @asynccontextmanager
    async def execution_context_scope(self, context, tools):
        if any(name.startswith('terminal_') for name in tools):
            async with self.manager.execution_scope(context):
                yield
        else:
            yield

    def get_system_prompt_addon(self):
        return ('Terminal tabs have stable terminal_session_id values independent of the UI selection. '
                'Use terminal_execute for ready shells and terminal_write_input for running applications. '
                'Each tab keeps cwd and shell/REPL state; use a new tab for parallel work. '
                'Use returned IDs and offsets, do not repeatedly poll empty output or restart live programs. '
                'Only your chat-owned tabs are accessible. Respect environment assignments, disabled terminals and refusals.')

    def get_request_messages(self, context, tools):
        if 'terminal_execute' not in tools:return []
        profile=self.manager.effective_profile(self.manager.route(context),context)
        details={'name':profile['name'],'active':profile['active'],
            'backend':profile['backend'],'shell':shell(profile)[0],
            'os':profile['target_os'] if profile['backend']=='ssh' else ('linux' if profile['backend']=='docker' else __import__('sys').platform),
            'initial_cwd':self.manager.current_directory(profile,context),
            'sessions':self.manager.terminal_list(profile['id'],context)}
        if profile['id']=='shared' and profile['backend']=='docker':
            verified=(self.manager.store.root/'shared-workspace.json').exists() or not self.manager._instances().get('shared')
            details.update(mounted_folders=profile['mounts'] if verified else None,shared_environment=True,
                workspace_note='This shared environment keeps its own mounted folders across chats and projects. The selected project is not automatically mounted. Use the listed host-to-container paths; if a project is outside them, use file tools or ask the user to connect that folder. Do not treat /workspace as the selected project.')
            if not verified:
                details['workspace_note']+=' The saved container is stopped; its actual folders will be checked on first terminal use. Read mounted_folders in the terminal response before operating on project files.'
        return ['Assigned terminal: '+json.dumps(details,ensure_ascii=False)]

    async def handle_request(self, request, path='', *, session_manager=None):
        from .api import handle_request
        if session_manager is not None:
            self.manager.sessions = session_manager
        return await handle_request(self.manager, request, path)

    async def deactivate(self):
        await self.manager.shutdown()

    async def shutdown(self):
        await self.manager.shutdown()
