"""Lazy exports avoid a BottomPanel ↔ Startup settings import cycle."""
__all__ = ["TopBar", "BottomPanel", "OrbitalCoreHub"]


def __getattr__(name):
    if name == 'TopBar':
        from .top_bar import TopBar
        return TopBar
    if name == 'BottomPanel':
        from .bottom_panel import BottomPanel
        return BottomPanel
    if name == 'OrbitalCoreHub':
        from .orbital_core_hub import OrbitalCoreHub
        return OrbitalCoreHub
    raise AttributeError(name)
