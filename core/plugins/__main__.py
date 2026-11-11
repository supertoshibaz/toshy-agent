"""Developer tools for plugin format v1. No GitHub account or plugin execution."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .manifest import PluginError, tags_for
from .package import export_package, inspect_directory, inspect_package
from .resolver import resolve_plugins
from .store import PluginStore


def main(argv=None):
    from core.paths import APP_PATHS
    parser = argparse.ArgumentParser(description='Toshy plugin packaging and compatibility planner; code changes activate after application restart.')
    parser.add_argument('--store', type=Path, default=APP_PATHS.root / 'plugins')
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('validate'); validate.add_argument('source', type=Path)
    export = commands.add_parser('export'); export.add_argument('source', type=Path); export.add_argument('destination', type=Path)
    install = commands.add_parser('install'); install.add_argument('archive', type=Path); install.add_argument('--sha256')
    order = commands.add_parser('order'); order.add_argument('ids', nargs='+')
    for name in ('enable', 'disable', 'remove'):
        command = commands.add_parser(name); command.add_argument('id')
    commands.add_parser('plan')
    args = parser.parse_args(argv)
    store = PluginStore(args.store)
    try:
        if args.command == 'validate':
            manifest = inspect_directory(args.source)[0] if args.source.is_dir() else inspect_package(args.source)
            result = dict(id=manifest['id'], version=manifest['version'], tags=tags_for(manifest), valid=True)
        elif args.command == 'export':
            manifest = export_package(args.source, args.destination)
            result = dict(id=manifest['id'], archive=str(args.destination.resolve()))
        elif args.command == 'install':
            manifest = store.install(args.archive, expected_sha256=args.sha256)
            result = dict(id=manifest['id'], version=manifest['version'], installed=True, runtime_activation_available=True)
        elif args.command == 'order':
            store.reorder(args.ids); result = store.state()
        elif args.command in ('enable', 'disable'):
            store.set_enabled(args.id, args.command == 'enable')
            result = dict(state=store.state(), runtime_activation_available=True)
        elif args.command == 'remove':
            store.remove(args.id); result = store.state()
        else:
            state = store.state()
            result = asdict(resolve_plugins(store.manifests(), state['order'], state['enabled']))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PluginError, OSError, ValueError) as error:
        print(f'Plugin error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
