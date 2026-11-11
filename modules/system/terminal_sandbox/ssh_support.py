"""Optional SSH transport, loaded only when SSH is actually used."""
import asyncio
import importlib
import json
import os

_passwords = {}
SERVICE = 'Toshy terminal SSH'


def password(identity, value=None):
    from core.model_connections import _secure_keyring
    if value is not None:
        _passwords[identity] = value
        try:
            backend = _secure_keyring()
            if value:
                backend.set_password(SERVICE, identity, value)
            else:
                try:
                    backend.delete_password(SERVICE, identity)
                except Exception:
                    pass
        except Exception:
            pass  # The value remains in memory only; never write plaintext.
        return
    if identity in _passwords:
        return _passwords[identity]
    try:
        return _secure_keyring().get_password(SERVICE, identity) or None
    except Exception:
        return None


async def library(root):
    # SSH is a normal locked application dependency. Never install unpinned
    # executable code on first use or import an old unverified fallback folder.
    try:
        return importlib.import_module('asyncssh')
    except ImportError as error:
        raise RuntimeError('SSH requires AsyncSSH. Repair Toshy using its verified installer or install the platform release lock.') from error


async def connect(manager, profile, job):
    ssh = await library(manager.store.root)
    host, port = profile['ssh_host'], profile['ssh_port']
    path = manager.store.root / 'ssh-hosts.json'
    known = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    identity = f'{host}:{port}'
    key = await asyncio.wait_for(ssh.get_server_host_key(host, port), 10)
    if key is None:
        raise RuntimeError('SSH-сервер не предоставил ключ.')
    fingerprint = key.get_fingerprint('sha256')
    if identity in known and known[identity] != fingerprint:
        raise ValueError('Ключ SSH-сервера изменился. Проверьте сервер и удалите старый ключ в настройках подключения.')
    if identity not in known:
        decision = await manager.policy.ask(profile, job, 'Первое подключение: проверьте отпечаток ключа SSH-сервера.',
            kind='host_key', details={'host': identity, 'fingerprint': fingerprint})
        if decision == 'deny':
            raise ValueError('Ключ SSH-сервера не подтверждён.')
        from .store import write_json
        # Re-read after waiting: another approval may have added a different host.
        known = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        known[identity] = fingerprint; write_json(path, known)
    class Client(ssh.SSHClient):
        def validate_host_public_key(self, host, addr, port, server_key):
            return server_key.get_fingerprint('sha256') == fingerprint
    options = dict(host=host, port=port, username=profile['ssh_user'], known_hosts=b'',
                   client_factory=Client, connect_timeout=10, login_timeout=20, keepalive_interval=15)
    secret = password(manager.store.owner + ':' + profile['id'])
    if secret:
        options['password'] = secret
    if profile['ssh_key']:
        options['client_keys'] = [os.path.expanduser(profile['ssh_key'])]
        if secret:
            options['passphrase'] = secret
    return await ssh.connect(**options)
