"""Per-event-loop host pacing shared by independent Web Search operations."""

import asyncio
import time
import weakref
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

_loops = weakref.WeakKeyDictionary()


class HostGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_start = 0.0
        self.cooldown = 0.0

    async def wait(self, interval):
        async with self.lock:
            while True:
                pause = max(self.next_start, self.cooldown) - time.monotonic()
                if pause <= 0:
                    self.next_start = time.monotonic() + interval
                    return
                if pause > 120:
                    from .network import NetworkConfigurationError

                    raise NetworkConfigurationError(
                        f"Сайт ограничил частоту запросов. Повторите позже, примерно через {int(pause) + 1} с."
                    )
                await asyncio.sleep(pause)

    def defer(self, seconds):
        self.cooldown = max(self.cooldown, time.monotonic() + seconds)


def host_gate(url):
    loop = asyncio.get_running_loop()
    hosts = _loops.setdefault(loop, OrderedDict())
    parsed = urlsplit(url)
    key = (parsed.scheme, parsed.hostname, parsed.port)
    gate = hosts.pop(key, None) or HostGate()
    hosts[key] = gate
    # Only evict idle gates; active waits and server cooldowns stay authoritative.
    if len(hosts) > 2048:
        now = time.monotonic()
        for old, value in list(hosts.items()):
            if (
                old != key
                and not value.lock.locked()
                and max(value.next_start, value.cooldown) < now
            ):
                hosts.pop(old, None)
                if len(hosts) <= 2048:
                    break
    return gate


def retry_after(value):
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0.0, parsedate_to_datetime(str(value)).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0.0
