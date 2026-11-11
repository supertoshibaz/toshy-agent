"""The only HTTP/SDK integration of the core pseudo-module."""
from contextlib import asynccontextmanager
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from core.security import token_matches


class McpEndpoint:
    def __init__(self, facade):
        self.facade = facade
        self.manager = None
        self._task = None
        self._ready = None
        self._stop = None
        import asyncio
        self._lock = asyncio.Lock()

    def _create_manager(self):
        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings
        server = Server('toshy-mcp')
        @server.list_tools()
        async def list_tools():
            return [types.Tool.model_validate(spec) for spec in self.facade.tool_specs()]
        @server.call_tool()
        async def call_tool(name, arguments):
            context = server.request_context
            meta = context.meta.model_dump(by_alias=True) if context.meta is not None else {}
            key = str(meta.get('toshy/requestKey') or '')
            if len(key) > 256:
                raise ValueError('Ключ запроса слишком длинный')
            return await self.facade.call(name, arguments, request_key=key)
        return StreamableHTTPSessionManager(server, stateless=True, json_response=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                allowed_hosts=['127.0.0.1', '127.0.0.1:*', 'localhost', 'localhost:*', '[::1]', '[::1]:*'],
                allowed_origins=['http://127.0.0.1:*', 'http://localhost:*']))

    async def _serve(self):
        try:
            manager = self._create_manager()
            async with manager.run():
                self.manager = manager
                self.facade.transport_error = ''
                if not self._ready.done():
                    self._ready.set_result(True)
                await self._stop.wait()
        except Exception as error:
            import traceback
            from core.logger import core_logger
            self.facade.transport_error = 'Не удалось запустить Toshy MCP: ' + str(error)
            core_logger.event('app', 'TOSHY_MCP_START_ERROR', self.facade.transport_error,
                level='ERROR', details={'traceback': traceback.format_exc()})
        finally:
            self.manager = None
            if not self._ready.done():
                self._ready.set_result(False)

    async def start(self):
        import asyncio
        async with self._lock:
            if self.manager is not None:
                return True
            if self._task is None or self._task.done():
                self._ready = asyncio.get_running_loop().create_future()
                self._stop = asyncio.Event()
                self._task = asyncio.create_task(self._serve())
            try:
                return await asyncio.wait_for(asyncio.shield(self._ready), timeout=15)
            except asyncio.TimeoutError:
                self.facade.transport_error = 'Превышено время запуска Toshy MCP. Ядро продолжает работу.'
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                return False

    async def stop(self):
        import asyncio
        if self._task is not None and not self._task.done():
            self._stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self.manager = None

    @asynccontextmanager
    async def lifespan(self):
        # The optional SDK runs in its own task/cancel scope. A missing or
        # incompatible MCP installation must never abort the core lifespan.
        try:
            if self.facade.settings['enabled']:
                await self.start()
            yield
        finally:
            await self.facade.shutdown()
            await self.stop()

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', []))
        authorization = headers.get(b'authorization', b'').decode('utf-8', errors='replace')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if not self.facade.settings['enabled']:
            return await JSONResponse({'detail': 'Toshy MCP выключен'}, status_code=403)(scope, receive, send)
        if not token_matches(token, self.facade.token):
            return await JSONResponse({'detail': 'Unauthorized MCP client'}, status_code=401)(scope, receive, send)
        if self.manager is None:
            return await JSONResponse({'detail': self.facade.transport_error or 'MCP не запущен'}, status_code=503)(scope, receive, send)
        await self.manager.handle_request(scope, receive, send)


def install(app, facade):
    endpoint = McpEndpoint(facade)
    app.mount('/mcp', endpoint)

    @app.get('/v1/toshy-mcp')
    async def configuration():
        return facade.configuration()

    @app.put('/v1/toshy-mcp')
    async def configure(request: Request):
        try:
            facade.configure(await request.json())
            if facade.settings['enabled']:
                await endpoint.start()
            else:
                await endpoint.stop()
            return facade.configuration()
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get('/v1/toshy-mcp/runs')
    async def runs():
        return {'runs': [facade.get_run(r['run_id']) for r in sorted(facade.records.values(),
            key=lambda r: r['created_at'], reverse=True)[:100]]}

    @app.post('/v1/toshy-mcp/runs/{run_id}/cancel')
    async def cancel(run_id: str):
        try:
            return await facade.cancel(run_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    return endpoint
