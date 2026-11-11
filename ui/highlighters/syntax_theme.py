from ui.themes.manager import theme_manager
from typing import Optional
from pygments.token import Token
from PySide6.QtGui import QTextCharFormat, QColor, QFont


def _make_fmt(color_hex: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color_hex))
    if bold:
        fmt.setFontWeight(QFont.Bold)
    if italic:
        fmt.setFontItalic(True)
    return fmt


# Единая палитра для ВСЕХ языков программирования (под графитовую тему)
TOKEN_FORMATS = {
    # Ключевые слова (def, class, import, return, if, for и др.) - мягкий фиолетовый
    Token.Keyword: _make_fmt("#c084fc", bold=True),
    Token.Keyword.Constant: _make_fmt("#38bdf8", bold=True),
    Token.Keyword.Type: _make_fmt("#67e8f9"),

    # Функции и методы - теплый золотистый
    Token.Name.Function: _make_fmt("#facc15"),
    Token.Name.Function.Magic: _make_fmt("#facc15", bold=True),

    # Классы - бирюзовый
    Token.Name.Class: _make_fmt("#67e8f9", bold=True),

    # Декораторы (@decorator) - оранжевый
    Token.Name.Decorator: _make_fmt("#fb923c"),

    # Встроенные имена и типы (self, True, False, print, len, int) - небесно-голубой
    Token.Name.Builtin: _make_fmt("#38bdf8"),
    Token.Name.Builtin.Pseudo: _make_fmt("#38bdf8", italic=True),

    # Строки (любые кавычки, f-строки) - мягкий зеленый
    Token.String: _make_fmt("#86efac"),
    Token.String.Doc: _make_fmt("#71717a", italic=True),  # Докстринги как серые комментарии

    # Числа - коралловый
    Token.Number: _make_fmt("#fb7185"),

    # Комментарии - приглушенный серый курсив
    Token.Comment: _make_fmt("#4ade80", italic=True),

    # Операторы (+, -, ==, !=) и знаки препинания
    Token.Operator: _make_fmt("#e4e4e7"),
    Token.Punctuation: _make_fmt("#a1a1aa"),
}


LIGHT_TOKEN_FORMATS = {
    Token.Keyword: _make_fmt('#7c3aad', bold=True),
    Token.Keyword.Constant: _make_fmt('#006fa6', bold=True),
    Token.Keyword.Type: _make_fmt('#007b80'),
    Token.Name.Function: _make_fmt('#8a5900'),
    Token.Name.Function.Magic: _make_fmt('#8a5900', bold=True),
    Token.Name.Class: _make_fmt('#007b80', bold=True),
    Token.Name.Decorator: _make_fmt('#a34d00'),
    Token.Name.Builtin: _make_fmt('#006fa6'),
    Token.Name.Builtin.Pseudo: _make_fmt('#006fa6', italic=True),
    Token.String: _make_fmt('#24713b'),
    Token.String.Doc: _make_fmt('#656661', italic=True),
    Token.Number: _make_fmt('#ae3758'),
    Token.Comment: _make_fmt('#387545', italic=True),
    Token.Operator: _make_fmt('#292b2c'),
    Token.Punctuation: _make_fmt('#656661'),
}


def get_format_for_token(token_type: Token) -> Optional[QTextCharFormat]:
    """Рекурсивно подбирает формат, поднимаясь по иерархии токена Pygments."""
    formats = LIGHT_TOKEN_FORMATS if theme_manager.current['foundation'] == 'light' else TOKEN_FORMATS
    curr = token_type
    while curr:
        if curr in formats:
            return formats[curr]
        curr = curr.parent
    return None
