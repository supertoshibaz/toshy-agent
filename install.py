#!/usr/bin/env python3
"""Install a local release with an existing Python (bootstrap scripts need none)."""
from pathlib import Path
import sys
from release_tools.cli import main

if __name__ == '__main__':
    arguments = sys.argv[1:]
    root = ['--root', str(Path(__file__).resolve().parent)]
    while '--root' in arguments:
        i = arguments.index('--root')
        root = arguments[i:i + 2]
        del arguments[i:i + 2]
    raise SystemExit(main([*root, 'install', '--source', str(Path(__file__).resolve().parent), *arguments]))
