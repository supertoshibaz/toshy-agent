"""One optional UI-close hook, including when the agent core is already stopped."""
import asyncio
import json
import os
import subprocess
import yaml

from .store import ContainerStore


def pending_cleanup():
    root = ContainerStore().root
    if (root / 'host-runtime.json').exists():
        return True
    path = root / 'instances.json'
    if not path.exists():
        return False
    return bool(json.loads(path.read_text(encoding='utf-8')))


def shutdown_notice():
    """Read-only close review, run in a worker, separate from stored-data cleanup.

    Stopped saved environments do not require a Docker daemon connection. A
    remembered live container is checked because it may have stopped externally.
    """
    store=ContainerStore()
    path=store.root/'instances.json'
    instances=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    docker=any(instance.get('running') or instance.get('initializer') for instance in instances.values())
    marker=store.root/'host-runtime.json'
    host=False
    if marker.exists():
        from core.log_session import LOG_ENV
        value=json.loads(marker.read_text(encoding='utf-8'))
        host=not os.environ.get(LOG_ENV) or value.get('app_session')==os.environ[LOG_ENV]
    if docker:
        try:
            result=subprocess.run(['docker','container','ls','--filter',f'label=toshy.terminal.owner={store.owner}',
                                   '--format','{{.Names}}'],capture_output=True,text=True,timeout=3,check=False)
            if result.returncode==0:docker=bool(result.stdout.strip())
        except (OSError,subprocess.TimeoutExpired):
            pass  # Unknown state: retain the last known running indication.
    return {'docker':docker,'host':host}


def close_application():
    """Called in a UI worker before the application's final close is approved."""
    from core.security import auth_headers
    from ui.core_bridge.process_manager import core_process
    from ui.core_bridge.transport import core_http_client
    from core.paths import APP_PATHS
    config = yaml.safe_load(APP_PATHS.config.read_text(encoding='utf-8')) or {}
    if core_process.is_running() and 'terminal_sandbox' in config.get('system_modules', []):
        with core_http_client(core_process.base_url, timeout=45) as client:
            response = client.post(core_process.base_url + '/v1/modules/terminal_sandbox/service/app-close',
                                   json={}, headers=auth_headers())
        if response.status_code != 404:
            if response.is_error:
                raise RuntimeError(response.text)
            return
    if not pending_cleanup():
        return
    # Module disabled/core stopped: containers still belong to this installation.
    from .runtime import TerminalManager
    manager = TerminalManager()
    asyncio.run(manager.shutdown(app_close=True))
