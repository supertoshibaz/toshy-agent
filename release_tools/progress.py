"""Immediate CLI output with one replaceable download/working line."""
import re
import shutil
import sys
import time


class ConsoleProgress:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.tty = bool(getattr(self.stream, 'isatty', lambda: False)())
        self.width = 0
        self.last_draw = 0.0
        self.download_name = None

    def clear(self):
        if self.width:
            self.stream.write('\r' + ' ' * self.width + '\r')
            self.stream.flush()
            self.width = 0

    def line(self, text):
        self.clear()
        self.stream.write(str(text) + '\n')
        self.stream.flush()

    def __call__(self, message):
        match = re.fullmatch(r'Downloading (.*): (\d+)%', str(message))
        if match:
            self.download(match[1], int(match[2]), 100)
        else:
            self.line(message)

    def draw(self, text, *, force=False):
        if not self.tty:
            return
        now = time.monotonic()
        if not force and now - self.last_draw < 0.1:
            return
        # A carriage return cannot erase wrapped terminal lines.
        width = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        text = text[:width]
        self.stream.write('\r' + text + ' ' * max(0, self.width - len(text)))
        self.stream.flush()
        self.width = len(text)
        self.last_draw = now

    def download(self, name, count, total=None, *, finished=False):
        if self.download_name != name:
            self.clear()
            self.download_name = name
            if not self.tty:
                self.line('Downloading ' + name + '…')
        if total:
            percent = min(100, max(0, int(count * 100 / total)))
            columns = max(30, shutil.get_terminal_size((80, 24)).columns)
            label = name[:max(8, columns - 36)]
            filled = percent * 20 // 100
            text = label + ' [' + '=' * filled + ' ' * (20 - filled) + f'] {percent:3d}%'
        else:
            text = name + f'  {count / (1024 * 1024):.1f} MiB'
        if self.tty:
            self.draw(text, force=finished)
        if finished:
            if self.tty:
                self.stream.write('\n'); self.stream.flush(); self.width = 0
            else:
                self.line('Downloaded ' + name)
            self.download_name = None

    def tick(self, label, elapsed):
        frame = '|/-\\'[int(elapsed * 5) % 4]
        self.draw(f'{frame} {label} ({int(elapsed)} s)')

    def raw(self, text):
        self.clear()
        self.stream.write(text)
        self.stream.flush()


def command_label(args):
    parts = list(map(str, args))
    if 'pip' in parts and 'sync' in parts:
        lock = parts[-1].replace('\\', '/').rsplit('/', 1)[-1]
        return 'Installing Python packages (' + lock + ')'
    if 'python' in parts and 'install' in parts:
        return 'Preparing Python ' + parts[-1]
    if 'venv' in parts:
        return 'Creating Python environment'
    if any(p in parts for p in ('dnf', 'apt-get', 'pacman', 'winget')):
        return 'Installing system packages'
    if 'find' in parts and 'python' in parts:
        return 'Locating private Python'
    if 'systemctl' in parts:
        return 'Starting Docker service'
    return 'Running ' + parts[0].replace('\\', '/').rsplit('/', 1)[-1] + ' ' + ' '.join(parts[1:2])
