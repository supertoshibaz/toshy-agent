"""Shared orbital motion curves; the Startup hub owns the only frame timer."""

import math
from functools import lru_cache
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainterPath

ROTATION_SPEED = 4.6875
FAILURE_COLORS = ("#991b1b", "#dc2626", "#7f1d1d", "#ef4444", "#b91c1c", "#a61b29")
MODULES_PER_RING = 16


def module_ring_batches(modules):
    """Balanced, contiguous rings retaining each module's original hit index."""
    count = max(1, math.ceil(len(modules) / MODULES_PER_RING))
    size = max(1, math.ceil(len(modules) / count))
    return [(offset, modules[offset:offset + size])
            for offset in range(0, max(1, len(modules)), size)]


@lru_cache(maxsize=128)
def ring_paths(inner, outer, count, half_gap):
    """Local, unrotated sector geometry. Callers draw but never mutate it."""
    alpha_inner = math.degrees(math.asin(half_gap / inner))
    alpha_outer = math.degrees(math.asin(half_gap / outer))
    step = 360.0 / count
    if step <= 2 * max(alpha_inner, alpha_outer):
        return ()
    outer_rect = QRectF(-outer, -outer, 2 * outer, 2 * outer)
    inner_rect = QRectF(-inner, -inner, 2 * inner, 2 * inner)
    paths = []
    for index in range(count):
        start = index * step
        path = QPainterPath()
        path.arcMoveTo(outer_rect, start + alpha_outer)
        path.arcTo(outer_rect, start + alpha_outer, step - 2 * alpha_outer)
        path.arcTo(inner_rect, start + step - alpha_inner, -(step - 2 * alpha_inner))
        path.closeSubpath()
        paths.append(path)
    return tuple(paths)


def failure_color(index, ring=0):
    return QColor(FAILURE_COLORS[(index + ring * 2) % len(FAILURE_COLORS)])


def jam_offset(elapsed):
    """Try to turn, tremble against resistance, then recoil to the same tooth."""
    t = elapsed % 1.8
    if t < 0.28:
        return 5.0 * (1.0 - (1.0 - t / 0.28) ** 3)
    if t < 0.55:
        return 5.0 + 0.32 * math.sin((t - 0.28) * 110.0)
    if t < 0.70:
        p = (t - 0.55) / 0.15
        start = 5.0 + 0.32 * math.sin(0.27 * 110.0)
        return start * (1.0 - p * p * (3.0 - 2.0 * p))
    if t < 1.10:
        p = (t - 0.70) / 0.40
        return -0.9 * math.sin(p * math.pi * 4.0) * (1.0 - p) ** 2
    return 0.0


def rendered_angles(angles, failed, elapsed):
    if not failed:
        return angles
    kick = jam_offset(elapsed)
    return angles[0] - kick, angles[1] + kick * 0.85
