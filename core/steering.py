"""Уточнения текущего запуска: один поток модели, сохранённые заметки и безопасные границы tools."""
import asyncio
import copy
from contextlib import contextmanager, aclosing
from langchain_core.messages import HumanMessage, message_to_dict, messages_from_dict

_RUNS = {}


class SteeringRequested(Exception):
    pass


class SteeringInbox:
    def __init__(self, context, journal, emit):
        self.context, self.journal, self.emit = context, journal, emit
        self.pending = copy.deepcopy(journal.data.get('pending_updates') or []) if journal else []
        self.interrupt = asyncio.Event()
        self.accepting = True
        if self.pending:
            self.interrupt.set()

    async def accept(self, message):
        if not self.accepting:
            raise ValueError('Генерация уже завершена. Уточнение осталось в поле ввода.')
        message.additional_kwargs['steering_update'] = True
        identity = message.additional_kwargs['chat_id']
        known = self.journal.data.setdefault('steering_ids', []) if self.journal else []
        if identity in known:
            return
        if len(self.pending) >= 32:
            raise ValueError('Слишком много ещё не применённых уточнений.')
        known.append(identity)
        self.pending.append(message_to_dict(message))
        if self.journal:
            self.journal.data.setdefault('pending_updates', []).append(message_to_dict(message))
        # Сначала пишем сообщение в журнал, затем разрешаем прерывание модели.
        await self.emit({'type': 'user_update', 'message': message_to_dict(message), 'id': identity, 'run_id': self.context.run_id})
        self.interrupt.set()

    def take(self):
        result = messages_from_dict(self.pending)
        self.pending = []
        self.interrupt.clear()
        # Удаление из дискового inbox происходит только вместе с новым протоколом.
        return result


@contextmanager
def steering_scope(context, journal, emit):
    inbox = SteeringInbox(context, journal, emit)
    _RUNS[context.run_id] = inbox
    try:
        yield inbox
    finally:
        inbox.accepting = False
        if _RUNS.get(context.run_id) is inbox:
            _RUNS.pop(context.run_id, None)


def inbox_for(run_id):
    return _RUNS.get(run_id)


def has_updates(context):
    inbox = inbox_for(context.run_id)
    return bool(inbox and inbox.pending)


async def steerable_stream(source, context):
    inbox = inbox_for(context.run_id)
    if inbox is None:
        async with aclosing(source):
            async for delta in source:
                yield delta
        return
    queue = asyncio.Queue(maxsize=1)
    # Генератор целиком принадлежит одной задаче: сохраняем ContextVar провайдера.
    async def produce():
        try:
            async with aclosing(source):
                async for delta in source:
                    await queue.put(('delta', delta))
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            await queue.put(('error', error))
        else:
            await queue.put(('end', None))
    task = asyncio.create_task(produce())
    stop = asyncio.create_task(inbox.interrupt.wait())
    read = None
    try:
        while True:
            read = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait((read, stop), return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                if read.done() and not read.cancelled():
                    kind, value = read.result()
                    if kind == 'delta':
                        yield value
                raise SteeringRequested()
            kind, value = read.result()
            read = None
            if kind == 'end':
                return
            if kind == 'error':
                raise value
            yield value
    finally:
        for pending in (task, stop, read):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(*(p for p in (task, stop, read) if p is not None), return_exceptions=True)
