"""Theme validation and shade generation. No Qt or application state changes."""
from __future__ import annotations

import colorsys
import json
from pathlib import Path
import re
import uuid

ROOT = Path(__file__).resolve().parent
BUILTINS = ('toshy', 'dark', 'light')
REQUIRED = ('background.window', 'background.panel', 'background.surface', 'background.input',
            'text.primary', 'text.secondary', 'text.disabled', 'accent.subtle', 'accent.primary',
            'control.hover', 'control.pressed', 'control.selected', 'control.disabled',
            'border.subtle', 'border.focus', 'icon.normal', 'icon.active', 'icon.disabled')
METRICS = {'spacing.small': 4, 'spacing.medium': 8, 'spacing.large': 12, 'spacing.dialog': 16,
           'radius.control': 6, 'radius.panel': 8, 'radius.tab': 4, 'radius.dialog': 8,
           'icon.size.small': 16, 'icon.size.normal': 20}


def validate(data):
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('Unsupported theme schema; expected version 1.')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', str(data.get('id', ''))):
        raise ValueError('Invalid theme identifier.')
    if not isinstance(data.get('name'), str) or not data['name'].strip():
        raise ValueError('A theme needs a display name.')
    if data.get('foundation') not in {'dark', 'light'}:
        raise ValueError('Theme foundation must be dark or light.')
    colors = data.get('colors')
    if not isinstance(colors, dict) or set(REQUIRED) - colors.keys():
        raise ValueError('Theme is missing required semantic colors.')
    if any(not isinstance(v, str) or not re.fullmatch(r'#[\da-fA-F]{6}', v) for v in colors.values()):
        raise ValueError('Theme colors must be six-digit HEX values.')
    metrics = dict(METRICS)
    for key, value in data.get('metrics', {}).items():
        if key not in metrics or isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 64:
            raise ValueError('Invalid theme metric: ' + str(key))
        metrics[key] = value
    return {**data, 'colors': dict(colors), 'metrics': metrics}


def load(path):
    return validate(json.loads(Path(path).read_text(encoding='utf-8')))


def discover():
    themes, errors = {}, []
    for path in [*(ROOT / (name + '.json') for name in BUILTINS), *sorted((ROOT / 'custom').glob('*.json'))]:
        try:
            value = load(path)
            if value['id'] in themes:
                raise ValueError('Duplicate theme identifier: ' + value['id'])
            themes[value['id']] = {**value, 'path': path, 'builtin': path.parent == ROOT}
        except (OSError, ValueError, TypeError) as error:
            errors.append((str(path), str(error)))
    return themes, errors


def _hex(rgb):
    return '#' + ''.join(f'{max(0, min(255, round(v * 255))):02x}' for v in rgb)


def custom_theme(name, foundation, tone, *, identity=None):
    if foundation not in {'dark', 'light'} or not re.fullmatch(r'#[\da-fA-F]{6}', tone):
        raise ValueError('Choose a dark/light foundation and a six-digit color.')
    base = load(ROOT / (foundation + '.json'))
    rgb = tuple(int(tone[i:i+2], 16) / 255 for i in (1, 3, 5))
    hue, _, saturation = colorsys.rgb_to_hls(*rgb)
    def shade(lightness, strength):
        return _hex(colorsys.hls_to_rgb(hue, lightness, min(saturation, strength)))
    if foundation == 'dark':
        colors = {'background.window': shade(.095, .10), 'background.panel': shade(.155, .12),
                  'background.surface': shade(.18, .15), 'background.input': shade(.075, .12),
                  'accent.subtle': shade(.235, .34), 'accent.primary': shade(.74, .65),
                  'control.hover': shade(.22, .18), 'control.pressed': shade(.12, .18),
                  'control.selected': shade(.235, .34), 'border.focus': shade(.74, .65),
                  'icon.active': shade(.74, .65)}
    else:
        colors = {'background.window': shade(.93, .16), 'background.panel': shade(.965, .16),
                  'background.surface': shade(.90, .16), 'background.input': shade(.975, .14),
                  'accent.subtle': shade(.84, .30), 'accent.primary': shade(.27, .45),
                  'control.hover': shade(.88, .24), 'control.pressed': shade(.80, .24),
                  'control.selected': shade(.84, .30), 'border.focus': shade(.27, .45),
                  'icon.active': shade(.27, .45)}
    return validate({**{k: v for k, v in base.items() if k not in {'path', 'builtin'}},
                     'id': identity or 'custom_' + uuid.uuid4().hex[:12], 'name': name.strip(),
                     'tone': tone, 'colors': {**base['colors'], **colors}})


def save_custom(data):
    value = validate(data)
    if value['id'] in BUILTINS:
        raise ValueError('Built-in themes cannot be overwritten.')
    directory = ROOT / 'custom'
    directory.mkdir(exist_ok=True)
    target = directory / (value['id'] + '.json')
    temporary = target.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
