"""Line-based fenced blocks, including the unfinished tail of a streamed reply."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class MessagePart:
    kind: str
    text: str
    language: str = ''
    closed: bool = True
    paragraph_gap: bool = False


@dataclass(frozen=True)
class FencedBlock:
    start: int
    end: int
    language: str
    code: str
    closed: bool


def fenced_blocks(markdown: str) -> list[FencedBlock]:
    lines = markdown.replace('\r\n', '\n').replace('\r', '\n').splitlines(keepends=True)
    blocks = []
    i = 0
    while i < len(lines):
        opening = re.fullmatch(r' {0,3}(`{3,}|~{3,})([^\n]*)\n?', lines[i])
        if not opening or (opening[1][0] == '`' and '`' in opening[2]):
            i += 1
            continue
        start = i
        fence, info = opening[1], opening[2].strip()
        language = info.split()[0] if info else ''
        closing = re.compile(r' {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*\n?')
        i += 1
        body = []
        while i < len(lines) and not closing.fullmatch(lines[i]):
            body.append(lines[i])
            i += 1
        closed = i < len(lines)
        code = ''.join(body)
        if closed:
            code = code.removesuffix('\n')  # The newline before the fence is structural.
            i += 1
        blocks.append(FencedBlock(start, i - 1, language, code, closed))
    return blocks


def split_message(markdown: str, *, chunk_prose=False) -> list[MessagePart]:
    text = markdown.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.splitlines(keepends=True)
    parts, before = [], 0
    for block in fenced_blocks(text):
        if block.start > before:
            parts.extend(prose_parts(''.join(lines[before:block.start])) if chunk_prose else [MessagePart('text', ''.join(lines[before:block.start]))])
        parts.append(MessagePart('code', block.code, block.language, block.closed))
        before = block.end + 1
    if before < len(lines):
        parts.extend(prose_parts(''.join(lines[before:])) if chunk_prose else [MessagePart('text', ''.join(lines[before:]))])
    return parts


def prose_parts(text):
    # Freeze every completed paragraph. Rebuilding a multi-paragraph QLabel
    # for each token could change its internal line layout above the new text.
    start = 0
    gap = False
    for boundary in re.finditer(r'\n[ \t]*\n(?:[ \t]*\n)*', text):
        end = boundary.end()
        yield MessagePart('text', text[start:end], paragraph_gap=gap)
        start, gap = end, True
    if start < len(text):
        yield MessagePart('text', text[start:], paragraph_gap=gap)
