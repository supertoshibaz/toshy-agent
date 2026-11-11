"""Persist local calendar context on user turns, never in the system prompt."""
from datetime import datetime

CLOCK = 'request_clock'


def stamp_latest_user(messages, now=None):
    user_index = next((i for i in range(len(messages)-1, -1, -1)
                       if messages[i].type == 'human' and not messages[i].additional_kwargs.get('module_guidance')), None)
    if user_index is None:
        return
    message = messages[user_index]
    if isinstance(message.additional_kwargs.get(CLOCK), dict):
        return  # Retry/resume preserves the original turn's time.
    previous = next((m.additional_kwargs[CLOCK] for m in reversed(messages[:user_index])
                     if m.type == 'human' and isinstance(m.additional_kwargs.get(CLOCK), dict)), {})
    # astimezone() uses the OS timezone on both Windows and Unix, including DST;
    # no IANA database, TZ environment variable or Linux path is required.
    current = (now or datetime.now().astimezone()).astimezone()
    year, date = current.strftime('%Y'), current.strftime('%d-%m')
    message.additional_kwargs[CLOCK] = {
        'timestamp': current.isoformat(timespec='minutes'),
        'year': year, 'date': date, 'time': current.strftime('%H:%M'),
        'send_year': previous.get('year') != year,
        'send_date': previous.get('date') != date or previous.get('year') != year,
    }


def clock_context(message):
    data = message.additional_kwargs.get(CLOCK)
    if message.type != 'human' or not isinstance(data, dict):
        return ''
    fields = []
    if data.get('send_year'):
        fields.append('Year: ' + str(data.get('year', '')))
    if data.get('send_date'):
        fields.append('Date (day-month): ' + str(data.get('date', '')))
    fields.append('Local time (HH:MM): ' + str(data.get('time', '')))
    return 'Application calendar context for this user request. ' + '; '.join(fields) + '.\n'


def preserve_clocks(messages, originals):
    by_id = {m.additional_kwargs.get('chat_id'): m.additional_kwargs[CLOCK]
             for m in originals if m.type == 'human' and m.additional_kwargs.get('chat_id')
             and isinstance(m.additional_kwargs.get(CLOCK), dict)}
    for message in messages:
        original = by_id.get(message.additional_kwargs.get('chat_id'))
        if message.type == 'human' and original is not None:
            message.additional_kwargs[CLOCK] = dict(original)
