"""Loopback OAuth handoff. Tokens use the same owner-specific OS vault as API keys."""
import asyncio
import json
from urllib.parse import urlsplit, parse_qs
from .store import get_secret, set_secret

CALLBACK = 'http://127.0.0.1:5000/v1/modules/mcp_client/service/oauth/callback'


class OAuthStorage:
    def __init__(self, owner, identity):
        self.owner, self.identity = owner, identity

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        value = get_secret(self.owner, self.identity, 'oauth:tokens')
        return OAuthToken.model_validate_json(value) if value else None

    async def set_tokens(self, tokens):
        set_secret(self.owner, self.identity, 'oauth:tokens', tokens.model_dump_json())

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        value = get_secret(self.owner, self.identity, 'oauth:client')
        return OAuthClientInformationFull.model_validate_json(value) if value else None

    async def set_client_info(self, client_info):
        set_secret(self.owner, self.identity, 'oauth:client', client_info.model_dump_json())


def provider(actor, url):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    pending = {}
    async def redirect(auth_url):
        if urlsplit(auth_url).scheme not in {'http', 'https'} or not urlsplit(auth_url).hostname:
            raise ValueError('Некорректный адрес авторизации')
        state = parse_qs(urlsplit(auth_url).query).get('state', [''])[0]
        if not state:
            raise ValueError('OAuth-сервер не вернул state')
        future = asyncio.get_running_loop().create_future()
        future._mcp_actor = actor
        actor.manager.oauth_waiters[state] = future
        pending.update(state=state, future=future)
        actor.authorization_url = auth_url
        actor.status = 'authorizing'
        if not actor.ready.done():
            actor.ready.set_result(True)
    async def callback():
        try:
            code, state, issuer = await asyncio.wait_for(pending['future'], 300)
            try:
                from mcp.shared.auth import AuthorizationCodeResult
                return AuthorizationCodeResult(code=code, state=state, iss=issuer)
            except ImportError:
                return code, state
        finally:
            actor.manager.oauth_waiters.pop(pending.get('state'), None)
            actor.authorization_url = ''
            actor.status = 'connecting'
    return OAuthClientProvider(server_url=url, storage=OAuthStorage(actor.owner, actor.record['id']),
        client_metadata=OAuthClientMetadata(client_name='Toshy', redirect_uris=[CALLBACK],
            grant_types=['authorization_code', 'refresh_token'], response_types=['code'], token_endpoint_auth_method='none'),
        redirect_handler=redirect, callback_handler=callback)
