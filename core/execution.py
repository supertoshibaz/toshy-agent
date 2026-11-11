"""UI-independent entry points for external runs of the existing agent/runner."""
from langchain_core.messages import HumanMessage
from core.context import bind_execution_context
from contextlib import aclosing


class CoreExecution:
    def __init__(self, agent):
        self.agent = agent

    async def pipeline(self, reference, inputs, context, emit, definition, nested):
        agent = self.agent
        module, pid = reference.split('/', 1)
        async with agent.registry.execution_snapshot(context.allowed_modules, context=context) as (tools, _, _):
            connection = agent.client.model_connection(context.model_profile_id)
            with bind_execution_context(context), agent.client.bind_connection(connection):
                with agent.registry.dispatch_scope(context, tools, [], emit), agent.pipeline_service.frame([], context, tools, emit):
                    return await agent.pipeline_service.run(module, pid, inputs,
                        definition_override=definition, resolve_override=lambda ref: nested[ref])

    async def task(self, task, context, emit):
        messages = [HumanMessage(content=task)]
        from core.request_clock import stamp_latest_user
        stamp_latest_user(messages)
        async with aclosing(self.agent.run_events(messages, context)) as events:
            async for event in events:
                if event.get('type') == 'done':
                    final = event['state']['messages']
                    if final and getattr(final[-1], 'tool_calls', None):
                        raise RuntimeError('Агент достиг лимита шагов до завершения задачи')
                    return final
                await emit(event)
        raise RuntimeError('Агент завершился без результата')
