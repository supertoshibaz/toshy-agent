"""One plain user message, one model pass, no agent graph or request addons."""
import asyncio
from contextlib import aclosing

from langchain_core.messages import HumanMessage,AIMessage,message_to_dict
from core.context import bind_execution_context
from core.reasoning_control import reasoning_scope, finalizable_stream, FinalizationRequested


def context_preview(session_id=None,branch='main'):
    """Describe the raw path without loading ordinary agent context or a core."""
    return {'session_id':session_id or None,'branch':branch or 'main','raw_request':True,
            'base_system_prompt':'','core_tools':[],'module_sections':[],'hidden_history':[],'tool_count':0,
            'markdown':'# Чистый запрос\n\nСистемный промпт пуст.\n\n```text\n\n```\n\n'
                       'Дополнительный контекст отсутствует: история, память, дата и время, '
                       'описания инструментов и служебные инструкции не отправляются.\n\n'
                       'Модель получает только текст текущего сообщения пользователя.\n'}


async def raw_events(client,history,context,*,journal=None):
    # Construct afresh: even IDs, clock, replies and explicit file metadata must
    # stay on the saved chat card rather than becoming provider input.
    latest=next((m for m in reversed(history) if isinstance(m,HumanMessage)),None)
    if latest is None or not isinstance(latest.content,str):
        raise ValueError('Для чистого запроса нужен текст пользователя.')
    prompt=HumanMessage(content=latest.content)
    connection=await asyncio.to_thread(client.model_connection,context.model_profile_id) if context.model_profile_id else await asyncio.to_thread(client.model_connection)
    if journal is not None:
        journal.data['model_fingerprint']=connection.fingerprint
        journal.save(force=True)
    def record(event):
        if journal is not None:journal.consume(event)
    start={'type':'_model_start','messages':[message_to_dict(prompt)],'steps_count':0}
    record(start)
    content=[];reasoning=[]
    # Reuse only the display parser; it cannot issue tools or follow-up calls.
    from core.graph import _ThinkStreamFilter
    display=_ThinkStreamFilter()
    with bind_execution_context(context),reasoning_scope(context,lambda:record({'type':'run_finalizing'})) as control,client.bind_connection(connection):
        final_pass=False
        while True:
            source=client.chat_completion_stream([prompt],tools=None,raw_request=True,control_reasoning=not final_pass)
            try:
                async with aclosing(source if final_pass else finalizable_stream(source,context)) as stream:
                    async for delta in stream:
                        if '_reasoning_control' in delta:
                            yield {'type':'reasoning_control',**delta['_reasoning_control']};continue
                        record({'type':'_model_delta','delta':delta})
                        thought=delta.get('reasoning_content') or delta.get('reasoning') or ''
                        events=[]
                        if thought:
                            events.extend(display.flush());display.in_thought=False
                            events.append({'type':'thought','delta':thought})
                        if delta.get('content'):events.extend(display.feed(delta['content']))
                        for event in events:
                            (reasoning if event['type']=='thought' else content).append(event['delta'])
                            record(event);yield event
            except FinalizationRequested:pass
            for event in display.flush():
                (reasoning if event['type']=='thought' else content).append(event['delta'])
                record(event);yield event
            if control.finalizing and not final_pass:
                reasoning.extend(content);content=[];display=_ThinkStreamFilter()
                working=AIMessage(content='<think>'+''.join(reasoning)+'</think>')
                record({'type':'_model_complete','messages':[message_to_dict(prompt),message_to_dict(working)]})
                record({'type':'content_interim'});yield {'type':'content_interim'}
                control.final_answer_started=True;final_pass=True
                record(start)
                continue
            break
    text=''.join(content);notes=''.join(reasoning)
    if final_pass and not text.strip():raise RuntimeError('Модель не выдала итоговый текстовый ответ.')
    answer=AIMessage(content=('<think>'+notes+'</think>\n' if notes else '')+text)
    record({'type':'_model_complete','messages':[message_to_dict(prompt),message_to_dict(answer)]})
    yield {'type':'done','state':{'messages':[*history,answer],'model_name':connection.model}}
