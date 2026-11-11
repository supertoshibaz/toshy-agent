from ui.themes.manager import theme_manager
from ui.styles.binding import watch_paint
"""Saved subagent templates, painted together without per-agent timers/widgets."""
from ui.i18n import tr

import math
from PySide6.QtCore import Qt, QRectF, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QPainter, QPen, QColor, QFont, QRegion
from PySide6.QtWidgets import QWidget, QToolTip
from ui.core_bridge.subagents_client import subagents_client
from ui.event_bus import event_bus
from ui.components.orbital_motion import (
    ROTATION_SPEED,
    failure_color,
    rendered_angles,
    ring_paths,
    module_ring_batches,
)


class SubagentOrbitals(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        watch_paint(self)
        self.hub = hub
        self.client = subagents_client()
        self.records = {}
        self.pending = {}
        self.positions = []
        self._angles = {}
        self._last_frame = ()
        self.enabled = False
        self.progress = 0.0
        self.setMouseTracking(True)
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(600)
        self.animation.valueChanged.connect(self._progress)
        self.animation.finished.connect(self._finished)
        self.client.changed.connect(self.refresh)
        self.client.failed.connect(
            lambda text: (
                QToolTip.showText(self.mapToGlobal(self.rect().center()), text, self)
                if self.isVisible()
                else None
            )
        )
        hub.spawn_started.connect(self.spawn)
        hub.motion_changed.connect(self.update)
        hub.spin_timer.timeout.connect(self._on_spin_tick)
        self.refresh(self.client.data)

    def refresh(self, data):
        enabled = bool(data.get("module_enabled"))
        # Retain the old picture while it collapses after a module disable.
        if enabled or not self.records:
            self.records = data.get("subagents") or {}
        self._angles = {
            identity: self._angles.get(identity, self._initial_angles(identity))
            for identity in self.records
        }
        self.pending = data.get("pending") or {}
        self.hub.reload_modules()
        if enabled != self.enabled:
            self.enabled = enabled
            self._animate(1.0 if enabled else 0.0)
        self._layout()
        self.update()

    def _on_spin_tick(self):
        if not self.isVisible() or self.progress <= 0:
            return
        if self.enabled and any(cfg.get("enabled") for cfg in self.records.values()):
            delta = ROTATION_SPEED * self.hub.spin_delta
            frame = []
            for identity, cfg in self.records.items():
                if cfg.get("enabled"):
                    system, custom = self._angles[identity]
                    self._angles[identity] = (system - delta) % 360, (
                        custom + delta
                    ) % 360
                    frame.append((identity, self._rendered_angles(identity, True)))
            frame = tuple(frame)
            if frame != self._last_frame:
                self._last_frame = frame
                self.update()

    @staticmethod
    def _initial_angles(identity):
        phase = (
            sum((index + 1) * ord(char) for index, char in enumerate(identity)) % 360
        )
        return float(phase), float((-phase) % 360)

    def _rendered_angles(self, identity, active):
        if not active or not self.hub.is_failed:
            return self._angles[identity]
        return rendered_angles(
            self._angles[identity],
            True,
            self.hub.fault_elapsed
            * (0.9 + self._initial_angles(identity)[0] % 7 * 0.035),
        )

    def _animate(self, target):
        self.animation.stop()
        if target:
            self.show()
            self.raise_()
        self.animation.setEasingCurve(
            QEasingCurve.OutBack if target else QEasingCurve.InBack
        )
        self.animation.setStartValue(self.progress)
        self.animation.setEndValue(target)
        self.animation.start()

    def spawn(self):
        if self.enabled:
            self.progress = 0.0
            self._animate(1.0)

    def _progress(self, value):
        self.progress = max(0.0, float(value))
        self.update()

    def _finished(self):
        if not self.enabled:
            self.hide()

    def _layout(self):
        self.positions = []
        records = list(self.records)
        if not records:
            self.hide()
            return
        width, height = self.width(), self.height()
        gap = 8.0
        side = max(1.0, (width - min(330.0, max(0.0, width - 60.0))) / 2 - 8)
        largest = math.ceil(len(records) / 2)
        # Reserve a 2x2 area on each side from the beginning, so adding the
        # third/eighth template changes placement rather than its size.
        target = max(1.0, min(180.0, (side - gap) / 2, (height - 24 - gap) / 2))
        columns = 1 if largest <= 2 else 2
        if largest > 4:
            columns = max(2, math.ceil(math.sqrt(largest * side / max(1, height - 20))))
        rows = math.ceil(largest / columns)
        diameter = min(
            target,
            (side - gap * (columns - 1)) / columns,
            (height - 20 - gap * (rows - 1)) / rows,
        )
        diameter = max(1.0, diameter)
        mask = QRegion()
        for index, identity in enumerate(records):
            right, cell = index % 2, index // 2
            count = len(records) // 2 if right else math.ceil(len(records) / 2)
            local_columns = 1 if count <= 2 else columns
            local_rows = math.ceil(count / local_columns)
            block_width = local_columns * diameter + (local_columns - 1) * gap
            block_height = local_rows * diameter + (local_rows - 1) * gap
            origin = width - side - 4 if right else 4
            x = (
                origin
                + (side - block_width) / 2
                + (cell % local_columns) * (diameter + gap)
            )
            y = (height - block_height) / 2 + (cell // local_columns) * (diameter + gap)
            rect = QRectF(x, y, diameter, diameter)
            self.positions.append((identity, rect))
            mask |= QRegion(rect.toAlignedRect(), QRegion.Ellipse)
        self.setMask(mask)
        if self.enabled:
            self.show()
            self.raise_()

    def showEvent(self, event):
        super().showEvent(event)
        if self.enabled and event.spontaneous():
            self.spawn()

    def resizeEvent(self, event):
        self._layout()
        super().resizeEvent(event)

    def paintEvent(self, event):
        if self.progress <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        for identity, rect in self.positions:
            cfg = self.records[identity]
            painter.save()
            painter.translate(rect.center())
            active = bool(cfg.get("enabled"))
            failed = active and self.hub.is_failed
            system_angle, custom_angle = self._rendered_angles(identity, active)
            rings = []
            for modules, radius, angle in (
                (self.hub.system_modules, 94, system_angle),
                (self.hub.custom_modules, 134, custom_angle),
            ):
                allowed = [
                    m
                    for m in modules
                    if m["name"] in cfg.get("allowed_modules", [])
                    and m["name"] != "subagent_manager"
                ]
                batches = module_ring_batches(allowed) if radius == 134 else [(0, allowed)]
                for row, (offset, batch) in enumerate(batches):
                    rings.append((batch, radius + row * 40, angle, offset, radius == 134))
            outermost = max(radius + 13 for _, radius, _, _, _ in rings)
            scale = rect.width() / 320 * self.progress * 147 / outermost
            painter.scale(scale, scale)
            for allowed, radius, angle, offset, custom in rings:
                count = max(2, len(allowed))
                inner, outer = radius - 13, radius + 13
                painter.save()
                painter.rotate(-angle)
                for index, path in enumerate(ring_paths(inner, outer, count, 4.0)):
                    module = allowed[index] if index < len(allowed) else {}
                    color = (
                        QColor(module.get("color", "#242424"))
                        if active and module.get("enabled")
                        else theme_manager.color('control.disabled')
                    )
                    if failed:
                        color = failure_color(index + offset, custom)
                    painter.setPen(QPen(color.lighter(115), 1.0))
                    painter.setBrush(color)
                    painter.drawPath(path)
                painter.restore()
            painter.setBrush(QColor('#262630') if active else theme_manager.color('control.disabled'))
            painter.setPen(
                QPen(
                    QColor(
                        "#eab308"
                        if identity in self.pending
                        else '#818cf8' if active else theme_manager.value('border.subtle')
                    ),
                    2.5,
                )
            )
            painter.drawEllipse(QRectF(-68, -68, 136, 136))
            painter.setPen(QColor('#e4e4e7') if active else theme_manager.color('text.disabled'))
            font = QFont(self.font())
            font.setPixelSize(22)
            font.setBold(True)
            painter.setFont(font)
            name = str(cfg.get("name") or identity)
            label = name if len(name) < 14 else name[:12] + "…"
            painter.drawText(
                QRectF(-62, -48, 124, 96), Qt.AlignCenter | Qt.TextWordWrap, label
            )
            painter.restore()

    def _hit(self, pos):
        for identity, rect in self.positions:
            distance = math.hypot(
                pos.x() - rect.center().x(), pos.y() - rect.center().y()
            )
            if distance <= rect.width() / 2 * min(1, self.progress):
                return identity, distance <= rect.width() * 68 / 320 * min(
                    1, self.progress
                )
        return "", False

    def mouseMoveEvent(self, event):
        identity, center = self._hit(event.position())
        if identity:
            cfg = self.records[identity]
            state = tr('ui.caption.35cfe273a48c') if cfg.get("enabled") else tr('ui.caption.2d2d65e8e86a')
            tip = (
                str(cfg.get("name") or identity)
                + " · "
                + state
                + "\n"
                + str(cfg.get("description") or "")
            )
            tip += tr('ui.caption.d4ea1811541a')
            if identity in self.pending:
                tip += tr('ui.caption.8e344c2a322b')
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
            self.setCursor(Qt.PointingHandCursor)
        else:
            QToolTip.hideText()
            self.unsetCursor()

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or not self.enabled:
            return
        identity, center = self._hit(event.position())
        if not identity:
            return
        if center:
            cfg = self.pending.get(identity, self.records[identity])
            self.client.mutate("save", identity, {"enabled": not cfg.get("enabled")})
        else:
            event_bus.subagent_settings_requested.emit(identity)
