from __future__ import annotations
from ui.icons import set_label_icon, get as get_icon, set_icon, symbol_button
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text
from ui.styles.binding import apply_style


from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
import hashlib
import math
import struct
import wave
import shutil
import subprocess
import threading

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl, QStandardPaths, Signal, QFileInfo
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    QT_MEDIA_AVAILABLE = True
except Exception:  # Packaging can omit Qt multimedia plugins/modules.
    QAudioOutput = None
    QMediaPlayer = None
    QT_MEDIA_AVAILABLE = False


class _FlowLayout(QLayout):
    def __init__(self, parent=None, margin: int = 0, h_spacing: int = 6, v_spacing: int = 6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, max(1, width), 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        size += QSize(left + right, top + bottom)
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(left, top, -right, -bottom)
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_spacing
            if next_x - self._h_spacing > effective.right() and line_height > 0:
                x = effective.x()
                y += line_height + self._v_spacing
                next_x = x + hint.width() + self._h_spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return (y + line_height - rect.y()) + bottom


def format_file_size(size: int) -> str:
    value = float(max(0, int(size or 0)))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def media_icon(media_type: str) -> str:
    return {
        "image": "▧",
        "audio": "♪",
        "video": "▶",
        "text": "≡",
    }.get(str(media_type or "").lower(), "□")


def show_project_copy_menu(widget, pos, source_path):
    from PySide6.QtWidgets import (QMenu, QFileDialog)
    from ui.widgets.dialogs import MessageBox as QMessageBox
    from ui.app_state import app_state
    menu = QMenu(widget)
    action = localize(add_caption_action(menu, 'addAction', 'Добавить в Project Folder…'), 'ui.chat_attachments.add_to_project_folder_0ff0')
    action.setEnabled(bool(app_state.project_id and source_path))
    if menu.exec(widget.mapToGlobal(pos)) != action:
        return
    source = Path(source_path)
    root = Path(app_state.workspace).resolve()
    target, _ = QFileDialog.getSaveFileName(widget, 'Добавить в Project Folder', str(root / source.name))
    if not target:
        return
    target = Path(target).resolve()
    if not target.is_relative_to(root):
        QMessageBox.warning(widget, tr('ui.chat_attachments.project_folder_31bf'), tr('ui.chat_attachments.choose_a_path_inside_the_project_3cbe'))
        return
    if source.resolve() == target:
        return
    from ui.core_bridge.async_runner import AsyncRunner
    if not hasattr(widget, '_copy_runner'):
        widget._copy_runner = AsyncRunner(widget)
    from core.attachment_files import copy_to_project
    from shiboken6 import isValid
    widget._copy_runner.submit(lambda: copy_to_project(source, target),
        on_error=lambda error: QMessageBox.warning(widget, tr('ui.chat_attachments.project_folder_31bf'), str(error)) if isValid(widget) else None)


class PendingAttachmentChip(QFrame):
    remove_requested = Signal(str)

    def __init__(self, item: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.item = item
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: show_project_copy_menu(self, pos, item.get('source_path')))
        self.key = str(item.get("pending_id") or item.get("source_path") or id(self))
        self.setObjectName("PendingAttachmentChip")
        apply_style(self, "#PendingAttachmentChip { background:@background.panel@; border:1px solid @control.hover@; border-radius:6px; }"
            "#PendingAttachmentChip QLabel { background:transparent; border:none; }")

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 5, 5, 5)
        row.setSpacing(6)

        icon = QLabel(self)
        from ui.file_icons import bind_file_icon
        icon.setFixedSize(18, 18)
        bind_file_icon(icon, str(item.get('source_path') or item.get('filename') or 'file'), 18)
        apply_style(icon, "color:@text.secondary@; font-size:13px;")
        row.addWidget(icon)

        name = bind_caption(QLabel('', self), str(item.get("position", "")) + ". " + str(item.get("filename") or "file"))
        apply_style(name, "color:@text.primary@; font-size:11px;")
        bind_caption(name, str(item.get("source_path") or ""), setter='setToolTip')
        name.setMaximumWidth(230)
        row.addWidget(name)

        size = QLabel(format_file_size(int(item.get("size") or 0)), self)
        apply_style(size, "color:@text.disabled@; font-size:10px;")
        row.addWidget(size)

        if item.get('media_type') == 'text':
            self.memory_button = QToolButton(self)
            self.memory_button.setObjectName('AttachmentMemoryButton')
            self.memory_button.setIcon(get_icon('memory'))
            self.memory_button.setIconSize(QSize(16, 16))
            self.memory_button.setFixedSize(24, 24)
            set_text(self.memory_button, 'ui.chat_attachments.load_into_temporary_memory_e6a2', setter='setToolTip')
            set_text(self.memory_button, 'ui.chat_attachments.load_into_temporary_memory_e6a2', setter='setAccessibleName')
            self.memory_button.setCheckable(True)
            self.memory_button.setChecked(bool(item.get('to_memory', False)))
            self.memory_button.setCursor(Qt.PointingHandCursor)
            self.memory_button.setToolTipDuration(10000)
            apply_style(self.memory_button, 'QToolButton {color:@text.disabled@; background:transparent; border:1px solid @control.hover@; border-radius:4px; padding:0;}'
                'QToolButton:checked {color:@accent.primary@; background:@control.selected@; border-color:@accent.primary@;}'
                'QToolButton:hover {border-color:@accent.primary@;}')
            self.memory_button.toggled.connect(lambda checked: item.__setitem__('to_memory', checked))
            row.addWidget(self.memory_button)

        close = QToolButton(self)
        set_icon(close, 'close', square=True)
        close.setCursor(Qt.PointingHandCursor)
        close.setFixedSize(18, 18)
        apply_style(close, 'QToolButton { color:@text.secondary@; background:transparent; border:none; font-size:15px; padding:0; }QToolButton:hover { color:@text.primary@; background:@control.hover@; border-radius:3px; }')
        close.clicked.connect(lambda: self.remove_requested.emit(self.key))
        row.addWidget(close)


class PendingAttachmentsPanel(QFrame):
    remove_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PendingAttachmentsPanel")
        apply_style(self, '#PendingAttachmentsPanel { background:@background.window@; border:1px solid @border.subtle@; border-radius:7px; }QScrollArea { background:transparent; border:none; }QScrollArea > QWidget > QWidget { background:transparent; }')

        root = QVBoxLayout(self)
        root.setContentsMargins(7, 7, 7, 7)
        root.setSpacing(0)

        self.scroll = QScrollArea(self)
        # QAbstractScrollArea's default minimumSizeHint reserves ~70 px even
        # for one short chip. Let the measured rows determine this height.
        self.scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setFrameShape(QFrame.NoFrame)

        self.content = QWidget(self.scroll)
        self.flow_layout = _FlowLayout(self.content, margin=0, h_spacing=6, v_spacing=6)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll)
        self.hide()

    def set_items(self, items: Iterable[Dict[str, Any]]) -> None:
        while self.flow_layout.count():
            item = self.flow_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()
        count = 0
        for attachment in items:
            chip = PendingAttachmentChip(attachment, self.content)
            chip.remove_requested.connect(self.remove_requested)
            self.flow_layout.addWidget(chip)
            count += 1
        self.setVisible(count > 0)
        self.content.updateGeometry()
        self.updateGeometry()

    def preferred_height(self, width: int) -> int:
        # Unlimited attachment count is supported. The overlay itself stays
        # compact; once the chips need more room it gets a vertical scrollbar.
        margins = self.layout().contentsMargins()
        frame = self.contentsMargins()
        inner_width = max(1, int(width) - margins.left() - margins.right() - frame.left() - frame.right())
        content_height = self.flow_layout.heightForWidth(inner_width)
        padding = margins.top() + margins.bottom() + frame.top() + frame.bottom()
        if content_height + padding > 190:
            inner_width = max(1, inner_width - self.scroll.style().pixelMetric(QStyle.PM_ScrollBarExtent))
            content_height = self.flow_layout.heightForWidth(inner_width)
        self.content.setMinimumHeight(content_height)
        return min(190, content_height + padding)


class _ClickablePreview(QLabel):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(68, 68)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        apply_style(self, "QLabel { background:@background.window@; border:1px solid @control.hover@; border-radius:6px; "
            "color:@text.secondary@; font-size:22px; }"
            "QLabel:hover { border-color:@text.disabled@; background:@background.window@; }")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_square_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        side = max(1, min(pixmap.width(), pixmap.height()))
        x = max(0, (pixmap.width() - side) // 2)
        y = max(0, (pixmap.height() - side) // 2)
        square = pixmap.copy(x, y, side, side)
        self.setPixmap(square.scaled(66, 66, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        bind_caption(self, "")


class _WaveformPreview(_ClickablePreview):
    def __init__(self, path: Optional[str], parent=None):
        super().__init__(parent)
        self._peaks = self._read_peaks(path) if path else []
        bind_caption(self, "")

    @staticmethod
    def _read_peaks(path: str, columns: int = 24) -> List[float]:
        """Read a small, real waveform preview for PCM WAV files.

        Other formats stay fully supported as attachments; when Qt/the stdlib
        cannot cheaply decode them here, the painter falls back to a neutral
        waveform glyph instead of introducing another media dependency.
        """
        try:
            with wave.open(path, "rb") as wav:
                frames = wav.getnframes()
                channels = max(1, wav.getnchannels())
                width = wav.getsampwidth()
                if frames <= 0 or width not in (1, 2, 3, 4):
                    return []
                values: List[float] = []
                samples_per_col = max(1, min(256, frames // max(1, columns * 2)))
                max_value = float((1 << (width * 8 - 1)) - 1) if width > 1 else 127.0
                for idx in range(columns):
                    pos = min(frames - 1, int((idx / max(1, columns - 1)) * (frames - 1)))
                    wav.setpos(pos)
                    raw = wav.readframes(samples_per_col)
                    peak = 0.0
                    frame_bytes = width * channels
                    for off in range(0, len(raw) - frame_bytes + 1, frame_bytes):
                        for ch in range(channels):
                            sample = raw[off + ch * width : off + (ch + 1) * width]
                            if width == 1:
                                val = sample[0] - 128
                            elif width == 2:
                                val = struct.unpack_from("<h", sample)[0]
                            elif width == 3:
                                val = int.from_bytes(sample, "little", signed=True)
                            else:
                                val = struct.unpack_from("<i", sample)[0]
                            peak = max(peak, abs(float(val)) / max_value)
                    values.append(min(1.0, peak))
                return values
        except Exception:
            return []

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor("#a78bfa"), 2.0, Qt.SolidLine, Qt.RoundCap))
        rect = self.rect().adjusted(8, 9, -8, -9)
        mid = rect.center().y()
        peaks = self._peaks
        if not peaks:
            # Neutral waveform placeholder for compressed formats when there is
            # no cheap decoder available. It is intentionally not fake audio data.
            peaks = [0.18, 0.42, 0.68, 0.36, 0.78, 0.52, 0.28, 0.62, 0.88, 0.48, 0.72, 0.32,
                     0.58, 0.82, 0.44, 0.66, 0.26, 0.54, 0.76, 0.38, 0.62, 0.46, 0.30, 0.18]
        step = rect.width() / max(1, len(peaks))
        for i, peak in enumerate(peaks):
            x = rect.left() + (i + 0.5) * step
            half = max(2.0, peak * rect.height() * 0.46)
            painter.drawLine(int(x), int(mid - half), int(x), int(mid + half))
        painter.end()


class _VideoThumbnailService(QObject):
    """Serial, cached first-frame extractor for chat video thumbnails.

    Thumbnail extraction is deliberately kept outside QtMultimedia.  On Linux,
    Qt's FFmpeg backend can dump one full libav stream description per video to
    the parent terminal and may also create a noticeable startup spike when a
    session contains many clips.  A single daemon worker invokes ffmpeg
    sequentially with stderr captured, so the GUI stays responsive and normal
    extraction is completely silent.
    """

    thumbnail_ready = Signal(str, str)  # source signature, cached thumbnail path
    _instance = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: List[tuple[str, str, Path]] = []
        self._queued_keys = set()
        self._lock = threading.Lock()
        self._worker_running = False
        self._ffmpeg = shutil.which("ffmpeg")
        self._reported_missing_ffmpeg = False

        base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        if not base:
            base = str(Path.home() / ".cache" / "toshy")
        self._cache_dir = Path(base) / "video_thumbnails"
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _source_signature(path: str) -> Optional[str]:
        try:
            p = Path(path).resolve()
            st = p.stat()
            raw = f"{p}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8", errors="replace")
            return hashlib.sha1(raw).hexdigest()
        except OSError:
            return None

    def cache_lookup(self, path: str) -> tuple[Optional[str], Optional[QPixmap]]:
        key = self._source_signature(path)
        if not key:
            return None, None
        cache_file = self._cache_dir / f"{key}.jpg"
        if not cache_file.is_file():
            return key, None
        pixmap = QPixmap(str(cache_file))
        if pixmap.isNull():
            try:
                cache_file.unlink()
            except OSError:
                pass
            return key, None
        return key, pixmap

    def request(self, path: str) -> Optional[str]:
        key, cached = self.cache_lookup(path)
        if not key:
            return None
        cache_file = self._cache_dir / f"{key}.jpg"
        if cached is not None:
            QTimer.singleShot(0, lambda k=key, p=str(cache_file): self.thumbnail_ready.emit(k, p))
            return key

        with self._lock:
            if key in self._queued_keys:
                return key
            self._queued_keys.add(key)
            self._queue.append((key, str(path), cache_file))
            if not self._worker_running:
                self._worker_running = True
                threading.Thread(target=self._worker_loop, daemon=True).start()
        return key

    def _worker_loop(self):
        while True:
            with self._lock:
                if not self._queue:
                    self._worker_running = False
                    return
                key, source_path, cache_file = self._queue.pop(0)
                self._queued_keys.discard(key)

            source = Path(source_path)
            if not source.is_file():
                continue

            ffmpeg = self._ffmpeg
            if not ffmpeg:
                if not self._reported_missing_ffmpeg:
                    self._reported_missing_ffmpeg = True
                    print("[VideoThumbnail] ffmpeg not found; video thumbnails are disabled.")
                continue

            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cmd = [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-nostats",
                    "-nostdin",
                    "-ss", "0",
                    "-i", str(source),
                    "-frames:v", "1",
                    "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                    "-q:v", "3",
                    "-y",
                    str(cache_file),
                ]
                creationflags = 0
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creationflags = subprocess.CREATE_NO_WINDOW
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                    timeout=20,
                )
                if proc.returncode == 0 and cache_file.is_file() and cache_file.stat().st_size > 0:
                    self.thumbnail_ready.emit(key, str(cache_file))
                else:
                    try:
                        cache_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    # Only real failures reach the terminal; ordinary ffmpeg
                    # stream metadata is captured above and never printed.
                    detail = " ".join((proc.stderr or "").strip().splitlines()[-2:]).strip()
                    if detail:
                        print(f"[VideoThumbnail] {source.name}: {detail}")
            except subprocess.TimeoutExpired:
                try:
                    cache_file.unlink(missing_ok=True)
                except OSError:
                    pass
                print(f"[VideoThumbnail] timeout: {source.name}")
            except Exception as error:
                try:
                    cache_file.unlink(missing_ok=True)
                except OSError:
                    pass
                print(f"[VideoThumbnail] {source.name}: {error}")


class _VideoThumbnail(_ClickablePreview):
    """A lightweight video preview backed by the shared thumbnail queue."""

    def __init__(self, path: Optional[str], parent=None):
        super().__init__(parent)
        set_label_icon(self, 'video', size=20)
        self._service = _VideoThumbnailService.instance()
        self._request_key: Optional[str] = None
        self._connected = False

        if not path or not Path(path).is_file():
            return

        key, cached = self._service.cache_lookup(path)
        self._request_key = key
        if cached is not None:
            self.set_square_pixmap(cached)
            return

        try:
            self._service.thumbnail_ready.connect(self._on_thumbnail_ready)
            self._connected = True
        except (RuntimeError, TypeError):
            return
        self._request_key = self._service.request(path)

    def _on_thumbnail_ready(self, key: str, cache_path: str):
        if not self._request_key or key != self._request_key:
            return
        pixmap = QPixmap(str(cache_path or ""))
        if not pixmap.isNull():
            self.set_square_pixmap(pixmap)
        self._disconnect_service()

    def _disconnect_service(self):
        if not self._connected:
            return
        self._connected = False
        try:
            self._service.thumbnail_ready.disconnect(self._on_thumbnail_ready)
        except (RuntimeError, TypeError):
            pass

    def dispose(self):
        self._disconnect_service()

    def event(self, event):
        if event.type() == QEvent.DeferredDelete:
            self.dispose()
        return super().event(event)


class AttachmentMessageWidget(QFrame):
    remove_requested = Signal(str)

    def __init__(
        self,
        attachment: Dict[str, Any],
        path_resolver: Callable[[Dict[str, Any]], Optional[str]],
        *,
        removable: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.attachment = dict(attachment)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._project_copy_menu)
        self.attachment_id = str(self.attachment.get("id") or "")
        self.path_resolver = path_resolver
        self._resolved_path = self.path_resolver(self.attachment)
        self.player = None
        self.audio_output = None

        self.setObjectName("ChatAttachmentCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        apply_style(self, "#ChatAttachmentCard { background:@background.panel@; border:1px solid @control.hover@; border-radius:7px; }"
            "#ChatAttachmentCard QLabel { background:transparent; border:none; }")

        filename = str(self.attachment.get("filename") or "file")
        media_type = str(self.attachment.get("media_type") or "").lower()

        # Attachment cards have a stable footprint. Long filenames never make
        # a message wider; the full name is still available in the tooltip.
        card_width = 246
        title_width = 122
        self.setFixedWidth(card_width)
        self.setFixedHeight(84)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(7, 7, 6, 7)
        row.setSpacing(8)

        self.preview = self._make_preview(media_type)
        bind_caption(self.preview, self._resolved_path or filename, setter='setToolTip')
        self.preview.clicked.connect(self._open_file)
        row.addWidget(self.preview, 0, Qt.AlignVCenter)

        info = QVBoxLayout()
        info.setContentsMargins(0, 1, 0, 1)
        info.setSpacing(3)

        self.title = title = QLabel("", self)
        apply_style(title, "color:@text.primary@; font-size:11px; font-weight:600;")
        bind_caption(title, filename, setter='setToolTip')
        title.setFixedWidth(title_width)
        title.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        bind_caption(title, title.fontMetrics().elidedText(str(self.attachment.get("position", 1)) + ". " + filename, Qt.ElideRight, title_width))
        info.addWidget(title)

        size = QLabel(format_file_size(int(self.attachment.get("size") or 0)), self)
        apply_style(size, "color:@text.disabled@; font-size:10px;")
        info.addWidget(size)

        if media_type == "audio":
            play = set_icon(QPushButton('', self), 'right', square=True)
            set_text(play, 'ui.chat_attachments.play_pause_fef4', setter='setToolTip')
            play.setFixedSize(28, 22)
            play.setObjectName("ChatInputAuxBtn")
            play.clicked.connect(lambda: self._play_pause(play))
            if self._setup_player() is None:
                set_text(play, 'ui.chat_attachments.open_in_external_player_4ae9', setter='setToolTip')
            info.addWidget(play, 0, Qt.AlignLeft)
        else:
            info.addStretch(1)
        row.addLayout(info)

        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(2)

        if removable:
            close = self._small_button("×", tr('ui.caption.d796700ca7f0'), size=15)
            close.clicked.connect(lambda: self.remove_requested.emit(self.attachment_id))
            buttons.addWidget(close)
        buttons.addStretch(1)
        row.addLayout(buttons)

        if not self._resolved_path or not Path(self._resolved_path).is_file():
            title.setStyleSheet("color:#f59e0b; font-size:11px; font-weight:600;")
            set_label_icon(self.preview, 'warning', size=20)
            set_text(self.preview, 'ui.chat_attachments.file_unavailable_at_its_saved_path_0b3a', setter='setToolTip')

    def _small_button(self, text: str, tooltip: str, *, size: int = 13) -> QToolButton:
        btn = QToolButton(self)
        bind_caption(btn, text)
        bind_caption(btn, tooltip, setter='setToolTip')
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(20, 20)
        apply_style(btn, f"QToolButton {{ color:@text.secondary@; background:transparent; border:none; font-size:{size}px; padding:0; }}"
            "QToolButton:hover { color:@text.primary@; background:@control.hover@; border-radius:3px; }")
        return btn

    def _make_preview(self, media_type: str) -> _ClickablePreview:
        if media_type == "image" and self._resolved_path and Path(self._resolved_path).is_file():
            widget = _ClickablePreview(self)
            bind_caption(widget, "▧")
            widget.set_square_pixmap(QPixmap(self._resolved_path))
            return widget
        if media_type == "video":
            return _VideoThumbnail(self._resolved_path, self)
        if media_type == "audio":
            return _WaveformPreview(self._resolved_path, self)
        widget = _ClickablePreview(self)
        from ui.file_icons import bind_file_icon
        path = self._resolved_path or str(self.attachment.get('filename') or 'file')
        bind_file_icon(widget, path, 40)
        return widget

    def _project_copy_menu(self, pos):
        show_project_copy_menu(self, pos, self._resolved_path)

    def _open_file(self):
        # Do not keep an in-process video decoder alive while the OS launches
        # its default viewer/player for the same file.
        try:
            dispose_preview = getattr(self.preview, "dispose", None)
            if callable(dispose_preview):
                dispose_preview()
        except RuntimeError:
            pass
        if self._resolved_path and Path(self._resolved_path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._resolved_path))

    def _setup_player(self):
        if not QT_MEDIA_AVAILABLE or not self._resolved_path or not Path(self._resolved_path).is_file():
            return None
        try:
            player = QMediaPlayer(self)
            audio = QAudioOutput(self)
            player.setAudioOutput(audio)
            player.setSource(QUrl.fromLocalFile(self._resolved_path))
            self.player = player
            self.audio_output = audio
            return player
        except Exception:
            self.player = None
            self.audio_output = None
            return None

    def _play_pause(self, button: QPushButton):
        if self.player is None:
            self._open_file()
            return
        try:
            playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        except Exception:
            playing = False
        if playing:
            self.player.pause()
            symbol_button(button, "▶")
        else:
            self.player.play()
            set_icon(button, 'player-pause', square=True)

    def dispose(self):
        try:
            dispose_preview = getattr(self.preview, "dispose", None)
            if callable(dispose_preview):
                dispose_preview()
        except RuntimeError:
            pass

        player = self.player
        audio = self.audio_output
        self.player = None
        self.audio_output = None
        if player is not None:
            try:
                player.stop()
            except RuntimeError:
                pass
            try:
                player.setAudioOutput(None)
            except (RuntimeError, TypeError):
                pass
            try:
                player.setSource(QUrl())
            except RuntimeError:
                pass
        for obj in (player, audio):
            if obj is not None:
                try:
                    obj.deleteLater()
                except RuntimeError:
                    pass

    def event(self, event):
        if event.type() == QEvent.DeferredDelete:
            self.dispose()
        return super().event(event)


class MessageAttachmentsWidget(QWidget):
    attachment_removed = Signal(str)

    def __init__(
        self,
        attachments: List[Dict[str, Any]],
        path_resolver: Callable[[Dict[str, Any]], Optional[str]],
        *,
        removable: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.attachments = attachments
        self.path_resolver = path_resolver
        self.removable = removable
        self._cards: Dict[str, AttachmentMessageWidget] = {}
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 4, 0, 0)
        self.layout.setSpacing(6)
        self.rebuild()

    def _dispose_card(self, card: AttachmentMessageWidget) -> None:
        try:
            card.dispose()
        except RuntimeError:
            pass
        self.layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()

    def rebuild(self):
        # Rebuild is used for initial/session refreshes. Always detach multimedia
        # resources before scheduling QWidget deletion.
        while self.layout.count():
            item = self.layout.takeAt(0)
            widget = item.widget() if item else None
            if isinstance(widget, AttachmentMessageWidget):
                try:
                    widget.dispose()
                except RuntimeError:
                    pass
            if widget:
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()

        for number, attachment in enumerate(self.attachments, 1):
            attachment["position"] = number
            card = AttachmentMessageWidget(
                attachment,
                self.path_resolver,
                removable=self.removable,
                parent=self,
            )
            card.remove_requested.connect(self.attachment_removed)
            self.layout.addWidget(card, 0, Qt.AlignLeft)
            attachment_id = str(attachment.get("id") or "")
            if attachment_id:
                self._cards[attachment_id] = card
        self.setVisible(bool(self.attachments))

    def remove_attachment(self, attachment_id: str) -> None:
        """Remove one card without recreating every other multimedia widget."""
        key = str(attachment_id or "")
        card = self._cards.pop(key, None)
        if card is not None:
            self._dispose_card(card)
        for number, attachment in enumerate(self.attachments, 1):
            attachment['position'] = number
            other = self._cards.get(str(attachment.get('id') or ''))
            if other is not None:
                other.attachment['position'] = number
                title = str(number) + '. ' + str(attachment.get('filename') or 'file')
                bind_caption(other.title, other.title.fontMetrics().elidedText(title, Qt.ElideRight, other.title.width()))
        self.setVisible(bool(self.attachments))

    def dispose(self) -> None:
        for card in list(self._cards.values()):
            try:
                card.dispose()
            except RuntimeError:
                pass
        self._cards.clear()

    def event(self, event):
        if event.type() == QEvent.DeferredDelete:
            self.dispose()
        return super().event(event)

