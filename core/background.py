"""Bounded workers for substantial memory/indexing work in async services."""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial


_workers = min(4, max(1, (os.cpu_count() or 1) // 2))
_pool = ThreadPoolExecutor(max_workers=_workers, thread_name_prefix='toshy-data')


async def run_data_task(function, /, *args, **kwargs):
    # Session, branch and allowed modules must survive the worker boundary.
    context = copy_context()
    call = partial(context.run, function, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(_pool, call)
