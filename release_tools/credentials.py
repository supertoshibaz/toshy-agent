"""Ownership receipts for new OS-keyring entries, never secret values."""
import os
from pathlib import Path
import sqlite3


def database(data):
    path = Path(data) / 'runtime/installed-credentials.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.execute('CREATE TABLE IF NOT EXISTS credentials(service TEXT, account TEXT, PRIMARY KEY(service,account))')
    if os.name != 'nt':
        path.chmod(0o600)
    return db


class TrackedKeyring:
    def __init__(self, backend, data):
        self.backend, self.data = backend, data

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def get_password(self, service, account):
        return self.backend.get_password(service, account)

    def set_password(self, service, account, value):
        # Do not claim pre-existing entries belonging to another installation.
        if self.backend.get_password(service, account) is None:
            with database(self.data) as db:
                db.execute('INSERT OR IGNORE INTO credentials VALUES (?,?)', (service, account))
        self.backend.set_password(service, account, value)

    def delete_password(self, service, account):
        result = self.backend.delete_password(service, account)
        with database(self.data) as db:
            db.execute('DELETE FROM credentials WHERE service=? AND account=?', (service, account))
        return result


def track(backend):
    data = os.environ.get('TOSHY_DATA_DIR')
    return TrackedKeyring(backend, data) if data else backend


def clean(data):
    path = Path(data) / 'runtime/installed-credentials.sqlite'
    if not path.exists():
        return
    with database(data) as db:
        entries = list(db.execute('SELECT service,account FROM credentials'))
    if not entries:
        return
    from core.model_connections import _secure_keyring
    import keyring.errors
    backend = _secure_keyring()
    for service, account in entries:
        try:
            backend.delete_password(service, account)
        except keyring.errors.PasswordDeleteError:
            pass
        with database(data) as db:
            db.execute('DELETE FROM credentials WHERE service=? AND account=?', (service, account))
