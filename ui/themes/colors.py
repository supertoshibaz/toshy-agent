"""Local colour shifts, without extending or rewriting a theme's palette."""
import re

_REFERENCE = re.compile(r'@([\w.]+)(?:~([+-]?(?:\d+(?:\.\d*)?|\.\d+)))?@')


def shifted_hex(value, shift=0):
    if not -1 <= shift <= 1:
        raise ValueError('Colour shift must be between -1 and 1.')
    offset = round(255 * shift)
    channels = [max(0, min(255, int(value[i:i+2], 16) + offset)) for i in (1, 3, 5)]
    return '#' + ''.join(f'{channel:02x}' for channel in channels)


def resolve_values(template, theme):
    values = {**theme['colors'], **theme['metrics'], 'workspace.background': workspace_background(theme)}
    def replace(match):
        key, shift = match.groups()
        if key not in values:
            return match.group(0)
        return shifted_hex(values[key], float(shift)) if shift is not None else str(values[key])
    return _REFERENCE.sub(replace, template)


def workspace_background(theme):
    # Recessed in both foundations; dark workspaces keep a little more detail.
    shift = -0.015 if theme['foundation'] == 'dark' else -0.04
    return shifted_hex(theme['colors']['background.window'], shift)
