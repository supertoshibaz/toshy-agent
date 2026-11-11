"""Копирование видимого текста с исходными переносами, без мягких переносов окна."""
import html
from PySide6.QtGui import QTextDocument
from ui.views.chat_markdown import split_message
from ui.views.markdown_render import markdown_to_html


def plain_message(source):
    fragments = []
    parts = split_message(source)
    for index, part in enumerate(parts):
        if part.kind == 'code':
            fragments.append('<pre>' + html.escape(part.text) + '</pre>')
        else:
            text = part.text.removesuffix('\n') if index + 1 < len(parts) and parts[index + 1].kind == 'code' else part.text
            rendered = markdown_to_html(text)
            rendered = rendered.replace("<div style='margin:0; padding:0;'><br/></div>",
                                        "<p style='-qt-paragraph-type:empty;'><br/></p>")
            fragments.append(rendered)
    document = QTextDocument()
    document.setHtml(''.join(fragments))
    return document.toPlainText()
