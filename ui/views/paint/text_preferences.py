"""Portable defaults for new vector text objects."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor,QFont,QTextCharFormat,QTextBlockFormat

def capture_defaults(canvas):
    char=getattr(canvas,'_text_default_char',None);block=getattr(canvas,'_text_default_block',None)
    if char is None:return {}
    return {'font':char.font().toString(),'color':char.foreground().color().name(QColor.HexArgb),
            'alignment':int(block.alignment()) if block is not None else int(Qt.AlignLeft)}

def restore_defaults(canvas,data):
    if not isinstance(data,dict) or not data.get('font'):return
    font=QFont()
    if not font.fromString(data['font']):return
    char=QTextCharFormat();char.setFont(font);color=QColor(data.get('color') or '#000000')
    if color.isValid():char.setForeground(color)
    block=QTextBlockFormat();block.setAlignment(Qt.AlignmentFlag(int(data.get('alignment',int(Qt.AlignLeft)))))
    canvas._text_default_char=char;canvas._text_default_block=block
