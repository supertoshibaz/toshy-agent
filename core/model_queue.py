"""Один запрос к локальному серверу; служебные задачи уступают обычным."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from weakref import WeakKeyDictionary

_loops = WeakKeyDictionary()
_held = ContextVar('local_model_slots', default=frozenset())


class _Gate:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.busy = False
        self.waiting_users = 0


@asynccontextmanager
async def model_slot(connection, *, auxiliary=False):
    if connection.mode != 'local':
        yield
        return
    key = connection.base_url.rstrip('/').removesuffix('/v1')
    if key in _held.get():
        yield
        return
    gates = _loops.setdefault(asyncio.get_running_loop(), {})
    gate = gates.setdefault(key, _Gate())
    async with gate.condition:
        if not auxiliary:gate.waiting_users += 1
        try:
            await gate.condition.wait_for(lambda: not gate.busy and (not auxiliary or not gate.waiting_users))
            gate.busy = True
        finally:
            if not auxiliary:gate.waiting_users -= 1
            gate.condition.notify_all()
    token = _held.set(_held.get() | {key})
    try:
        yield
    finally:
        _held.reset(token)
        async with gate.condition:
            gate.busy = False
            gate.condition.notify_all()
