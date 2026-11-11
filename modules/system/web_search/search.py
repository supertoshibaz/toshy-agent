"""Bounded DuckDuckGo HTML search with explicit refusal diagnostics."""
import asyncio
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .network import create_web_session, NetworkConfigurationError, safe_error

_lock = threading.Lock()
_routes = {}
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Fetch-Mode': 'navigate',
}


class SearchBlocked(NetworkConfigurationError):
    pass


def parse_results(html, endpoint, limit):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select_one('form[action*="anomaly"], #challenge-form, .anomaly-modal') or 'anomaly.js' in html:
        raise SearchBlocked('DuckDuckGo returned an anti-bot challenge. Search is temporarily unavailable; '
                            'do not repeat equivalent queries. Try later or read a known source URL.')
    rows, seen = [], set()
    links = soup.select('a.result__a, a.result-link, div.body > a:has(h2)')
    for link in links:
        href = urljoin(endpoint, link.get('href', ''))
        parsed = urlsplit(href)
        if parsed.hostname and parsed.hostname.removeprefix('www.') == 'duckduckgo.com':
            href = parse_qs(parsed.query).get('uddg', [''])[0]
            parsed = urlsplit(href)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or href in seen:continue
        heading = link.select_one('h2')
        title = (heading or link).get_text(' ', strip=True)
        if not title:continue
        container = link.find_parent(class_='result')
        snippet = container.select_one('.result__snippet') if container else None
        if snippet is None:
            tr = link.find_parent('tr')
            following = tr.find_next_sibling('tr') if tr else None
            snippet = following.select_one('.result-snippet') if following else None
        if snippet is None and heading is not None:
            # Newer DDG markup places the heading and snippet in the same link.
            body = link.get_text(' ', strip=True).removeprefix(title).strip()
        else:body = snippet.get_text(' ', strip=True) if snippet else ''
        seen.add(href)
        rows.append({'title': title, 'href': href, 'body': body, 'engine': 'duckduckgo'})
        if len(rows) >= limit:break
    if rows:return rows
    if soup.select_one('.no-results, .no-results__message') or 'no more results' in soup.get_text(' ', strip=True).lower():
        return []
    raise NetworkConfigurationError('DuckDuckGo returned no recognizable search results. '
                                    'The response may be a refusal or its markup may have changed; this does not prove that no sources exist.')


async def _fetch(query, options, limit):
    errors = []
    async with create_web_session(options, headers=_HEADERS) as session:
        for endpoint in ('https://html.duckduckgo.com/html/', 'https://lite.duckduckgo.com/lite/'):
            url = endpoint + '?' + urlencode({'q': query, 'kl': 'wt-wt'})
            try:
                response = await session.get(url, timeout=12, max_bytes=2 * 1024 * 1024)
                status = response.status_code
                if status in {202, 403, 429}:
                    raise SearchBlocked(f'DuckDuckGo refused automated search (HTTP {status}). '
                                        'Do not repeat equivalent queries; try later or read a known source URL.')
                if status != 200:
                    raise NetworkConfigurationError(f'DuckDuckGo search returned HTTP {status}.')
                return parse_results(response.text, endpoint, limit)
            except SearchBlocked:
                raise
            except Exception as error:
                errors.append(error)
    # Keep the real network/proxy exception type for the existing safe_error
    # redaction; never expose credentials through a combined error string.
    raise errors[-1]


async def _fallback_async(query, options, limit):
    import json
    from pathlib import Path
    from core.process_owner import run_json_worker
    failures=[]
    for backend in ('brave','yahoo'):
        try:
            status,stdout,_=await run_json_worker(Path(__file__).with_name('search_worker.py'),
                {'query':query,'limit':limit,'proxy':options.proxy_for('https://duckduckgo.com/'),
                 'manual':options.manual,'backend':backend},timeout=10,max_output=2*1024*1024,isolated=False)
            if status:raise NetworkConfigurationError('Provider worker failed.')
            result=json.loads(stdout)
            if result.get('rows'):return result['rows']
            failures.append(result.get('error') or backend+': empty response')
        except asyncio.TimeoutError:failures.append(backend+': request timed out')
        except Exception as error:failures.append(backend+': '+safe_error(error,options.uses_proxy))
    raise NetworkConfigurationError('Fallback search providers unavailable. '+'; '.join(failures))


async def async_search(query, options, max_results=8):
    if not isinstance(query,str) or not query.strip() or len(query)>2000:
        raise ValueError('Provide a nonempty search query of at most 2000 characters.')
    route=(options.http_proxy,options.https_proxy,options.manual)
    with _lock:
        if route not in _routes:
            if len(_routes)>=64:
                idle=next((key for key,value in _routes.items() if value['users']==0 and not value['lock'].locked()),None)
                if idle is None:raise NetworkConfigurationError('Too many active search routes.')
                _routes.pop(idle)
            _routes[route]={'lock':threading.Lock(),'last':0,'ddg_until':0,'blocked_until':0,'users':0}
        state=_routes[route];state['users']+=1
    acquired=False
    try:
        # Thread locks support both the synchronous compatibility API and callers
        # in different asyncio loops. Network work never holds the global lock.
        while not state['lock'].acquire(blocking=False):await asyncio.sleep(.025)
        acquired=True
        now=time.monotonic()
        if now<state['blocked_until']:
            raise SearchBlocked('Search providers recently refused this route. '
                                f"Wait {max(1,round(state['blocked_until']-now))} seconds.")
        await asyncio.sleep(max(0,.8-(now-state['last'])))
        limit=max(1,min(50,int(max_results)))
        primary_error='DuckDuckGo is cooling down after a refusal.'
        async with asyncio.timeout(45):
            if time.monotonic()>=state['ddg_until']:
                try:return await _fetch(query,options,limit)
                except Exception as error:
                    primary_error=type(error).__name__+': '+safe_error(error,options.uses_proxy)
                    if isinstance(error,SearchBlocked):state['ddg_until']=time.monotonic()+30
            try:return await _fallback_async(query,options,limit)
            except Exception as error:
                state['blocked_until']=time.monotonic()+30
                raise NetworkConfigurationError(primary_error+'\n'+safe_error(error,options.uses_proxy)
                    +'\nTry later or read a known source URL.') from error
    finally:
        if acquired:
            state['last']=time.monotonic();state['lock'].release()
        with _lock:state['users']-=1


def search(query, options, max_results=8):
    """Synchronous compatibility API; async tools must await async_search."""
    return asyncio.run(async_search(query,options,max_results))
