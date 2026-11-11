"""Portable data archives. Offline recovery: python -m core.backups restore FILE."""
from __future__ import annotations
import argparse
import atexit
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile

from core.paths import APP_PATHS
from core.private_files import create_private
from core.file_history import FileHistory, HistoryError


def settings():
    path=APP_PATHS.runtime/'backup_settings.json'
    try:raw=json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:raw={}
    return dict(directory=raw.get('directory') or str(APP_PATHS.root/'backups'),
                interval_hours=max(0,int(raw.get('interval_hours',0))),
                keep=max(1,int(raw.get('keep',10))),include_history=bool(raw.get('include_history',True)),
                last_success=float(raw.get('last_success',0)))


def configure(updates):
    value=settings();value.update({k:v for k,v in updates.items() if k in value and k!='last_success'})
    value['interval_hours']=max(0,min(720,int(value['interval_hours'])))
    value['keep']=max(1,min(1000,int(value['keep'])))
    value['directory']=str(Path(value['directory']).expanduser().resolve())
    _save_settings(value);return value


def _save_settings(value):
    APP_PATHS.runtime.mkdir(parents=True,exist_ok=True)
    target=APP_PATHS.runtime/'backup_settings.json';temp=target.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,target)


def _selected():
    root=APP_PATHS.root
    paths=[APP_PATHS.config,APP_PATHS.sessions,APP_PATHS.ui_state,
           root/'core/permanent_memory.sqlite',root/'modules/custom',root/'plugins',
           APP_PATHS.runtime/'backup_settings.json',APP_PATHS.runtime/'skill-environments.json']
    for module in (root/'modules/system').glob('*'):
        if module.is_dir():
            for name in ('skills','pipelines','subagents','mcp','knowledge'):
                paths.append(module/name)
    return paths


def _allowed(relative):
    path=PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative:return False
    return (relative in {'config.yaml','ui/app_state.json','core/permanent_memory.sqlite',
                        'runtime/backup_settings.json','runtime/skill-environments.json'}
            or path.parts[:1] in [('sessions',),('plugins',)]
            or path.parts[:2]==('modules','custom')
            or path.parts[:2]==('runtime','file_history')
            or len(path.parts)>3 and path.parts[:2]==('modules','system') and path.parts[3] in {'skills','pipelines','subagents','mcp','knowledge'})


def _allowed_root(relative):
    parts=PurePosixPath(relative).parts
    return (relative in {'sessions','modules/custom','plugins','runtime/file_history'}
            or len(parts)==4 and parts[:2]==('modules','system')
            and parts[3] in {'skills','pipelines','subagents','mcp','knowledge'}
            and _allowed(relative+'/placeholder'))


def _files(selected,destination):
    seen=set()
    for source in selected:
        if not source.exists():continue
        for path in ([source] if source.is_file() else source.rglob('*')):
            if not path.is_file() or path.is_symlink():continue
            if any(p.is_symlink() for p in path.parents if p.is_relative_to(APP_PATHS.root)):continue
            if path.resolve().is_relative_to(destination):continue
            relative=path.relative_to(APP_PATHS.root).as_posix()
            if not _allowed(relative) or relative in seen:continue
            if any(p in {'.venv','venv','__pycache__','node_modules','.git'} for p in path.parts):continue
            if path.name.endswith(('-wal','-shm','.pyc','.lock','.tmp')) or path.name in {'core.token'}:continue
            seen.add(relative);yield relative,path


def _copy_file(source,destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    with source.open('rb') as stream:header=stream.read(16)
    if header==b'SQLite format 3\0':
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise HistoryError('Повреждена SQLite: '+str(source))
        shutil.copystat(source,destination)
    else:shutil.copy2(source,destination)


def create(*,directory=None,include_history=None):
    value=settings();destination=Path(directory or value['directory']).expanduser().resolve()
    if APP_PATHS.root.is_relative_to(destination) or any(p.is_relative_to(destination) for p in _selected()):
        raise HistoryError('Выберите отдельную папку для архивов, например backups внутри Toshy.')
    protected=[APP_PATHS.sessions,APP_PATHS.root/'modules',APP_PATHS.root/'plugins',APP_PATHS.runtime/'file_history']
    if any(destination.is_relative_to(path) for path in protected):
        raise HistoryError('Папка архивов должна находиться вне сохраняемых данных Toshy.')
    destination.mkdir(parents=True,exist_ok=True)
    name='toshy-backup-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.zip'
    target=destination/name;temporary=target.with_suffix('.partial')
    history=FileHistory()
    with history.locked() as db:
        if any(history._active(db,row['id']) for row in db.execute('SELECT id FROM workspaces').fetchall()):
            raise HistoryError('Перед резервным копированием завершите генерации и сохранения.')
        selected=_selected()
        if value['include_history'] if include_history is None else include_history:selected.append(history.directory)
        manifest=dict(format='toshy-backup-v1',created=time.time(),files={},
                      roots=[p.relative_to(APP_PATHS.root).as_posix() for p in selected
                             if _allowed_root(p.relative_to(APP_PATHS.root).as_posix())],
                      includes_workspaces=False,consistency='SQLite online backup; create while application is idle')
        try:
            with tempfile.TemporaryDirectory(prefix='toshy-backup-') as staging,os.fdopen(create_private(temporary),'w+b') as storage:
                with zipfile.ZipFile(storage,'w',zipfile.ZIP_DEFLATED) as archive:
                    for relative,source in _files(selected,destination):
                        staged=Path(staging)/relative;_copy_file(source,staged)
                        digest=hashlib.sha256()
                        with staged.open('rb') as stream:
                            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
                        manifest['files'][relative]=dict(sha256=digest.hexdigest(),size=staged.stat().st_size)
                        archive.write(staged,relative)
                        staged.unlink()
                    archive.writestr('backup-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
                storage.flush();os.fsync(storage.fileno())
            verify(temporary)
            os.replace(temporary,target)
            value['last_success']=time.time();_save_settings(value)
            # Only prune archives previously produced by Toshy in this folder.
            old=sorted(destination.glob('toshy-backup-*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)
            for path in old[value['keep']:]:
                try:verify(path)
                except (ValueError,OSError,zipfile.BadZipFile):continue
                path.unlink()
            return dict(path=str(target),files=len(manifest['files']),size=target.stat().st_size)
        finally:temporary.unlink(missing_ok=True)


def verify(path):
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist())!=len(set(archive.namelist())):raise HistoryError('Дубликаты имён в архиве.')
        manifest=json.loads(archive.read('backup-manifest.json'))
        if manifest.get('format')!='toshy-backup-v1':raise HistoryError('Неизвестный формат резервной копии.')
        if any(not _allowed_root(root) for root in manifest.get('roots',[])):
            raise HistoryError('Недопустимые корни восстановления.')
        if set(archive.namelist())!={'backup-manifest.json',*manifest['files']}:raise HistoryError('Состав архива не совпадает с описанием.')
        for relative,record in manifest['files'].items():
            if not _allowed(relative):raise HistoryError('Недопустимый путь в резервной копии: '+relative)
            info=archive.getinfo(relative)
            if info.file_size!=record['size']:raise HistoryError('Не совпадает размер: '+relative)
            digest=hashlib.sha256()
            with archive.open(relative) as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
            if digest.hexdigest()!=record['sha256']:raise HistoryError('Не совпадает контрольная сумма: '+relative)
        return manifest


def list_backups():
    directory=Path(settings()['directory'])
    return [dict(path=str(path),size=path.stat().st_size,created=path.stat().st_mtime)
            for path in sorted(directory.glob('toshy-backup-*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)]


def register_process():
    """Offline recovery refuses to run while either UI or core is alive."""
    from release_tools.trial import register
    register()
    with FileHistory().locked(database=False):
        from core.backup_restore import recover_pending
        recover_pending()
        directory=APP_PATHS.runtime/'backup_processes';directory.mkdir(parents=True,exist_ok=True)
        path=directory/(str(os.getpid())+'.pid');path.write_text(str(os.getpid()))
    atexit.register(lambda:path.unlink(missing_ok=True))


def _require_closed():
    for path in (APP_PATHS.runtime/'backup_processes').glob('*.pid'):
        try:pid=int(path.read_text());os.kill(pid,0)
        except (ProcessLookupError,ValueError,FileNotFoundError):continue
        except PermissionError:pass
        if pid!=os.getpid():raise HistoryError('Закройте Toshy и его ядро перед восстановлением данных.')


def restore(path):
    _require_closed()
    from core.backup_restore import recover_pending,restore_archive
    with FileHistory().locked(database=False):recover_pending()
    with tempfile.TemporaryDirectory(prefix='toshy-recovery-') as staging:
        archive_path=Path(staging)/'selected.zip'
        shutil.copy2(path,archive_path)
        manifest=verify(archive_path)
        safety=create(include_history=True)
        try:
            with FileHistory().locked(database=False):
                _require_closed()
                restore_archive(archive_path,manifest)
        except BaseException as error:
            raise HistoryError(f'Restore interrupted: {error}. Safety backup: {safety["path"]}') from error
    return dict(restored=len(manifest['files']),safety=safety['path'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['create','list','verify','restore'])
    parser.add_argument('file',nargs='?')
    args=parser.parse_args()
    if args.action in {'verify','restore'} and not args.file:parser.error('Укажите ZIP резервной копии.')
    if args.action=='restore':result=restore(Path(args.file).resolve())
    elif args.action=='verify':result=verify(args.file)
    elif args.action=='create':
        _require_closed();result=create()
    else:result=list_backups()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
