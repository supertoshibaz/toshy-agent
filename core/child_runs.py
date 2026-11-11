"""Module-independent child execution lifecycle."""
from contextlib import contextmanager

_running_children = {}


@contextmanager
def track_child_run(context):
    import asyncio

    task = asyncio.current_task()
    _running_children[context.run_id] = (
        task,
        context.session_id,
        context.branch,
        context.parent_run_id,
    )
    try:
        yield
    finally:
        _running_children.pop(context.run_id, None)


async def cancel_child_runs(parent_run_id, *, whole_chat=False):
    import asyncio

    rows = list(_running_children.values())
    scopes = {
        (session, branch)
        for task, session, branch, parent in rows
        if parent == parent_run_id and session
    }
    jobs = {
        task
        for task, session, branch, parent in rows
        if parent == parent_run_id or whole_chat and (session, branch) in scopes
    }
    jobs.discard(asyncio.current_task())
    for task in jobs:
        if not task.done() and not task.cancelling():
            task.cancel()
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)
    return len(jobs)
