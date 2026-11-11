"""UI provenance for built-in read/search tools. Never append this to prompts."""
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def tool_sources(name, args, output, workspace):
    text = str(output or '')
    args = args or {}
    sources = []

    def site(url, title='', recursive=False):
        parsed = urlsplit(str(url))
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            return
        address = urlunsplit((parsed.scheme, parsed.netloc, '/' if recursive else parsed.path,
                              '' if recursive else parsed.query, ''))
        sources.append({'kind':'site', 'url':address, 'label':title or address,
                        'recursive':recursive, 'tool':name})

    if name == 'read_url' and 'saved to temporary memory ID:' in text:
        final = re.search(r'\((https?://[^\s]+)\) saved to temporary memory ID:', text)
        site(final.group(1) if final else args.get('url', ''))
    elif name == 'deep_search' and (re.search(r'\bSaved [1-9]\d* page\(s\)', text)
                                     or 'System: Deep crawl completed!' in text):
        site(args.get('root_url', ''), recursive=True)
    return sources
