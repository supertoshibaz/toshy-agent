"""Session-owned model usage. Counts come from responses, never from chat text."""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from contextlib import asynccontextmanager
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from core.sqlite_storage import SQLiteStore

_OWNER = uuid.uuid4().hex
_MODEL_KEY = 'token_usage_model'
_LIMITS = {}
_LIMIT_LOCKS = {}
_LIMIT_RUNS = {}
_PURPOSE = ContextVar('model_usage_purpose', default='model')
_OBSERVER = ContextVar('model_usage_observer', default=None)


@contextmanager
def usage_scope(purpose='model', observer=None):
    purpose_token = _PURPOSE.set(purpose)
    observer_token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(observer_token)
        _PURPOSE.reset(purpose_token)


def studio_id():
    from core.log_session import session_log_path
    return hashlib.sha256(str(session_log_path()).encode()).hexdigest()


def integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def positive(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else None


def context_limit(data):
    if not isinstance(data, dict):
        return None
    for name in ('n_ctx', 'context_length', 'max_context_length', 'max_model_len', 'context_window'):
        value = integer(data.get(name))
        if value:
            return value
    for name in ('generation_settings', 'default_generation_settings'):
        child = data.get(name)
        if isinstance(child, dict):
            value = context_limit(child)
            if value:
                return value
    return None


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS model_usage (
        id TEXT PRIMARY KEY, app_id TEXT NOT NULL, owner TEXT NOT NULL,
        status TEXT NOT NULL, started REAL NOT NULL, input_tokens INTEGER,
        output_tokens INTEGER, incomplete INTEGER NOT NULL DEFAULT 0,
        speed REAL, speed_source TEXT, first_token_ms REAL,
        branch TEXT NOT NULL DEFAULT 'main', context_input INTEGER,
        context_output INTEGER, context_tokens INTEGER, subagent_id TEXT NOT NULL DEFAULT '',run_id TEXT NOT NULL DEFAULT '',context_limit INTEGER)''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(model_usage)')}
    for name, definition in (('branch', "TEXT NOT NULL DEFAULT 'main'"),
                             ('context_input', 'INTEGER'), ('context_output', 'INTEGER'),
                             ('context_tokens', 'INTEGER'), ('subagent_id', "TEXT NOT NULL DEFAULT ''"),
                             ('run_id', "TEXT NOT NULL DEFAULT ''"), ('context_limit','INTEGER')):
        if name not in columns:
            db.execute(f'ALTER TABLE model_usage ADD COLUMN {name} {definition}')
    if 'purpose' not in columns:
        db.execute("ALTER TABLE model_usage ADD COLUMN purpose TEXT NOT NULL DEFAULT 'model'")
    db.execute('CREATE INDEX IF NOT EXISTS model_usage_app ON model_usage(app_id,status)')
    db.execute('CREATE INDEX IF NOT EXISTS model_usage_run ON model_usage(run_id,started)')
    db.execute('CREATE INDEX IF NOT EXISTS model_usage_branch ON model_usage(branch,status,started DESC)')


def session_connection(store, branch='main', *, use_saved_default=True):
    """Resolve identity without requiring a credential to view saved statistics."""
    from core.model_connections import ModelConnection, load_settings
    header = store.read_branch_header(branch) or store.read_branch_header('main') or {}
    metadata = header.get('metadata') or {}
    settings = load_settings()
    identity = metadata.get('model_profile_id') or settings.get('selected')
    record = next((x for x in settings.get('connections', []) if x.get('id') == identity), None)
    if use_saved_default and not metadata.get('model_profile_id'):
        # Legacy chats may predate explicit profile selection. A new global
        # default must not relabel or erase the ledger of an unopened chat.
        with store.connection() as db:
            saved = SQLiteStore._state(db, _MODEL_KEY, {})
        if saved.get('model') and saved.get('source'):
            return ModelConnection('local' if saved.get('llama') else 'api', saved['source'],
                                   saved['model'], saved.get('protocol', 'chat_completions'))
    if record is None:
        return None
    mode = record.get('mode', 'api')
    return ModelConnection(mode, str(record.get('base_url') or '').rstrip('/'),
        str(record.get('model') or ''), record.get('protocol', 'chat_completions') if mode == 'api' else 'chat_completions',
        dict(record))


def _prepare(db, connection, *, reset=True):
    state = SQLiteStore._state(db, _MODEL_KEY, {})
    if state.get('fingerprint') != connection.fingerprint:
        if not reset:
            return state or None
        db.execute('DELETE FROM model_usage')
        state = dict(fingerprint=connection.fingerprint, epoch=uuid.uuid4().hex,
                     model=connection.model, source=connection.base_url, protocol=connection.protocol,
                     limit=None, llama=False)
        SQLiteStore._set_state(db, _MODEL_KEY, state)
    return state


def prepare(store, connection, *, recover=False, branch=None, reset=True):
    with store.connection(write=True, create=False) as db:
        _schema(db)
        if branch is not None:
            selected = session_connection(store, branch, use_saved_default=not reset)
            if selected is not None and selected.fingerprint != connection.fingerprint:
                return None
        state = _prepare(db, connection, reset=reset)
        if recover:
            # One core owns the session requests. A different core incarnation
            # cannot finish requests left pending by its predecessor.
            db.execute("UPDATE model_usage SET status='done',incomplete=1 WHERE status='pending' AND owner<>?", (_OWNER,))
        return state


def reset_for_session(manager, session_id, branch='main'):
    store = manager.storage(session_id)
    connection = session_connection(store, branch, use_saved_default=False)
    if connection:
        prepare(store, connection, branch=branch)


def read_usage(store, app_id, branch='main'):
    empty = dict(input=None, output=None, total=None, input_partial=False, output_partial=False, total_partial=False)
    result = dict(current=dict(empty), lifetime=dict(empty), limit=None, model='', source='', speed=None,
                  speed_source='', first_token_ms=None, pending=False, average_speed=None,
                  context=dict(input=None, output=None, total=None))
    try:
        with store.connection() as db:
            state = SQLiteStore._state(db, _MODEL_KEY, {})
            if not state:
                return result
            result.update({key: state.get(key) for key in ('limit', 'model', 'source')})
            for key, where, args in (('lifetime', '', ()), ('current', ' AND app_id=?', (app_id,))):
                row = db.execute("SELECT SUM(input_tokens),SUM(output_tokens),COUNT(*),"
                    "SUM(input_tokens IS NULL OR incomplete=1),SUM(output_tokens IS NULL OR incomplete=1) "
                    "FROM model_usage WHERE status='done'"+where, args).fetchone()
                i, o, count, im, om = row
                result[key] = dict(input=i, output=o, total=(i or 0)+(o or 0) if i is not None or o is not None else None,
                                   input_partial=bool(im), output_partial=bool(om), total_partial=bool(im or om))
            purpose = " AND purpose='model'" if 'purpose' in {r[1] for r in db.execute('PRAGMA table_info(model_usage)')} else ''
            latest = db.execute("SELECT speed,speed_source,first_token_ms FROM model_usage WHERE status='done'"+purpose+" ORDER BY started DESC LIMIT 1").fetchone()
            if latest:
                result.update(speed=latest[0], speed_source=latest[1] or '', first_token_ms=latest[2])
            # Weight completed responses by their generation time rather than
            # averaging fast short replies equally with long slow replies.
            generated, seconds = db.execute("SELECT SUM(output_tokens),SUM(1.0*output_tokens/speed) "
                "FROM model_usage WHERE status='done' AND incomplete=0 AND app_id=? "
                "AND output_tokens>0 AND speed>0"+purpose, (app_id,)).fetchone()
            if generated is not None and positive(seconds):
                result['average_speed'] = generated / seconds
            columns = {row[1] for row in db.execute('PRAGMA table_info(model_usage)')}
            if {'branch', 'context_input', 'context_output', 'context_tokens'} <= columns:
                child_filter = " AND subagent_id=''" if 'subagent_id' in columns else ''
                latest = db.execute("SELECT context_input,context_output,context_tokens,id FROM model_usage "
                    "WHERE status='done' AND branch=?"+child_filter+purpose+" ORDER BY started DESC,rowid DESC LIMIT 1", (branch,)).fetchone()
                if latest:
                    result['context'] = dict(zip(('input', 'output', 'total'), latest[:3]))
                    breakdown = SQLiteStore._state(db, 'context_breakdown:' + branch, {})
                    if breakdown.get('request_id') == latest[3]:
                        result['breakdown'] = breakdown
                compressed = SQLiteStore._state(db,'compression:'+branch+':',{})
                if compressed.get('status') == 'completed':
                    last = db.execute("SELECT started FROM model_usage WHERE status='done' AND branch=?"+child_filter+purpose+
                                      " ORDER BY started DESC,rowid DESC LIMIT 1",(branch,)).fetchone()
                    if not last or last[0] < compressed.get('updated',0):
                        result['context'] = dict(input=None,output=None,total=None)
                        result.pop('breakdown', None)
            result['pending'] = bool(db.execute("SELECT 1 FROM model_usage WHERE status='pending' LIMIT 1").fetchone())
            return result
    except FileNotFoundError:
        return result


async def discover_limit(connection, llama=False, *, force=False):
    """Only explicit server metadata. No model-name tables or inferred limits."""
    key = (connection.fingerprint, connection.mode == 'local' or bool(llama))
    cached = _LIMITS.get(key)
    if not force and cached and cached[0] > time.monotonic():
        return cached[1]
    lock = _LIMIT_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _LIMITS.get(key)
        if not force and cached and cached[0] > time.monotonic():
            return cached[1]
        limit = None
        try:
            if connection.mode == 'api' and not connection.key:
                from dataclasses import replace
                from core.model_connections import resolve_key
                key_value = await asyncio.to_thread(resolve_key, connection.options)
                connection = replace(connection, key=key_value)
            async with httpx.AsyncClient(timeout=httpx.Timeout(2, connect=1), headers=connection.headers(),
                    trust_env=connection.options.get('trust_env', True)) as http:
                if connection.mode == 'local' or llama:
                    url = connection.base_url.removesuffix('/v1') + '/props'
                    response = await http.get(url)
                    if response.is_success:
                        limit = context_limit(response.json())
                if limit is None:
                    response = await http.get(connection.base_url + '/models')
                    if response.is_success:
                        records = response.json().get('data') or []
                        for record in records:
                            if record.get('id') == connection.model:
                                limit = context_limit(record)
                                break
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        # UI polling never probes the model. Explicit inspection/new requests
        # can bypass this cache when a server restarts with a different n_ctx.
        ttl = 60 if limit is not None else 30
        _LIMITS[key] = (time.monotonic() + ttl, limit)
        return limit


async def usage_snapshot(manager, session_id, branch='main', app_id=None, *, refresh_model=False):
    store = manager.storage(session_id)
    connection = await asyncio.to_thread(session_connection, store, branch)
    if connection:
        state = await asyncio.to_thread(prepare, store, connection, recover=True, branch=branch, reset=False)
        matching = state and state.get('fingerprint') == connection.fingerprint
        limit = await discover_limit(connection, state.get('llama', False), force=True) if matching and refresh_model else None
        if limit is not None:
            def save_limit():
                with store.connection(write=True, create=False) as db:
                    latest = SQLiteStore._state(db, _MODEL_KEY, {})
                    if latest.get('epoch') == state['epoch'] and latest.get('limit') != limit:
                        latest['limit'] = limit
                        SQLiteStore._set_state(db, _MODEL_KEY, latest)
            await asyncio.to_thread(save_limit)
    return await asyncio.to_thread(read_usage, store, app_id or studio_id(), branch)


class UsageMeter:
    def __init__(self, connection, session_id=None, *, streaming=True):
        from core.context import get_execution_context, sanitize_session_id
        from core.paths import APP_PATHS
        context = get_execution_context()
        self.connection = connection
        self.session_id = session_id or context.session_id
        self.store = SQLiteStore(APP_PATHS.sessions/sanitize_session_id(self.session_id)/'session.sqlite') if self.session_id else None
        self.branch = context.branch or 'main'
        self.subagent_id = context.subagent_id
        self.parent_run_id = context.parent_run_id
        self.run_id = context.run_id
        self.id, self.app_id = uuid.uuid4().hex, studio_id()
        self.purpose, self.observer = _PURPOSE.get(), _OBSERVER.get()
        self.epoch = None
        self.request_description = None
        self.usage, self.timings = {}, {}
        self.limit = None
        self.streaming, self.complete = streaming, False
        self.rejected = False
        self.started = self.first = self.last = None

    def begin(self):
        if self.store is None or not self.store.path.exists():
            return
        with self.store.connection(write=True, create=False) as db:
            _schema(db)
            selected = session_connection(self.store, self.branch, use_saved_default=False)
            # Validate under the write lock: a concurrent profile change must
            # never be undone by a request that captured the former profile.
            if self.purpose == 'model' and not self.subagent_id and selected and selected.fingerprint != self.connection.fingerprint:
                return
            if self.subagent_id:
                if selected is None:
                    return
                parent = db.execute("SELECT json_extract(payload,'$.model_fingerprint') FROM runs WHERE id=?", (self.parent_run_id,)).fetchone()
                if parent and parent[0] and parent[0] != selected.fingerprint:
                    return  # The parent run belongs to a discarded model ledger.
            state = _prepare(db, selected or self.connection)
            self.epoch = state['epoch']
            db.execute('INSERT INTO model_usage(id,app_id,owner,status,started,branch,subagent_id,run_id,purpose) VALUES (?,?,?,?,?,?,?,?,?)',
                       (self.id, self.app_id, _OWNER, 'pending', time.time(), self.branch, self.subagent_id, self.run_id, self.purpose))

    def observe(self, event):
        response = event.get('response') or event.get('message') or {}
        if not isinstance(response, dict):
            response = {}
        usage = event.get('usage') or response.get('usage')
        gemini = event.get('usageMetadata')
        if isinstance(gemini, dict):
            usage = {
                'prompt_tokens': gemini.get('promptTokenCount'),
                'completion_tokens': (gemini.get('candidatesTokenCount', 0) + gemini.get('thoughtsTokenCount', 0)),
                'prompt_tokens_details': {'cached_tokens': gemini.get('cachedContentTokenCount', 0)},
            }
        if isinstance(usage, dict):
            # Streaming snapshots/deltas can repeat cumulative counters. Keep
            # the latest values; never add snapshots to one another.
            for key, value in usage.items():
                if value is not None:
                    if isinstance(value, dict) and isinstance(self.usage.get(key), dict):
                        self.usage[key].update(value)
                    else:
                        self.usage[key] = value
        if usage and self.connection.protocol == 'chat_completions' and event.get('choices') == []:
            # Standard final usage-only chunk. Its counts stay valid even if
            # the connection later stalls before the optional [DONE] marker.
            self.complete = True
        if isinstance(event.get('timings'), dict):
            self.timings.update(event['timings'])
        limit = context_limit(event) or context_limit(response)
        if limit is not None:
            self.limit = limit
        if self.streaming:
            text = event.get('delta')
            has_output = bool(text) if isinstance(text, str) else bool(isinstance(text, dict) and any(text.get(k) for k in ('text','thinking','partial_json')))
            has_output |= bool(event.get('content'))
            for choice in event.get('choices') or []:
                delta = choice.get('delta') or {}
                has_output |= any(bool(delta.get(k)) for k in ('content','reasoning','reasoning_content','tool_calls'))
            has_output |= any(c.get('content', {}).get('parts') for c in event.get('candidates', []))
            if has_output:
                self.last = time.perf_counter()
                if self.first is None:
                    self.first = self.last

    def counts(self):
        usage, timings = self.usage, self.timings
        i = integer(usage.get('prompt_tokens', usage.get('input_tokens')))
        o = integer(usage.get('completion_tokens', usage.get('output_tokens')))
        if self.connection.protocol != 'anthropic_messages' and i is not None:
            details = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
            if not isinstance(details, dict):
                details = {}
            cached = integer(details.get('cached_tokens'))
            if cached is None:
                cached = integer(timings.get('cache_n'))
            if cached is not None:
                i = max(0, i-cached)
        if i is None:
            i = integer(timings.get('prompt_n'))
        if o is None:
            o = integer(timings.get('predicted_n'))
        return i, o

    def context_counts(self):
        """Last completed request's input (including cache) plus its output."""
        if not self.complete:
            return None, None, None
        usage, timings = self.usage, self.timings
        i = integer(usage.get('prompt_tokens', usage.get('input_tokens')))
        o = integer(usage.get('completion_tokens', usage.get('output_tokens')))
        if i is not None and self.connection.protocol == 'anthropic_messages':
            # This protocol reports fresh input separately from cache reads
            # and writes, unlike OpenAI-compatible prompt_tokens.
            for key in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
                i += integer(usage.get(key)) or 0
        if i is None:
            prompt, cached = integer(timings.get('prompt_n')), integer(timings.get('cache_n'))
            if prompt is not None and cached is not None:
                i = prompt + cached
        if o is None:
            o = integer(timings.get('predicted_n'))
        total = i + o if i is not None and o is not None else integer(usage.get('total_tokens'))
        return i, o, total

    def finish(self, error=None):
        if self.epoch is None:
            return
        i, o = self.counts()
        context_i, context_o, context_total = self.context_counts()
        seen = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            nested = error.__cause__ or error.__context__
            if nested is None:
                break
            error = nested
        rejected = self.rejected or isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        if isinstance(error, httpx.HTTPStatusError):
            rejected |= 400 <= error.response.status_code < 500
        rejected |= bool(getattr(error, 'usage_rejected', False))
        speed = positive(self.timings.get('predicted_per_second'))
        speed_source = 'llama.cpp' if speed else ''
        if speed is None and o is not None and o > 1 and self.first and self.last and self.last > self.first:
            speed = (o-1)/(self.last-self.first)
            speed_source = 'расчёт по потоку'
        ttft = (self.first-self.started)*1000 if self.first is not None and self.started is not None else None
        with self.store.connection(write=True, create=False) as db:
            state = SQLiteStore._state(db, _MODEL_KEY, {})
            if state.get('epoch') != self.epoch:
                return
            if rejected and not self.usage and not self.timings:
                db.execute('DELETE FROM model_usage WHERE id=?', (self.id,))
                return
            db.execute("UPDATE model_usage SET status='done',input_tokens=?,output_tokens=?,incomplete=?,speed=?,speed_source=?,first_token_ms=?,"
                "context_input=?,context_output=?,context_tokens=?,context_limit=? WHERE id=? AND status='pending'",
                (i,o,int(not self.complete),speed,speed_source,ttft,context_i,context_o,context_total,self.limit,self.id))
            if self.limit is not None and not self.subagent_id and self.purpose == 'model':
                state['limit'] = self.limit
            if self.purpose == 'model' and not self.subagent_id and 'prompt_n' in self.timings and 'predicted_n' in self.timings:
                state['llama'] = True
            SQLiteStore._set_state(db, _MODEL_KEY, state)
            if self.request_description is not None and self.complete and self.purpose == 'model' and not self.subagent_id:
                from core.context_breakdown import allocate
                breakdown = allocate(self.request_description, context_i, context_o)
                if breakdown is not None:
                    breakdown.update(request_id=self.id, run_id=self.run_id)
                    SQLiteStore._set_state(db, 'context_breakdown:' + self.branch, breakdown)


@asynccontextmanager
async def usage_request(connection, session_id=None, *, streaming=True, messages=None, tools=None):
    meter = UsageMeter(connection, session_id, streaming=streaming)
    if messages is not None:
        try:
            from core.context_breakdown import describe_request
            meter.request_description = describe_request(messages, tools)
        except Exception:
            pass  # Optional estimates do not participate in model execution.
    # Statistics failures must not discard model output or repeat a request.
    try:
        await asyncio.to_thread(meter.begin)
    except Exception:
        meter.epoch = None
    if meter.epoch is not None and meter.purpose == 'model' and not meter.subagent_id:
        # Revalidate metadata when starting real work, not on every UI tick.
        try:
            key = connection.fingerprint
            meter.limit = await discover_limit(connection, force=_LIMIT_RUNS.get(key) != meter.run_id)
            _LIMIT_RUNS[key] = meter.run_id
        except Exception:
            pass  # Optional metadata must never discard a model request.
    meter.started = time.perf_counter()
    error = None
    try:
        yield meter
    except BaseException as caught:
        error = caught
        raise
    finally:
        if meter.observer is not None and meter.purpose == 'model':
            try:
                meter.observer(meter)
            except Exception:
                pass
        try:
            await asyncio.shield(asyncio.to_thread(meter.finish, error))
        except Exception:
            pass
