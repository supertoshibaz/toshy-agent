"""Полный запрос проверки задач; видимое сообщение хранится отдельно."""
from typing import Iterable, Dict, Any


def build_task_status_prompt(tasks: Iterable[Dict[str, Any]]) -> str:
    rows = ['- [' + str(t['id']) + '] ' + str(t.get('text') or '').strip() for t in tasks if t.get('id')]
    return (
        'Review the current status of these existing tasks using this conversation history:\n\n'
        + ('\n'.join(rows) or '- (no outstanding tasks)') + '\n\n'
        'A task is complete only when all requested work is finished. Unanswered questions, unfinished parts, '
        'deferred actions or missing user input mean it is NOT complete. Do not create tasks or call create_tasks. '
        'Before your final answer, call report_task_statuses for exactly every task listed above, using its exact ID. '
        'Use completed=true only for fully finished work and leave comment empty. For completed=false, explain '
        'what remains in comment. Do not report unlisted tasks. Correct any rejected report and call the tool again. '
        'After a successful report, give one brief final answer in the language of this conversation. Mention '
        'ONLY unfinished tasks and what remains; omit completed tasks, introductions and conclusions. '
        'If none remain, say only that there are no unfinished tasks.'
    )
