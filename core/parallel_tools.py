"""Compatibility for explicit parallel envelopes, using the normal dispatcher."""
import asyncio
import json

from jsonschema import Draft202012Validator


async def execute_parallel(registry, arguments, context, tools):
    if not isinstance(arguments, dict) or set(arguments) != {'tool_uses'}:
        raise ValueError('Parallel calls require a tool_uses array; no calls were executed.')
    uses = arguments['tool_uses']
    if not isinstance(uses, list) or not 1 <= len(uses) <= 64:
        raise ValueError('tool_uses must contain between 1 and 64 calls; no calls were executed.')
    calls = []
    # Validate the entire batch before starting even one operation.
    for item in uses:
        if not isinstance(item, dict) or set(item) != {'recipient_name', 'parameters'}:
            raise ValueError('Every parallel call requires recipient_name and parameters; no calls were executed.')
        name, parameters = item['recipient_name'], item['parameters']
        if not isinstance(name, str) or not isinstance(parameters, dict):
            raise ValueError('recipient_name must be a string and parameters an object; no calls were executed.')
        resolved = name
        if resolved not in tools and resolved.startswith(('toshy.', 'functions.')):
            resolved = resolved.split('.', 1)[1]
        tool = tools.get(resolved)
        if tool is None or not tool.model_visible:
            raise ValueError(f'Unavailable parallel tool: {name}; no calls were executed.')
        schema = dict(tool.args_schema)
        if schema.get('type') == 'object' and 'properties' in schema:
            schema.setdefault('additionalProperties', False)
        errors = list(Draft202012Validator(schema).iter_errors(parameters))
        if errors:
            raise ValueError(f'Invalid arguments for {name}: {errors[0].message}; no calls were executed.')
        calls.append((name, resolved, parameters, tool))

    async def run(call):
        recipient, name, parameters, _ = call
        output = await registry.execute_tool(name, parameters, context=context, tools=tools)
        return {'recipient_name': recipient, 'output': str(output)}

    results = []
    index = 0
    while index < len(calls):
        # Read-only tools may overlap. Other tools retain ordering unless their
        # module explicitly opts into parallel dispatch, just like normal calls.
        def concurrent(call):
            tool = call[3]
            return tool.history_read_only or bool(tool.parallel_group)

        if not concurrent(calls[index]):
            results.append(await run(calls[index]))
            index += 1
            continue
        end = index
        while end < len(calls) and concurrent(calls[end]) and end - index < 8:
            end += 1
        tasks = [asyncio.create_task(run(call)) for call in calls[index:end]]
        try:
            results.extend(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        index = end
    return json.dumps({'tool_results': results}, ensure_ascii=False)
