"""Полное чтение разрешает пользователь; модель не может включить его аргументом."""
from core.context import get_execution_context
from collections import OrderedDict

_budgets = OrderedDict()


async def authorized_page_text(page, memory):
    context = get_execution_context()
    if not context.full_web_pages:
        return ''
    from core.token_usage import discover_limit, read_usage, studio_id
    from core.sqlite_storage import SQLiteStore
    client = memory.client
    if client is None:
        return 'Полное чтение недоступно: нет подключения модели. Текст сохранён в памяти.\n'
    connection = client.model_connection()
    import asyncio
    try:
        limit = await discover_limit(connection)
        usage = await asyncio.to_thread(read_usage, SQLiteStore(context.storage_dir / 'session.sqlite'), studio_id(), context.branch)
    except Exception:
        return 'Текст сохранён в памяти. Не удалось проверить свободное место в контексте для полного чтения.\n'
    used = (usage.get('context') or {}).get('total')
    # Верхняя оценка по байтам безопаснее среднего «символов на токен».
    estimate = max(1, len(page.storage_text.encode('utf-8')))
    if not limit or used is None:
        return ('Полный текст сохранён в памяти. Для передачи целиком нужно определить '
                'лимит и текущее заполнение контекста; сейчас они неизвестны.\n')
    previous_used, emitted = _budgets.get(context.run_id, (used, 0))
    if previous_used != used:emitted = 0
    available = max(0, limit - used - emitted - max(2048, limit // 20))
    if estimate > available:
        return (f'Полный текст не передан: оценка {estimate} токенов, доступно около {available}. '
                'Освободите контекст или используйте поиск по временной памяти.\n')
    _budgets[context.run_id] = (used, emitted + estimate)
    _budgets.move_to_end(context.run_id)
    while len(_budgets) > 512:_budgets.popitem(last=False)
    return (f'USER-AUTHORIZED FULL PAGE TEXT: {page.url}\n'
            + page.storage_text + '\nEND OF FULL PAGE TEXT\n\n')
