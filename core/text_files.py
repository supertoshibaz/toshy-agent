"""Lossless text decoding; unknown encodings must never become writable replacement text."""
import codecs
from dataclasses import dataclass


@dataclass(frozen=True)
class TextFormat:
    encoding: str = 'utf-8'
    bom: bytes = b''
    newline: str = '\n'

    def encode(self, text):
        return self.bom + text.replace('\n', self.newline).encode(self.encoding, errors='strict')


def decode_text(raw):
    encoding, bom = 'utf-8', b''
    for marker, codec in ((codecs.BOM_UTF32_LE, 'utf-32-le'), (codecs.BOM_UTF32_BE, 'utf-32-be'),
                          (codecs.BOM_UTF8, 'utf-8'), (codecs.BOM_UTF16_LE, 'utf-16-le'),
                          (codecs.BOM_UTF16_BE, 'utf-16-be')):
        if raw.startswith(marker):
            encoding, bom = codec, marker
            break
    text = raw[len(bom):].decode(encoding, errors='strict')
    newline = '\r\n' if '\r\n' in text else '\r' if '\r' in text else '\n'
    return text.replace('\r\n', '\n').replace('\r', '\n'), TextFormat(encoding, bom, newline)
