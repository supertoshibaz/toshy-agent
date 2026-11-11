"""Authenticated UI-to-core calls for explicitly registered plugin services."""
import inspect
import asyncio


def register(context, name, handler):
    if not name.startswith(context.plugin_id + '.') or not callable(handler):
        raise ValueError('A plugin service must be callable and own its namespace.')
    registry = getattr(context.runtime, 'services', None)
    if registry is None:
        context.runtime.services = registry = {}
    if name in registry:
        raise ValueError('Plugin service is already registered: ' + name)
    registry[name] = (context.component_id, handler)
    context.register_cleanup(lambda: registry.pop(name, None))


async def call(runtime, name, payload):
    registration = getattr(runtime, 'services', {}).get(name)
    if not registration or not runtime.selected(registration[0]):
        raise ValueError('Plugin service is unavailable. Enable its plugin and restart the kernel.')
    handler = registration[1]
    if inspect.iscoroutinefunction(handler):
        return await handler(payload)
    result = await asyncio.to_thread(handler, payload)
    return await result if inspect.isawaitable(result) else result
