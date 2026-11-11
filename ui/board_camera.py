"""Камера — состояние интерфейса, а не содержимое версионируемой доски."""
import json
import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from core.paths import APP_PATHS


def _key(path):
    path = Path(path).resolve()
    if path.is_relative_to(APP_PATHS.root):
        return 'app:' + os.path.normcase(str(path.relative_to(APP_PATHS.root)))
    return 'file:' + os.path.normcase(str(path))


def _valid(value):
    if not isinstance(value, dict):return False
    center, zoom = value.get('center'), value.get('zoom')
    return (isinstance(center,list) and len(center)==2
            and all(type(n) in (int,float) and math.isfinite(n) for n in center)
            and type(zoom) in (int,float) and math.isfinite(zoom) and 0.15<=zoom<=3.0)


def load_camera(path):
    database = APP_PATHS.runtime / 'board_cameras.sqlite'
    if not database.is_file():return None
    try:
        with closing(sqlite3.connect(database)) as db:
            row = db.execute('SELECT camera FROM cameras WHERE path=?',(_key(path),)).fetchone()
        value = json.loads(row[0]) if row else None
        return value if _valid(value) else None
    except (OSError, sqlite3.Error, ValueError):
        return None


def save_camera(path, center, zoom):
    value = {'center':list(center),'zoom':zoom}
    if not _valid(value):return
    APP_PATHS.runtime.mkdir(parents=True,exist_ok=True)
    try:
        with closing(sqlite3.connect(APP_PATHS.runtime / 'board_cameras.sqlite',timeout=5)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cameras(path TEXT PRIMARY KEY,camera TEXT NOT NULL)')
            db.execute('INSERT INTO cameras VALUES(?,?) ON CONFLICT(path) DO UPDATE SET camera=excluded.camera',
                       (_key(path),json.dumps(value)))
            db.commit()
    except (OSError,sqlite3.Error) as error:
        from core.logger import core_logger
        core_logger.warn('BOARD_CAMERA',str(error))
