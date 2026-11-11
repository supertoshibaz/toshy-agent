"""Plugin authentication/catalogs for existing transports; credentials stay out of profiles."""
import asyncio


def register(context, identity, provider):
    if not identity.startswith(context.plugin_id + '.'):
        raise ValueError('Provider IDs must belong to their plugin namespace.')
    registry = getattr(context.runtime, 'model_providers', None)
    if registry is None:
        context.runtime.model_providers = registry = {}
    if identity in registry:
        raise ValueError('Model provider is already registered: ' + identity)
    for method in ('configure', 'authenticate', 'list_models'):
        if not callable(getattr(provider, method, None)):
            raise TypeError('Model provider must implement ' + method)
    registry[identity] = provider
    context.register_cleanup(lambda: registry.pop(identity, None))
    return provider


def providers():
    from core.plugins.runtime import get_runtime
    runtime = get_runtime()
    return dict(getattr(runtime, 'model_providers', {})) if runtime else {}


def provider_for(record):
    identity = record.get('provider')
    if not identity:
        return None
    provider = providers().get(identity)
    if provider is None:
        raise ValueError('Model provider is unavailable. Enable its plugin and restart Toshy: ' + str(identity))
    return provider


async def authenticate(connection):
    provider = provider_for(connection.options)
    return await asyncio.to_thread(provider.authenticate, connection) if provider else connection


async def list_models(connection):
    provider = provider_for(connection.options)
    return await asyncio.to_thread(provider.list_models, connection) if provider else None


def observe(connection, method, *args):
    """Optional provider-owned telemetry; it must never interrupt inference."""
    try:
        provider = provider_for(connection.options)
        callback = getattr(provider, method, None) if provider else None
        if callback is not None:
            callback(connection, *args)
    except Exception:
        pass


def retry_request(connection, payload, status, detail):
    """Allow a provider to remove an explicitly rejected optional field."""
    provider = provider_for(connection.options)
    callback = getattr(provider, 'retry_request', None) if provider else None
    return bool(callback(connection, payload, status, detail)) if callback else False
