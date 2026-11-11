from ui.themes.manager import theme_manager
from ui.styles.binding import watch_paint
from ui.i18n import tr
import sys
import math
import time
from typing import List, Dict, Any, Tuple
from PySide6.QtWidgets import QWidget, QToolTip
from PySide6.QtCore import Qt, QTimer, QRectF, QVariantAnimation, QEasingCurve, QPointF, Signal
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QMouseEvent, QFont

from ui.core_bridge.process_manager import core_process
from ui.core_bridge.module_manager import ModuleConfigSync
from ui.core_bridge.async_runner import AsyncRunner
from ui.components.orbital_motion import ROTATION_SPEED, failure_color, rendered_angles, ring_paths, module_ring_batches
from ui.components.module_icons import ModuleIcons



class OrbitalCoreHub(QWidget):
    """
    Хаб управления ядром с дополнительными кольцами для кастомных модулей.
    """
    spawn_started = Signal()
    motion_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("OrbitalCoreHub")
        watch_paint(self)
        self.setFixedSize(370, 370)
        self.setMouseTracking(True)

        self.module_sync = ModuleConfigSync()
        self.async_runner = AsyncRunner(self)
        self._toggle_in_flight = False
        self.system_modules: List[Dict[str, Any]] = []
        self.custom_modules: List[Dict[str, Any]] = []
        self.module_icons = ModuleIcons()

        self.GAP_PX = 8.0
        self.HALF_GAP = self.GAP_PX / 2.0
        self.RING_THICKNESS = 32.0

        self.r_btn = 70.0
        self.r_sys_in = self.r_btn + self.GAP_PX               # 78px
        self.r_sys_out = self.r_sys_in + self.RING_THICKNESS  # 110px
        self.r_cust_in = self.r_sys_out + self.GAP_PX         # 118px
        self.r_cust_out = self.r_cust_in + self.RING_THICKNESS # 150px

        self.is_running = core_process.is_running()
        self.is_failed = core_process.has_failed()
        self.rot_angle_sys = 0.0
        self.rot_angle_cust = 0.0
        self.spin_delta = 0.0
        self.fault_elapsed = 0.0
        self._last_spin_time = None
        self.expand_progress = 1.0

        self.hovered_btn = False
        self.hovered_ring = None
        self.hovered_slice_idx = -1

        self.spin_timer = QTimer(self)
        self.spin_timer.setInterval(16)
        self.spin_timer.timeout.connect(self._on_spin_tick)

        self.spawn_anim = QVariantAnimation(self)
        self.spawn_anim.setDuration(600)
        self.spawn_anim.setEasingCurve(QEasingCurve.OutBack)
        self.spawn_anim.valueChanged.connect(self._on_anim_progress)

        core_process.status_changed.connect(self._on_core_status_changed)
        core_process.failure_changed.connect(self._on_failure_changed)
        from ui.event_bus import event_bus
        event_bus.pipeline_resources_changed.connect(self.reload_modules)

        self.reload_modules()
        self.play_spawn_animation()
        self._sync_spin_timer()

    def reload_modules(self):
        self.system_modules = self.module_sync.get_system_modules_data()
        self.custom_modules = self.module_sync.get_custom_modules_data()
        self.module_icons.reload([*self.system_modules, *self.custom_modules])
        self.update()

    def play_spawn_animation(self):
        self.spawn_started.emit()
        self.spawn_anim.stop()
        self.spawn_anim.setStartValue(0.2)
        self.spawn_anim.setEndValue(1.0)
        self.spawn_anim.start()

    def _on_anim_progress(self, val):
        self.expand_progress = float(val)
        self.update()

    def _on_spin_tick(self):
        now = time.monotonic()
        elapsed = min(0.1, max(0.0, now - (self._last_spin_time or now)))
        self._last_spin_time = now
        self.spin_delta = elapsed if self.is_running and not self.is_failed else 0.0
        if self.is_failed:
            previous = self._rendered_angles()
            self.fault_elapsed += elapsed
            if self._rendered_angles() != previous:
                self.update()
        elif self.is_running:
            self.rot_angle_sys = (self.rot_angle_sys - ROTATION_SPEED * elapsed) % 360.0
            self.rot_angle_cust = (self.rot_angle_cust + ROTATION_SPEED * elapsed) % 360.0
            self.update()

    def _sync_spin_timer(self):
        self.spin_timer.setInterval(33)
        if (
            (self.is_running or self.is_failed)
            and self.isVisible()
            and not self.window().isMinimized()
        ):
            if not self.spin_timer.isActive():
                self._last_spin_time = time.monotonic()
                self.spin_timer.start()
        else:
            self.spin_timer.stop()
            self.spin_delta = 0.0
            self._last_spin_time = None

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_spin_timer()

    def hideEvent(self, event):
        self.spin_timer.stop()
        self.spin_delta = 0.0
        self._last_spin_time = None
        super().hideEvent(event)

    def _on_failure_changed(self, failed):
        self.is_failed = failed
        self.fault_elapsed = 0.0
        self.spin_delta = 0.0
        self._last_spin_time = time.monotonic()
        self._sync_spin_timer()
        self.update()
        self.motion_changed.emit()

    def _rendered_angles(self):
        return rendered_angles(
            (self.rot_angle_sys, self.rot_angle_cust), self.is_failed, self.fault_elapsed
        )

    def _on_core_status_changed(self, is_online: bool, status_key: str):
        self.is_running = is_online
        self._sync_spin_timer()

        self.reload_modules()
        self.update()
        self.motion_changed.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        painter.translate(cx, cy)
        painter.scale(self._orbital_scale(), self._orbital_scale())
        cx = cy = 0.0
        system_angle, custom_angle = self._rendered_angles()

        # Внешнее кольцо (Кастомные модули)
        for modules, inner, outer, offset in self._custom_rings():
            self._draw_ring_parallel(
                painter, cx, cy, modules=modules, r_in=inner, r_out=outer,
                rot_angle=custom_angle, ring_type="cust", module_offset=offset)

        # Внутреннее кольцо (Системные модули)
        self._draw_ring_parallel(
            painter, cx, cy,
            modules=self.system_modules,
            r_in=self.r_sys_in,
            r_out=self.r_sys_out,
            rot_angle=system_angle,
            ring_type="sys"
        )

        # Центральная кнопка
        self._draw_center_button(painter, cx, cy)

    def _custom_rings(self):
        return [(modules, self.r_cust_in + row * (self.RING_THICKNESS + self.GAP_PX),
                 self.r_cust_out + row * (self.RING_THICKNESS + self.GAP_PX), offset)
                for row, (offset, modules) in enumerate(module_ring_batches(self.custom_modules))]

    def _orbital_scale(self):
        return self.r_cust_out / self._custom_rings()[-1][2]

    def _draw_ring_parallel(
        self,
        painter: QPainter,
        cx: float,
        cy: float,
        modules: List[Dict[str, Any]],
        r_in: float,
        r_out: float,
        rot_angle: float,
        ring_type: str,
        module_offset: int = 0,
    ):
        num_modules = len(modules)
        num_slices = max(2, num_modules)

        cur_r_in = r_in * self.expand_progress
        cur_r_out = r_out * self.expand_progress

        if cur_r_in <= self.HALF_GAP + 1 or cur_r_out <= cur_r_in + 2:
            return

        painter.save()
        painter.translate(cx, cy)
        painter.rotate(-rot_angle)
        icons = []
        for i, path in enumerate(
            ring_paths(cur_r_in, cur_r_out, num_slices, self.HALF_GAP)
        ):

            if i < num_modules:
                mod = modules[i]
                is_on = mod["enabled"]
                if is_on:
                    base_c = QColor(mod["color"])
                    if not self.is_running:
                        fill_color = QColor.fromRgbF(
                            base_c.redF() * 0.8, base_c.greenF() * 0.8, base_c.blueF() * 0.8
                        )
                        border_color = fill_color.lighter(115)
                    else:
                        fill_color = base_c
                        border_color = base_c.lighter(120)
                else:
                    fill_color = theme_manager.color('control.disabled')
                    border_color = theme_manager.color('border.subtle')
            else:
                fill_color = theme_manager.color('background.input')
                border_color = theme_manager.color('border.subtle')

            if self.is_failed:
                fill_color = failure_color(i + module_offset, ring_type == "cust")
                border_color = fill_color.lighter(120)

            if not self.is_failed and i < num_modules and modules[i].get("pending_disable"):
                border_color = QColor("#eab308")

            if self.hovered_ring == ring_type and self.hovered_slice_idx == i + module_offset:
                fill_color = fill_color.lighter(130)
                border_color = (
                    fill_color.lighter(140) if self.is_failed else QColor("#ffffff")
                )

            painter.setBrush(QBrush(fill_color))
            painter.setPen(QPen(border_color, 1.5))
            painter.drawPath(path)
            if i < num_modules:
                radius = (cur_r_in + cur_r_out) / 2.0
                room = 2 * radius * math.sin(math.pi / num_slices) - self.GAP_PX
                size = min(20 * self.expand_progress,
                           cur_r_out - cur_r_in - 8 * self.expand_progress, room - 4)
                if size >= 5:
                    angle = math.radians((i + 0.5) * 360 / num_slices + rot_angle)
                    icons.append((modules[i], QRectF(cx + radius * math.cos(angle) - size / 2,
                                 cy - radius * math.sin(angle) - size / 2, size, size)))
        painter.restore()
        # Draw icons outside the rotating transform, in fractional widget
        # coordinates. Their orientation and raster sampling stay independent
        # of the ring's rotation.
        for module, rect in icons:
            self.module_icons.paint(painter, module, rect, moving=True)

    def _draw_center_button(self, painter: QPainter, cx: float, cy: float):
        btn_rect = QRectF(cx - self.r_btn, cy - self.r_btn, 2 * self.r_btn, 2 * self.r_btn)

        if self.is_running:
            btn_bg = QColor("#ffffff") if not self.hovered_btn else QColor("#f4f4f5")
            border_color = QColor("#d4d4d8")
            text_color = QColor("#09090b")
            text_label = "Toshy"
        elif core_process.is_starting():
            btn_bg = theme_manager.color('background.surface')
            border_color = QColor("#ca8a04")
            text_color = QColor("#facc15")
            text_label = "Starting..."
        else:
            btn_bg = theme_manager.color('control.hover' if self.hovered_btn else 'control.disabled')
            border_color = theme_manager.color('border.subtle')
            text_color = theme_manager.color('text.primary')
            text_label = "Startup"

        painter.setBrush(QBrush(btn_bg))
        painter.setPen(QPen(border_color, 2.5))
        painter.drawEllipse(btn_rect)

        painter.setPen(QPen(text_color))
        font_size = 13 if core_process.is_starting() else 16

        font = QFont()
        font.setPointSize(font_size)
        font.setBold(True)
        font.setStyleHint(QFont.SansSerif)
        if sys.platform == "win32":
            font.setFamily("Segoe UI")
        else:
            font.setFamilies(["Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans", "sans-serif"])

        painter.setFont(font)
        painter.drawText(btn_rect, Qt.AlignCenter, text_label)

    def _get_polar_hit(self, pos: QPointF) -> Tuple[str, int]:
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        scale = self._orbital_scale()
        dx = (pos.x() - cx) / scale
        dy = (pos.y() - cy) / scale
        dist = math.hypot(dx, dy)

        if dist <= self.r_btn:
            return "btn", -1

        angle_deg = (math.degrees(math.atan2(-dy, dx))) % 360.0
        system_angle, custom_angle = self._rendered_angles()

        if self.r_sys_in <= dist <= self.r_sys_out:
            num_slices = max(2, len(self.system_modules))
            step_deg = 360.0 / num_slices
            alpha_d = math.degrees(math.asin(min(0.99, self.HALF_GAP / dist)))
            rel_angle = (angle_deg - system_angle) % 360.0
            slice_idx = int(rel_angle // step_deg)
            in_slice_deg = rel_angle % step_deg
            if alpha_d <= in_slice_deg <= (step_deg - alpha_d):
                return "sys", slice_idx

        for modules, inner, outer, offset in self._custom_rings():
            if inner <= dist <= outer:
                num_slices = max(2, len(modules))
                step_deg = 360.0 / num_slices
                alpha_d = math.degrees(math.asin(min(0.99, self.HALF_GAP / dist)))
                rel_angle = (angle_deg - custom_angle) % 360.0
                slice_idx = int(rel_angle // step_deg)
                in_slice_deg = rel_angle % step_deg
                if alpha_d <= in_slice_deg <= (step_deg - alpha_d):
                    return "cust", offset + slice_idx

        return "none", -1

    def mouseMoveEvent(self, event: QMouseEvent):
        hit_type, idx = self._get_polar_hit(event.position())

        old_hover_btn = self.hovered_btn
        old_hover_ring = self.hovered_ring
        old_hover_slice = self.hovered_slice_idx

        self.hovered_btn = (hit_type == "btn")
        self.hovered_ring = hit_type if hit_type in ("sys", "cust") else None
        self.hovered_slice_idx = idx if self.hovered_ring else -1

        if (self.hovered_btn != old_hover_btn) or (self.hovered_ring != old_hover_ring) or (self.hovered_slice_idx != old_hover_slice):
            self.update()

        if hit_type == "btn":
            if self.is_failed:
                tip = tr('ui.caption.dfaf3e3cdb8d')
            elif core_process.is_starting():
                tip = tr('ui.caption.503cc75e6122')
            elif self.is_running:
                tip = tr("btn_stop_core")
            else:
                tip = tr("btn_start_core")
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        elif hit_type == "sys" and idx < len(self.system_modules):
            m = self.system_modules[idx]
            status_tag = tr('ui.caption.75c72ef50c4d') if m.get("pending_disable") else tr('ui.caption.cd07e6ebcd5c') if m["enabled"] else tr('ui.caption.b0486f77db44')
            QToolTip.showText(event.globalPosition().toPoint(), tr('ui.caption.091441193aff', v0=m.get('display_name') or m['name'], v1=status_tag), self)
        elif hit_type == "cust" and idx < len(self.custom_modules):
            m = self.custom_modules[idx]
            status_tag = tr('ui.caption.75c72ef50c4d') if m.get("pending_disable") else tr('ui.caption.cd07e6ebcd5c') if m["enabled"] else tr('ui.caption.b0486f77db44')
            QToolTip.showText(event.globalPosition().toPoint(), tr('ui.caption.aa52c68bce41', v0=m.get('display_name') or m['name'], v1=status_tag), self)
        else:
            QToolTip.hideText()

        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hovered_btn = False
        self.hovered_ring = None
        self.hovered_slice_idx = -1
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            hit_type, idx = self._get_polar_hit(event.position())

            if hit_type == "btn":
                if core_process.is_starting():
                    QToolTip.showText(event.globalPosition().toPoint(), tr('ui.caption.49ba94fcdf33'), self)
                    event.accept()
                    return

                if self.is_running:
                    core_process.stop_core()
                else:
                    core_process.start_core()
                event.accept()
                return

            if hit_type == "sys" and idx < len(self.system_modules):
                target_mod = self.system_modules[idx]["name"]
                self._toggle_module(target_mod, "system")
                event.accept()
                return

            if hit_type == "cust" and idx < len(self.custom_modules):
                target_mod = self.custom_modules[idx]["name"]
                self._toggle_module(target_mod, "custom")
                event.accept()
                return

        super().mousePressEvent(event)

    def _toggle_module(self, module_name: str, category: str):
        if self._toggle_in_flight:
            return
        self._toggle_in_flight = True

        def done(_enabled):
            self._toggle_in_flight = False
            self.reload_modules()
            from ui.core_bridge.subagents_client import subagents_client
            subagents_client().refresh()

        def failed(error):
            self._toggle_in_flight = False
            QToolTip.showText(self.mapToGlobal(self.rect().center()), tr('ui.caption.3d2bf639ec43', v0=error), self)

        self.async_runner.submit(
            lambda: self.module_sync.toggle_module(module_name, category, self.is_running),
            done,
            failed,
        )
