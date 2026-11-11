"""Workspace image copies and real widgets embedded in Markdown text flow."""
from ui.styles.binding import apply_style, watch_paint
from ui.themes.manager import theme_manager
from ui.i18n import bind_caption, tr, set_text
from ui.icons import set_icon

import math
import os
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import unquote

from PySide6.QtCore import QObject, Signal, QEvent, Qt, QTimer, QSize, QSizeF, QRect, QRectF, QUrl
from PySide6.QtGui import QIcon, QPixmap, QImage, QPainter, QColor, QPen, QLinearGradient, QTextCursor, QTextCharFormat, QTextBlockFormat, QTextFormat, QTextObjectInterface
try:
    from PySide6.QtGui import QPyTextObject
except ImportError:
    class QPyTextObject(QObject, QTextObjectInterface):
        pass
from PySide6.QtWidgets import QApplication, QWidget, QToolButton, QPlainTextEdit, QHBoxLayout
from ui.app_state import app_state
from ui.event_bus import event_bus

IMAGE_RE = re.compile(r'^\s*!\[([^\]]*)\]\((?:<([^>]+)>|([^\)]+))\)\s*(?:<!--\s*toshy-image-width:(original|quarter|half|full)\s*-->)?\s*$')
IMAGE_OBJECT = QTextFormat.UserObject.value + 38
IMAGE_SOURCE = QTextFormat.UserProperty.value + 38
IMAGE_PADDING = QTextFormat.UserProperty.value + 39


def image_reference(line):
    match = IMAGE_RE.match(line)
    return (match.group(1), match.group(2) or match.group(3)) if match else None


def image_size_mode(source):
    match = IMAGE_RE.match(source)
    return (match.group(4) or 'fit') if match else 'fit'


def with_image_size(source,mode):
    if mode not in {'original','quarter','half','full'} or not image_reference(source):
        return source
    plain = re.sub(r'\s*<!--\s*toshy-image-width:(?:original|quarter|half|full)\s*-->\s*$','',source)
    return plain.rstrip() + ' <!-- toshy-image-width:' + mode + ' -->'


def utf16_length(text):
    return len(text.encode('utf-16-le'))//2


def document_path(editor):
    path = getattr(editor, 'file_path', None)
    card = getattr(editor, 'card_item', None)
    if not path and card and card.scene():
        views = card.scene().views()
        path = getattr(views[0], 'file_path', None) if views else None
    return Path(path).resolve() if path else None


def resolve_image(reference, base_path=None):
    url = QUrl(reference)
    if url.isLocalFile():
        return Path(url.toLocalFile())
    if url.scheme() and not (len(url.scheme()) == 1 and ':' in reference):
        return None
    base = Path(base_path).parent if base_path else Path(app_state.workspace)
    return (base / unquote(reference)).resolve()


def open_reference(reference, base_path=None, *, switch_view=False):
    from PySide6.QtGui import QDesktopServices
    reference = reference.strip().removeprefix('<').removesuffix('>')
    url = QUrl(reference)
    if url.scheme() in {'http', 'https', 'mailto'}:
        return QDesktopServices.openUrl(url)
    else:
        plain = reference.split('#',1)[0]
        path = (Path(base_path).resolve() if not plain and base_path else resolve_image(plain,base_path))
        if path and path.is_file():
            if url.fragment() and path.suffix.lower() in {'.md','.markdown'}:
                event_bus.open_markdown_anchor_requested.emit(str(path),url.fragment())
                event_bus.switch_view_tab_requested.emit(2)
                return True
            event_bus.open_file_requested.emit(str(path))
            if switch_view:
                extension = path.suffix.lower()
                index = (3 if extension == '.canvas' else
                         4 if extension in {'.tpaint', '.png', '.jpg', '.jpeg'} else 2)
                event_bus.switch_view_tab_requested.emit(index)
            return True
    return False


def import_images(mime, base_path=None):
    """Copy sources only; never mutate the clipboard or delete an original."""
    workspace = Path(base_path).resolve().parent if base_path else Path(app_state.view_workspace('board'))
    destination = workspace / 'resources'
    from ui.image_mime import sources as image_sources
    sources = [(source,None) if isinstance(source,Path) else (None,source)
               for source in image_sources(mime)]
    if not sources:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    result = []
    base = Path(base_path).resolve().parent if base_path else workspace
    for path, image in sources:
        if path:
            # A unique name protects existing references and same-named imports.
            safe_stem = re.sub(r'[^\w.-]+', '_', path.stem) or 'image'
            target = destination / (safe_stem + '-' + uuid.uuid4().hex[:12] + path.suffix.lower())
            shutil.copy2(path, target)
        else:
            target = destination / ('image-' + uuid.uuid4().hex[:12] + '.png')
            if not image.save(str(target), 'PNG'):
                raise OSError('Не удалось сохранить картинку')
        reference = Path(os.path.relpath(target, base)).as_posix()
        result.append(f'![{target.stem}](<{reference}>)')
    return result


class InlineImageWidget(QWidget):
    remove_requested = Signal()
    activated = Signal()
    size_changed = Signal(str)

    def __init__(self, path, parent=None, *, removable=True, line_height=20):
        super().__init__(parent)
        watch_paint(self)
        self.source_path = path
        self.pixmap = QPixmap(str(path)) if path else QPixmap()
        self.line_height = max(1, line_height)
        self.removable = removable
        self.hovered = False
        self.selected = False
        self.size_mode = "fit"
        self._display_slice = None
        self.setMouseTracking(True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background:transparent; border:none;")
        self.setMinimumWidth(1)
        self.close_button = QToolButton(self)
        set_icon(self.close_button, 'close', size=16)
        set_text(self.close_button, 'ui.inline_images.remove_image_from_text_7173', setter='setToolTip')
        self.close_button.setFixedSize(26, 26)
        self.close_button.setCursor(Qt.PointingHandCursor)
        button_style = ('QToolButton {color:@text.primary@;background:@background.panel@;'
                        'border:1px solid @border.subtle@;border-radius:0;padding:0;}'
                        'QToolButton:hover {background:@control.hover@;}'
                        'QToolButton:pressed,QToolButton:checked {background:@control.selected@;}')
        apply_style(self.close_button, button_style)
        self.close_button.clicked.connect(self.remove_requested)
        self.close_button.hide()
        self.copy_button = QToolButton(self)
        set_icon(self.copy_button, 'copy', size=18)
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1500)
        self._copy_feedback_timer.setTimerType(Qt.PreciseTimer)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_icon)
        self.copy_button.setIconSize(QSize(18, 18))
        set_text(self.copy_button, 'ui.inline_images.copy_image_to_clipboard_fca6', setter='setToolTip')
        self.copy_button.setFixedSize(26, 26)
        self.copy_button.setCursor(Qt.PointingHandCursor)
        self.copy_button.setFocusPolicy(Qt.NoFocus)
        apply_style(self.copy_button, button_style)
        self.copy_button.clicked.connect(self.copy_image)
        self.copy_button.setEnabled(not self.pixmap.isNull())
        self.copy_button.hide()

        self.size_toolbar = QWidget(parent)
        self.size_toolbar.setObjectName('InlineImageSizeToolbar')
        self.size_toolbar.setAttribute(Qt.WA_StyledBackground, True)
        apply_style(self.size_toolbar, '#InlineImageSizeToolbar {background:@background.panel@;border:1px solid @border.subtle@;border-radius:6px;}')
        bar = QHBoxLayout(self.size_toolbar)
        bar.setContentsMargins(4,4,4,4)
        bar.setSpacing(4)
        self.size_buttons = {}
        for mode,icon,tip in [('original','image',tr('ui.caption.36f2b9087993')),
            ('quarter','arrows-minimize',tr('ui.caption.e975208214bb')),('half','arrows-diagonal',tr('ui.caption.c086ea1944c2')),('full','arrows-horizontal',tr('ui.caption.e8c072cc6e2e'))]:
            button = QToolButton(self.size_toolbar)
            set_icon(button, icon, size=16)
            button.setFixedSize(28, 28)
            button.setCursor(Qt.PointingHandCursor)
            apply_style(button, button_style)
            bind_caption(button, tip, setter='setToolTip')
            button.setCheckable(True)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(lambda _,m=mode:self._choose_size(m))
            bar.addWidget(button)
            self.size_buttons[mode] = button
        self.size_toolbar.adjustSize()
        self.size_toolbar.hide()
        self.destroyed.connect(self.size_toolbar.deleteLater)

    def _choose_size(self,mode):
        self.size_mode = mode
        self.updateGeometry()
        self.size_changed.emit(mode)
        self._position_size_toolbar()
        self.update()

    def set_selected(self,selected):
        self.selected = bool(selected)
        self.size_toolbar.setVisible(self.selected and self.isVisible())
        self._position_size_toolbar()

    def _position_size_toolbar(self):
        toolbar = self.size_toolbar
        for mode,button in self.size_buttons.items():
            button.setChecked(mode==self.size_mode)
        toolbar.adjustSize()
        parent = self.parentWidget()
        if parent is not None:
            toolbar.move(max(0,min(self.x(),parent.width()-toolbar.width())),max(0,self.y()-toolbar.height()-2))
        toolbar.raise_()

    def moveEvent(self,event):
        super().moveEvent(event)
        self._position_size_toolbar()

    def showEvent(self,event):
        super().showEvent(event)
        self.size_toolbar.setVisible(self.selected)
        self._position_size_toolbar()

    def hideEvent(self,event):
        self.size_toolbar.hide()
        super().hideEvent(event)

    def mousePressEvent(self,event):
        if event.button() in (Qt.LeftButton,Qt.RightButton):
            self.set_selected(True)
            self.activated.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def copy_image(self):
        if not self.pixmap.isNull():
            # QPixmap uses premultiplied alpha on some platforms. Load original
            # pixels when available so translucent colours survive copying.
            image = QImage(str(self.source_path)) if self.source_path else QImage()
            QApplication.clipboard().setImage(image if not image.isNull() else self.pixmap.toImage())
            set_icon(self.copy_button, 'check', size=18)
            self._copy_feedback_timer.start()

    def _reset_copy_icon(self):
        set_icon(self.copy_button, 'copy', size=18)

    def extent(self, available):
        available = max(1, int(available))
        if self.size_mode in {'quarter','half','full'}:
            width = max(1,round(available*{'quarter':.25,'half':.5,'full':1.}[self.size_mode]))
        elif self.size_mode=='original' and not self.pixmap.isNull():
            width = self.pixmap.width()
        else:
            width = min(available,self.pixmap.width()) if not self.pixmap.isNull() else available
        height = width * self.pixmap.height()/self.pixmap.width() if not self.pixmap.isNull() else self.line_height*2
        return QSizeF(width, math.ceil(height/self.line_height)*self.line_height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.close_button.move(max(0, self.width()-28), 2)
        self.copy_button.move(max(0, self.width()-(58 if self.removable else 28)), 2)
        self._position_size_toolbar()

    def enterEvent(self, event):
        self.hovered = True
        self.close_button.setVisible(self.removable)
        self.copy_button.show()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered = False
        self.close_button.hide()
        self.copy_button.hide()
        self.update()
        super().leaveEvent(event)

    def set_display_slice(self, full_size, offset):
        value = (round(full_size.width()),round(full_size.height()),round(offset.x()),round(offset.y()))
        if value != self._display_slice:
            self._display_slice = value
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if self.pixmap.isNull():
            painter.setPen(theme_manager.color('text.secondary'))
            painter.drawText(self.rect(),Qt.AlignCenter,tr('ui.caption.90480027ecaf'))
            return
        width,_,ox,oy = self._display_slice or (self.width(),self.height(),0,0)
        scale = width/self.pixmap.width()
        height = self.pixmap.height()*scale
        # Blit only the visible source fragment. No oversized off-screen child
        # window or huge negative target rectangle enters the Qt paint engine.
        area = QRectF(event.rect()).intersected(QRectF(self.rect()))
        source_area = area.translated(ox,oy).intersected(QRectF(0,0,width,height))
        if source_area.isEmpty():
            return
        target = source_area.translated(-ox,-oy)
        source = QRectF(source_area.x()/scale,source_area.y()/scale,
                       source_area.width()/scale,source_area.height()/scale)
        painter.drawPixmap(target,self.pixmap,source)
        if self.hovered:
            band = QRectF(0, 0, self.width(), min(56, self.height()))
            shade = theme_manager.color('background.window')
            shade.setAlpha(225)
            clear = QColor(shade)
            clear.setAlpha(0)
            fade = QLinearGradient(band.topLeft(), band.bottomLeft())
            fade.setColorAt(0, shade)
            fade.setColorAt(1, clear)
            painter.fillRect(band.intersected(target), fade)



class ImageTextObject(QPyTextObject):
    def __init__(self, controller):
        super().__init__(controller.editor)
        self.controller = controller

    def intrinsicSize(self, doc, pos, fmt):
        source = str(fmt.property(IMAGE_SOURCE) or '')
        reference = image_reference(source)
        if not reference:
            return QSizeF(1, 1)
        widget = self.controller.widget_for(source, pos)
        size = widget.extent(self.controller.available_width())
        return QSizeF(size.width(), self.controller.editor.fontMetrics().ascent())

    def drawObject(self, painter, rect, doc, pos, fmt):
        # The image itself is a QWidget on the text viewport; the text object
        # reserves its rows, including the gap up to the next baseline.
        pass


class InlineImages(QObject):
    def __init__(self, handler):
        super().__init__(handler.editor)
        self.handler = handler
        self.editor = handler.editor
        self.widgets = {}
        self.syncing = False
        self._padding_dirty = True
        self.dirty = None
        self.fence_key = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.sync(full=False))
        self.renderer = ImageTextObject(self)
        self.editor.document().documentLayout().registerHandler(IMAGE_OBJECT, self.renderer)
        self.editor.document().contentsChange.connect(self.changed)
        self._position_timer = QTimer(self)
        self._position_timer.setSingleShot(True)
        self._position_timer.timeout.connect(self.position_widgets)
        self.editor.updateRequest.connect(self._viewport_updated)
        self.editor.verticalScrollBar().valueChanged.connect(lambda _:self._position_timer.start(0))
        self.editor.horizontalScrollBar().valueChanged.connect(lambda _:self._position_timer.start(0))
        self.editor.selectionChanged.connect(self._selection_changed)
        self.editor.viewport().installEventFilter(self)
        self.editor.installEventFilter(self)

    def _viewport_updated(self,*_):
        self.position_widgets()
        # QPlainTextEdit may move viewport children as part of its scroll.
        # Publish the final slice after that native operation has completed.
        self._position_timer.start(0)

    def available_width(self):
        return max(1, self.editor.viewport().width()-2*self.editor.document().documentMargin()-8)

    def supported(self):
        return self.handler.enabled

    def active(self):
        return self.handler.enabled and self.handler.visual_enabled

    def schedule(self):
        if not self.syncing:
            self.timer.start(0)

    def changed(self, position, removed, added):
        if self.syncing or not (removed or added):
            return
        self._padding_dirty = True
        doc = self.editor.document()
        start, end = position, min(position+added, doc.characterCount()-1)
        if self.dirty:
            start = min(start, self.dirty[0].position())
            end = max(end, self.dirty[1].position())
        left, right = QTextCursor(doc), QTextCursor(doc)
        left.setPosition(start); right.setPosition(end)
        self.dirty = (left, right)
        self.schedule()

    def source_at(self, pos):
        doc = self.editor.document()
        if pos < 0 or pos >= doc.characterCount()-1 or doc.characterAt(pos) != '\ufffc':
            return None
        cursor = QTextCursor(doc)
        cursor.setPosition(pos)
        cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
        return str(cursor.charFormat().property(IMAGE_SOURCE) or '') if cursor.charFormat().objectType() == IMAGE_OBJECT else None

    def widget_for(self, source, pos):
        # Anchors follow edits and keep identical image references independent.
        for widget, anchor in self.widgets.values():
            if anchor.position() == pos:
                if widget.property('image_source') != source:
                    reference = image_reference(source)
                    path = resolve_image(reference[1],document_path(self.editor))
                    if path != widget.source_path:
                        widget.source_path = path
                        widget.pixmap = QPixmap(str(path)) if path else QPixmap()
                    widget.setProperty('image_source',source)
                    widget.size_mode = image_size_mode(source)
                return widget
        reference = image_reference(source)
        path = resolve_image(reference[1], document_path(self.editor))
        card = getattr(self.editor, 'card_item', None)
        widget = InlineImageWidget(path, self.editor.viewport(),
            removable=not card.is_read_only() if card else not self.editor.isReadOnly(), line_height=self.editor.fontMetrics().lineSpacing())
        widget.setProperty('image_source', source)
        widget.size_mode = image_size_mode(source)
        anchor = QTextCursor(self.editor.document())
        anchor.setPosition(pos)
        key = uuid.uuid4().hex
        self.widgets[key] = (widget, anchor)
        self._padding_dirty = True
        widget.remove_requested.connect(lambda: self.remove(anchor))
        widget.activated.connect(lambda:self._activate_image(widget,anchor))
        widget.size_changed.connect(lambda mode:self._resize_image(widget,anchor,mode))
        return widget

    def _activate_image(self,widget,anchor):
        for other,_ in self.widgets.values():
            other.set_selected(other is widget)
        cursor = QTextCursor(anchor)
        cursor.movePosition(QTextCursor.NextCharacter,QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)

    def _selection_changed(self):
        cursor = self.editor.textCursor()
        for widget,anchor in self.widgets.values():
            widget.set_selected(cursor.hasSelection() and cursor.selectionStart()<=anchor.position()<cursor.selectionEnd())

    def _resize_image(self,widget,anchor,mode):
        if not self.editor.isReadOnly():
            cursor = QTextCursor(anchor)
            source = self.source_at(cursor.position())
            if source:
                cursor.movePosition(QTextCursor.NextCharacter,QTextCursor.KeepAnchor)
                fmt = cursor.charFormat()
                value = with_image_size(source,mode)
                fmt.setProperty(IMAGE_SOURCE,value)
                cursor.setCharFormat(fmt)
                widget.setProperty('image_source',value)
                self.editor.document().setModified(True)
        self._padding_dirty = True
        self.editor.document().markContentsDirty(anchor.position(),1)
        self.position_widgets()

    def remove(self, anchor):
        card = getattr(self.editor, 'card_item', None)
        if card.is_read_only() if card else self.editor.isReadOnly():
            return
        cursor = QTextCursor(anchor)
        if self.source_at(cursor.position()):
            cursor.setPosition(cursor.position()+1+self.padding_length(cursor.position()+1),QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            self.editor.setTextCursor(cursor)
            self.editor.setFocus()

    def remove_source(self, source):
        block = self.editor.document().firstBlock()
        while block.isValid():
            native = self.source_at(block.position())
            if native == source or block.text() == source:
                cursor = QTextCursor(block)
                cursor.setPosition(block.position()+ (1 if native else utf16_length(block.text())), QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                self.editor.setTextCursor(cursor)
                return
            block = block.next()

    def sync(self, full=True):
        if self.syncing:
            return
        self.timer.stop()
        doc = self.editor.document()
        changes = []
        active = self.active()
        fence_key = tuple(self.handler.code_block_ranges)
        if fence_key != self.fence_key:
            full = True
            self.fence_key = fence_key
        if not full and self.dirty is None:
            self.position_widgets()
            return
        if not active and not self.widgets:
            self.dirty = None
            return
        block = doc.firstBlock() if full or not self.dirty else doc.findBlock(self.dirty[0].position())
        last = doc.lastBlock() if full or not self.dirty else doc.findBlock(self.dirty[1].position())
        self.dirty = None
        while block.isValid() and block.blockNumber() <= last.blockNumber():
            text = block.text()
            source = self.source_at(block.position())
            if source and (not active or self.handler.is_in_code_block(block.blockNumber())):
                changes.append((block.position(), 1+self.padding_length(block.position()+1), source, False))
            elif active and not self.handler.is_in_code_block(block.blockNumber()) and image_reference(text):
                changes.append((block.position(), utf16_length(text), text, True))
            block = block.next()
        if changes:
            modified = doc.isModified()
            self.syncing = True
            try:
                edit = QTextCursor(doc)
                # Join presentation changes to the triggering edit: Undo must
                # never stop at a literal object replacement character.
                edit.joinPreviousEditBlock()
                for pos, length, source, render in reversed(changes):
                    edit.setPosition(pos)
                    edit.setPosition(pos+length, QTextCursor.KeepAnchor)
                    fmt = QTextCharFormat()
                    if render:
                        fmt.setObjectType(IMAGE_OBJECT)
                        fmt.setProperty(IMAGE_SOURCE, source)
                    edit.insertText('\ufffc' if render else source, fmt)
                edit.endEditBlock()
                doc.setModified(modified)
            finally:
                self.syncing = False
        self.position_widgets()

    def padding_length(self,pos):
        document=self.editor.document();length=0
        while pos+length<document.characterCount()-1 and document.characterAt(pos+length)=='\u2028':
            cursor=QTextCursor(document);cursor.setPosition(pos+length)
            cursor.movePosition(QTextCursor.NextCharacter,QTextCursor.KeepAnchor)
            if not cursor.charFormat().property(IMAGE_PADDING):break
            length+=1
        return length

    def _sync_padding(self):
        if not self._padding_dirty or self.syncing:return
        self._padding_dirty=False
        document=self.editor.document();changes=[];repairs=[];block=document.firstBlock()
        while block.isValid():
            iterator=block.begin()
            while not iterator.atEnd():
                fragment=iterator.fragment()
                if fragment.charFormat().property(IMAGE_PADDING):
                    for match in re.finditer(r'[^\u2028]+',fragment.text()):
                        repairs.append((fragment.position()+utf16_length(fragment.text()[:match.start()]),utf16_length(match.group())))
                iterator+=1
            source=self.source_at(block.position())
            if source and self.active():
                widget=self.widget_for(source,block.position())
                widget.line_height=max(1,self.editor.fontMetrics().lineSpacing())
                count=max(0,math.ceil(widget.extent(self.available_width()).height()/widget.line_height)-1)
                old=self.padding_length(block.position()+1)
                if count!=old:changes.append((block.position()+1,old,count))
            else:
                iterator=block.begin()
                while not iterator.atEnd():
                    fragment=iterator.fragment()
                    if fragment.charFormat().property(IMAGE_PADDING):
                        for match in re.finditer('\u2028+',fragment.text()):
                            changes.append((fragment.position()+utf16_length(fragment.text()[:match.start()]),len(match.group()),0))
                    iterator+=1
            block=block.next()
        if not changes and not repairs:return
        modified=document.isModified();self.syncing=True
        try:
            cursor=QTextCursor(document);cursor.joinPreviousEditBlock()
            for position,length in repairs:
                cursor.setPosition(position);cursor.setPosition(position+length,QTextCursor.KeepAnchor)
                fmt=cursor.charFormat();fmt.clearProperty(IMAGE_PADDING);fmt.clearProperty(QTextFormat.ForegroundBrush)
                cursor.setCharFormat(fmt)
            for position,old,count in reversed(changes):
                cursor.setPosition(position);cursor.setPosition(position+old,QTextCursor.KeepAnchor)
                fmt=QTextCharFormat();fmt.setProperty(IMAGE_PADDING,True)
                fmt.setForeground(Qt.transparent)
                cursor.insertText('\u2028'*count,fmt)
            cursor.endEditBlock();document.setModified(modified)
        finally:self.syncing=False

    def position_widgets(self):
        if self.syncing:return
        self._sync_padding()
        for key, (widget, anchor) in list(self.widgets.items()):
            source = self.source_at(anchor.position())
            if not source or not self.active():
                widget.set_selected(False)
                widget.hide()
                widget.deleteLater()
                del self.widgets[key]
                continue
            if widget.property('image_source') != source:
                reference = image_reference(source)
                path = resolve_image(reference[1],document_path(self.editor))
                if path != widget.source_path:
                    widget.source_path = path
                    widget.pixmap = QPixmap(str(path)) if path else QPixmap()
                widget.setProperty('image_source',source)
                widget.size_mode = image_size_mode(source)
            block = anchor.block()
            rect = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset())
            size = widget.extent(self.available_width())
            alignment = block.blockFormat().alignment()
            x = self.editor.document().documentMargin()+self.editor.contentOffset().x()
            if alignment & Qt.AlignHCenter:
                x += (self.available_width()-size.width())/2
            elif alignment & Qt.AlignRight:
                x += self.available_width()-size.width()
            full = QRect(round(x),round(rect.top()),round(size.width()),round(size.height()))
            visible = full.intersected(self.editor.viewport().rect())
            widget.set_display_slice(size,visible.topLeft()-full.topLeft())
            if not visible.isEmpty():
                widget.setGeometry(visible)
            widget.setVisible(not visible.isEmpty())
            widget.raise_()
            widget.size_toolbar.setVisible(widget.selected and widget.isVisible())
            widget._position_size_toolbar()

        # Floating editor controls must stay above embedded image/code widgets.
        from PySide6.QtWidgets import QWidget
        for overlay in self.editor.viewport().findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
            if overlay.isVisible() and overlay.property('editorOverlay'):
                overlay.raise_()
                tags = getattr(overlay, 'tags_bar', None)
                if tags is not None and tags.isVisible():
                    tags.raise_()

    def insert_mime(self, mime, pos=None):
        if not self.supported() or self.editor.isReadOnly():
            return False
        from ui.markdown_clipboard import editor_source
        source = editor_source(mime, document_path(self.editor))
        if source is not None:
            cursor = self.editor.cursorForPosition(pos) if pos is not None else self.editor.textCursor()
            cursor.beginEditBlock()
            self._after_image(cursor)
            lines = source.splitlines()
            prefix = '\n' if lines and image_reference(lines[0]) and cursor.positionInBlock() else ''
            suffix = '\n' if lines and image_reference(lines[-1]) and not source.endswith('\n') else ''
            cursor.insertText(prefix + source + suffix, QTextCharFormat())
            cursor.endEditBlock()
            self.editor.setTextCursor(cursor)
            self.sync()
            return True
        sources = import_images(mime, document_path(self.editor))
        if not sources:
            return False
        cursor = self.editor.cursorForPosition(pos) if pos is not None else self.editor.textCursor()
        cursor.beginEditBlock()
        self._after_image(cursor)
        prefix = '\n' if cursor.positionInBlock() else ''
        cursor.insertText(prefix+'\n'.join(sources)+'\n', QTextCharFormat())
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        self.sync()
        return True

    def _after_image(self, cursor):
        if not cursor.hasSelection() and self.source_at(cursor.block().position()) and cursor.positionInBlock() > 0:
            cursor.movePosition(QTextCursor.EndOfBlock)
            cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
        # Presentation formatting must never be inherited by user input.
        if cursor.charFormat().property(IMAGE_PADDING) or cursor.charFormat().objectType() == IMAGE_OBJECT:
            cursor.setCharFormat(QTextCharFormat())

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress and not self.editor.isReadOnly():
            cursor = self.editor.textCursor()
            if not cursor.hasSelection():
                block=cursor.block()
                if self.source_at(block.position()) and cursor.positionInBlock()>0:
                    if event.key() in {Qt.Key_Backspace,Qt.Key_Delete}:
                        # Reserved image rows are not editable empty lines.
                        # Select the image itself to remove it.
                        cursor.setPosition(block.position() if event.key()==Qt.Key_Backspace else min(block.position()+block.length(),self.editor.document().characterCount()-1))
                        self.editor.setTextCursor(cursor);return True
                    if event.key() in {Qt.Key_Left,Qt.Key_Up,Qt.Key_Right,Qt.Key_Down}:
                        target=block.position() if event.key() in {Qt.Key_Left,Qt.Key_Up} else block.position()+block.length()
                        cursor.setPosition(min(target,self.editor.document().characterCount()-1))
                        self.editor.setTextCursor(cursor);return True
                    if event.text() or event.key() in {Qt.Key_Return,Qt.Key_Enter}:
                        self._after_image(cursor)
                        self.editor.setTextCursor(cursor)
                        if event.key() in {Qt.Key_Return,Qt.Key_Enter}:return True
                elif cursor.charFormat().property(IMAGE_PADDING):
                    cursor.setCharFormat(QTextCharFormat())
                    self.editor.setTextCursor(cursor)
                previous = cursor.block().previous()
                following = cursor.block().next()
                if event.key() == Qt.Key_Backspace and cursor.atBlockStart() and previous.isValid() and self.source_at(previous.position()):
                    cursor.setPosition(previous.position(), QTextCursor.KeepAnchor)
                    cursor.removeSelectedText()
                    self.editor.setTextCursor(cursor)
                    return True
                if event.key() == Qt.Key_Delete and cursor.atBlockEnd() and following.isValid() and self.source_at(following.position()):
                    cursor.setPosition(following.position()+1+self.padding_length(following.position()+1), QTextCursor.KeepAnchor)
                    cursor.removeSelectedText()
                    self.editor.setTextCursor(cursor)
                    return True
        if event.type() == QEvent.Resize:
            self._padding_dirty = True
            self.editor.document().markContentsDirty(0, self.editor.document().characterCount())
            self.schedule()
        if event.type() in {QEvent.DragEnter, QEvent.DragMove} and self.supported():
            mime = event.mimeData()
            from ui.image_mime import can_import
            if not self.editor.isReadOnly() and can_import(mime):
                event.setDropAction(Qt.CopyAction)
                event.accept()
                return True
        if event.type() == QEvent.Drop:
            if self.insert_mime(event.mimeData(), event.position().toPoint()):
                event.setDropAction(Qt.CopyAction)
                event.accept()
                return True
        return super().eventFilter(watched, event)
