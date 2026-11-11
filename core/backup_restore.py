"""Journaled offline restore: retain originals until commit and roll back on failure."""
import json
import os
from pathlib import Path
import re
import shutil
import uuid
import zipfile
from core.paths import APP_PATHS
from core.private_files import atomic_private,private_directory,protect_private,protect_directory


def _journal():return APP_PATHS.runtime/'backup_restore.json'


def _save(value):atomic_private(_journal(),json.dumps(value).encode('utf-8'))


def _remove(path):
    if path.is_symlink() or path.is_file():path.unlink()
    elif path.exists():shutil.rmtree(path)


def _sync(path):
    if os.name=='nt':return
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def _target(relative):
    target=APP_PATHS.root/relative
    for part in [target,*target.parents]:
        if not part.is_relative_to(APP_PATHS.root):continue
        try:info=part.lstat()
        except FileNotFoundError:continue
        if part.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('Restore paths cannot contain links or reparse points.')
    return target


def _rollback(value):
    transaction=APP_PATHS.root/value['directory']
    for item in reversed(value['entries']):
        if not item.get('started'):continue
        target=_target(item['path']);old=transaction/'old'/item['path']
        if old.exists():
            _remove(target);target.parent.mkdir(parents=True,exist_ok=True)
            os.replace(old,target);_sync(target.parent)
        elif not item['existed']:
            _remove(target)
    value['phase']='rolled_back';_save(value)


def _cleanup(value):
    _remove(APP_PATHS.root/value['directory'])
    _journal().unlink(missing_ok=True);_sync(_journal().parent)


def recover_pending():
    """Call under the file-history process lock before registering UI/core."""
    try:
        protect_private(_journal())
        value=json.loads(_journal().read_text())
    except FileNotFoundError:return
    from core.backups import _allowed,_allowed_root,_require_closed
    if not re.fullmatch(r'\.restore-[a-f0-9]{32}',value.get('directory','')):raise ValueError('Invalid restore journal directory.')
    if value.get('phase') not in {'applying','committed','rolled_back'}:raise ValueError('Invalid restore journal phase.')
    for item in value['entries']:
        relative=item['path'];base=relative[:-4] if relative.endswith(('-wal','-shm')) else relative
        if not (_allowed(base) or _allowed_root(base)) or relative=='runtime/file_history/history.lock':
            raise ValueError('Invalid restore journal path.')
    if value['phase']=='applying':
        _require_closed();_rollback(value)
    try:_cleanup(value)
    except OSError as error:
        from core.logger import core_logger
        core_logger.warn('BACKUP_RESTORE_CLEANUP',str(error))


def restore_archive(archive_path,manifest):
    directory=private_directory(APP_PATHS.root/('.restore-'+uuid.uuid4().hex))
    new=directory/'new';new.mkdir();roots=manifest.get('roots',[])
    value=None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for relative in manifest['files']:
                _target(relative)
                target=new/relative;target.parent.mkdir(parents=True,exist_ok=True)
                with archive.open(relative) as source,target.open('xb') as output:
                    shutil.copyfileobj(source,output);output.flush();os.fsync(output.fileno())
                os.chmod(target,(archive.getinfo(relative).external_attr>>16)&0o700 or 0o600)
                if os.name=='nt':protect_private(target)
        for parent in [new,*[p for p in new.rglob('*') if p.is_dir()]]:protect_directory(parent)
        units=set()
        for relative in roots:
            _target(relative)
            if relative=='runtime/file_history':
                incoming=new/relative;current=APP_PATHS.root/relative
                children={p.name for parent in (incoming,current) if parent.is_dir() for p in parent.iterdir()}
                units.update(relative+'/'+name for name in children if name!='history.lock')
            else:units.add(relative)
        for relative in manifest['files']:
            if not any(Path(relative).is_relative_to(root) for root in roots):units.add(relative)
        for relative in list(units):
            incoming=new/relative
            if incoming.is_file():
                with incoming.open('rb') as stream:is_sqlite=stream.read(16)==b'SQLite format 3\0'
                if is_sqlite:units.update((relative+'-wal',relative+'-shm'))
        value={'directory':directory.name,'phase':'applying','entries':[
            {'path':relative,'existed':_target(relative).exists(),'started':False} for relative in sorted(units)]}
        _save(value)
        for item in value['entries']:
            target=_target(item['path']);incoming=new/item['path'];old=directory/'old'/item['path']
            item['started']=True;_save(value)
            if target.exists():
                old.parent.mkdir(parents=True,exist_ok=True);os.replace(target,old);_sync(target.parent);_sync(old.parent)
            if incoming.exists():
                target.parent.mkdir(parents=True,exist_ok=True);os.replace(incoming,target);_sync(target.parent)
        value['phase']='committed';_save(value)
    except BaseException:
        if value is not None:
            try:_rollback(value)
            except BaseException:raise  # Keep originals and journal for the next offline recovery.
        if value is None or value['phase']=='rolled_back':
            _remove(directory);_journal().unlink(missing_ok=True)
        raise
    else:
        # Cleanup failure must not turn a committed restore into a false failure.
        try:_cleanup(value)
        except OSError as error:
            from core.logger import core_logger
            core_logger.warn('BACKUP_RESTORE_CLEANUP',str(error))
