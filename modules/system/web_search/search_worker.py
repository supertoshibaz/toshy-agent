"""Disposable fallback provider process. Proxy credentials travel over stdin only."""
import contextlib
import json
import os
import re
import sys


def main():
    payload=json.load(sys.stdin)
    failures=[];rows=[]
    if payload['manual']:
        for key in list(os.environ):
            if key.lower().endswith('_proxy'):os.environ.pop(key,None)
    backend=payload['backend']
    if backend not in {'brave','yahoo'}:raise ValueError('Unsupported search provider.')
    with contextlib.redirect_stdout(sys.stderr):
        from ddgs import DDGS
        for backend in (backend,):
            try:
                with DDGS(proxy=payload['proxy'],timeout=8) as client:
                    if payload['manual'] and payload['proxy'] is None:
                        if not hasattr(client,'_proxy'):raise RuntimeError('Direct route is unavailable.')
                        client._proxy=None
                    rows=list(client.text(payload['query'],max_results=payload['limit'],backend=backend))
                if rows:
                    rows=[{**row,'engine':backend} for row in rows]
                    break
                failures.append(backend+': empty response')
            except Exception as error:
                # Provider exceptions can contain proxy passwords. Return only
                # sanitized diagnostics; stderr is never exposed as a tool result.
                detail=('Provider request failed through the configured proxy.' if payload['proxy'] else
                        re.sub(r'(?i)\b(https?|socks5h?)://[^/\s@]+@',r'\1://***@',str(error)))
                failures.append(backend+': '+type(error).__name__+': '+detail[:800])
    json.dump({'rows':rows,'error':None if rows else 'Fallback search providers unavailable. '+'; '.join(failures)},sys.stdout)


if __name__=='__main__':main()
