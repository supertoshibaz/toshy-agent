"""Module-owned icons loaded on metadata refresh, never on an animation frame."""

from pathlib import Path
from PySide6.QtCore import Qt, QRectF, QSizeF, QPointF, QByteArray
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


DEFAULT_ICON = Path(__file__).resolve().parent.parent / 'assets' / 'tabler' / 'puzzle.svg'


class ModuleIcons:
    def __init__(self):
        self.rasters = {}
        self.icons = {}
        self.files = {}

    @staticmethod
    def _key(module):
        return module.get('category', ''), module.get('name', '')

    @staticmethod
    def _load(path):
        if path == DEFAULT_ICON:
            return 'builtin:module'
        if path.suffix.lower() == '.svg':
            try:
                data = QByteArray(path.read_bytes())
            except OSError:
                return None
            renderer = QSvgRenderer(data)
            if not renderer.isValid():
                return None
            # Module icons are static; do not run an SVG animation timer.
            disable_animation = getattr(renderer, 'setAnimationEnabled', None)
            if disable_animation is not None:
                disable_animation(False)
            else:
                renderer.setFramesPerSecond(0)
            return renderer
        icon = QIcon(str(path))
        normal = icon.pixmap(64, 64, QIcon.Normal)
        if normal.isNull():
            return None
        return normal, icon.pixmap(64, 64, QIcon.Disabled)

    def reload(self, modules):
        icons, files = {}, {}
        for module in modules:
            directory = module.get('ui_dir')
            path = Path(directory) / str(module.get('icon') or 'icon.svg') if directory else DEFAULT_ICON
            try:
                if not path.is_file():
                    path = DEFAULT_ICON
                stat = path.stat()
                signature = (str(path), stat.st_mtime_ns, stat.st_size)
            except OSError:
                signature = (str(DEFAULT_ICON), None, None)
                path = DEFAULT_ICON
            images = self.files.get(signature)
            if images is None:
                images = self._load(path)
                if images is None:
                    images = self._load(DEFAULT_ICON)
            files[signature] = images
            icons[self._key(module)] = images
        self.icons, self.files = icons, files
        self.rasters.clear()

    def _paint_source(self, painter, module, rect):
        images = self.icons.get(self._key(module))
        if images == 'builtin:module':
            from ui.icons import paint
            paint(painter, rect, 'module', enabled=bool(module.get('enabled')))
            return
        if images is not None:
            vector = isinstance(images, QSvgRenderer)
            image = None if vector else images[0 if module.get('enabled') else 1]
            source_size = QSizeF(images.defaultSize() if vector else image.size())
            if source_size.isEmpty():
                return
            size = source_size.scaled(rect.size(), Qt.KeepAspectRatio)
            target = QRectF(QPointF(), size)
            target.moveCenter(rect.center())
            painter.save()
            try:
                painter.setRenderHint(QPainter.Antialiasing)
                if vector:
                    if not module.get('enabled'):
                        painter.setOpacity(painter.opacity() * 0.45)
                    # Keep fractional coordinates and rasterize the original
                    # paths at the final device scale on each cache miss.
                    images.render(painter, target)
                else:
                    painter.setRenderHint(QPainter.SmoothPixmapTransform)
                    painter.drawPixmap(target, image, QRectF(image.rect()))
            finally:
                painter.restore()

    def paint(self, painter, module, rect, *, moving=False):
        if moving:
            # Reuse the loaded SVG renderer, not a tiny raster whose sampling
            # changes at every fractional orbital position.
            self._paint_source(painter, module, rect)
            return
        from ui.themes.manager import theme_manager
        scale=painter.device().devicePixelRatioF()
        width,height=max(1,round(rect.width()*scale)),max(1,round(rect.height()*scale))
        key=(self._key(module),bool(module.get('enabled')),width,height,round(scale,3),id(theme_manager.current))
        image=self.rasters.get(key)
        if image is None:
            if len(self.rasters)>=256:self.rasters.clear()
            image=QPixmap(width,height);image.setDevicePixelRatio(scale);image.fill(Qt.transparent)
            buffer=QPainter(image)
            try:
                buffer.setRenderHint(QPainter.Antialiasing)
                self._paint_source(buffer,module,QRectF(0,0,width/scale,height/scale))
            finally:buffer.end()
            self.rasters[key]=image
        painter.save()
        try:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawPixmap(rect,image,QRectF(image.rect()))
        finally:painter.restore()
