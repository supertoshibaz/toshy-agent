"""Deterministic structured-data/text transforms; no eval or model calls."""
from __future__ import annotations
import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

OPERATIONS = {
    'json_parse': 'JSON → объект', 'json_dump': 'Объект → JSON',
    'get': 'Извлечь поле/путь', 'select': 'Выбрать поля', 'rename': 'Переименовать поля',
    'filter': 'Фильтр массива', 'sort': 'Сортировка массива', 'unique': 'Убрать дубликаты',
    'replace': 'Заменить текст', 'regex': 'Регулярное выражение', 'split': 'Разбить текст',
    'join': 'Объединить строки', 'strip_markdown': 'Убрать Markdown',
    'date': 'Дата/часовой пояс', 'convert': 'Преобразовать тип',
}


def extract(value, path):
    """JSON Pointer (/a/0) or dot path (a.0); literal keys use JSON Pointer."""
    if path in ('', None):
        return value
    keys = [p.replace('~1', '/').replace('~0', '~') for p in str(path)[1:].split('/')] if str(path).startswith('/') else str(path).split('.')
    for key in keys:
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def strip_markdown(text):
    text = re.sub(r'^\s*(`{3,}|~{3,})[^\n]*\n?', '', str(text), flags=re.M)
    text = re.sub(r'!\[([^\]]*)\]\([^\n)]*\)', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^\n)]*\)', r'\1', text)
    text = re.sub(r'^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)', '', text, flags=re.M)
    text = re.sub(r'(\*\*|__|~~)(.+?)\1', r'\2', text)
    text = re.sub(r'(?<!\w)([*_])([^\n]+?)\1(?!\w)', r'\2', text)
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    return text


def apply(value, operations, *, parse_json_input=False):
    if not isinstance(operations, list) or not operations:
        raise ValueError('Add at least one transform operation')
    if parse_json_input and isinstance(value, str) and operations[0].get('op') in {'get','select','rename','filter','sort','unique'}:
        try: value = json.loads(value)
        except ValueError as error: raise ValueError('Вход должен быть объектом или корректным текстом JSON: '+str(error)) from error
    for index, op in enumerate(operations, 1):
        try:
            kind = op['op']
            if kind == 'json_parse': value = json.loads(value)
            elif kind == 'json_dump': value = json.dumps(value, ensure_ascii=False, indent=op.get('indent', 2), allow_nan=False)
            elif kind == 'get': value = extract(value, op.get('path', ''))
            elif kind == 'select': value = {key: extract(value, key) for key in op['fields']}
            elif kind == 'rename': value = {op.get('fields', {}).get(k, k): v for k, v in value.items()}
            elif kind == 'filter':
                comparator = op.get('comparison', 'eq'); expected = op.get('value')
                def keep(item):
                    actual = extract(item, op.get('path', ''))
                    if comparator == 'eq': return actual == expected
                    if comparator == 'ne': return actual != expected
                    if comparator == 'contains': return expected in actual
                    if comparator == 'gt': return actual > expected
                    if comparator == 'lt': return actual < expected
                    raise ValueError('Unknown comparison: ' + comparator)
                value = [item for item in value if keep(item)]
            elif kind == 'sort': value = sorted(value, key=lambda x: extract(x, op.get('path', '')), reverse=bool(op.get('reverse', False)))
            elif kind == 'unique':
                result, seen = [], set()
                for item in value:
                    key = json.dumps(extract(item, op.get('path', '')), sort_keys=True, ensure_ascii=False)
                    if key not in seen: seen.add(key); result.append(item)
                value = result
            elif kind == 'replace': value = str(value).replace(str(op['old']), str(op.get('new', '')), int(op.get('count', -1)))
            elif kind == 'regex':
                # Run untrusted patterns with a deadline, including substitutions.
                import subprocess, sys
                worker = ('import json,re,sys; p=json.load(sys.stdin); '
                    'r=re.compile(p["pattern"], re.I if p.get("ignore_case") else 0); '
                    'v=r.sub(p["replacement"],p["text"]) if "replacement" in p else r.findall(p["text"]); '
                    'json.dump(v,sys.stdout,ensure_ascii=False)')
                report = subprocess.run([sys.executable, '-I', '-c', worker],
                    input=json.dumps(dict(op, text=str(value))), text=True, capture_output=True, timeout=2)
                if report.returncode: raise ValueError(report.stderr[-2000:])
                value = json.loads(report.stdout)
            elif kind == 'split': value = str(value).split(op.get('separator', '\n'))
            elif kind == 'join': value = str(op.get('separator', '\n')).join(str(x) for x in value)
            elif kind == 'strip_markdown': value = strip_markdown(value)
            elif kind == 'date':
                source = op.get('input_format', 'iso'); target = op.get('output_format', 'iso')
                zone = ZoneInfo(op.get('timezone', 'UTC'))
                if source == 'timestamp': date = dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
                elif source == 'timestamp_ms': date = dt.datetime.fromtimestamp(float(value) / 1000, dt.timezone.utc)
                elif source == 'iso': date = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                else: date = dt.datetime.strptime(str(value), source)
                if date.tzinfo is None: date = date.replace(tzinfo=ZoneInfo(op.get('input_timezone', 'UTC')))
                date = date.astimezone(zone)
                value = date.timestamp() if target == 'timestamp' else date.timestamp()*1000 if target == 'timestamp_ms' else date.isoformat() if target == 'iso' else date.strftime(target)
            elif kind == 'convert':
                from .runtime import typed
                target = op.get('type', 'string')
                value = json.loads(value) if isinstance(value, str) and target in {'object', 'array'} else typed(value, target)
                value = typed(value, target)
            else: raise ValueError('Unknown operation: ' + str(kind))
        except Exception as exc:
            raise ValueError(f'Transform {index} ({op.get("op", "?")}): {exc}') from exc
    return value
