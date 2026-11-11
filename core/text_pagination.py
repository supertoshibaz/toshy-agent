"""Text pages extend to a newline, with a bounded allowance."""


def paginate_text(content: str, min_chars: int = 1000, hard_limit: int = 2500) -> list[str]:
    if min_chars < 1 or hard_limit < min_chars:
        raise ValueError('Размер страницы должен быть положительным, предел — не меньше размера')
    pages = []
    pos = 0
    while pos < len(content):
        target = pos + min_chars
        if target >= len(content):
            end = len(content)
        else:
            newline = content.find('\n', target)
            space = content.find(' ', target)
            if newline != -1 and newline - pos <= hard_limit:
                end = newline + 1
            elif space != -1 and space - pos <= hard_limit:
                end = space + 1
            else:
                end = min(pos + hard_limit, len(content))
        text = content[pos:end].strip()
        if text:
            pages.append(text)
        pos = end
    return pages
