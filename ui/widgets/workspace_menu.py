"""Workspace menus with quick actions above the separate rounded action list."""
from PySide6.QtCore import Qt,QRectF
from PySide6.QtGui import QPainter,QPen
from PySide6.QtWidgets import QMenu,QWidget,QHBoxLayout,QToolButton,QWidgetAction,QLabel
from ui.styles.binding import apply_style,watch_paint
from ui.themes.manager import theme_manager
from ui.icons import set_icon
from ui.i18n import bind_caption

MENU_WIDTH=256
BUTTON_SIDE=28


class WorkspaceContextMenu(QMenu):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.quick_bar=QWidget(self)
        self.quick_bar.setObjectName('WorkspaceContextQuickActions')
        self.quick_layout=QHBoxLayout(self.quick_bar)
        self.quick_layout.setContentsMargins(0,0,0,0)
        self.quick_layout.setSpacing(5)
        self.quick_layout.addStretch(1)
        # A real action reserves the quick row. QMenu counts a top-only QSS
        # padding twice in its size hint, leaving the same gap at the bottom.
        self._quick_space = QLabel("", self)
        self._quick_space.setMinimumWidth(1)
        self._quick_space.setFixedHeight(36)
        self._quick_space.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._quick_space.setStyleSheet('background:transparent;border:0;')
        self._quick_space_action = QWidgetAction(self)
        self._quick_space_action.setDefaultWidget(self._quick_space)
        self.addAction(self._quick_space_action)
        watch_paint(self)
        standardize_workspace_menu(self)

    def add_quick_action(self,icon,tooltip,callback=None):
        button=QToolButton(self.quick_bar)
        set_icon(button,icon,size=18)
        bind_caption(button,tooltip,setter='setToolTip')
        button.setFixedSize(BUTTON_SIDE,BUTTON_SIDE)
        button.setCursor(Qt.PointingHandCursor)
        self.quick_layout.insertWidget(self.quick_layout.count()-1,button)
        if callback is not None:
            button.clicked.connect(lambda:(self.close(),callback()))
        return button

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.quick_bar.setGeometry(0,0,self.width(),BUTTON_SIDE)
        self.quick_bar.raise_()

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(theme_manager.color('border.subtle'),1))
        painter.setBrush(theme_manager.color('background.panel'))
        painter.drawRoundedRect(QRectF(.5,36.5,self.width()-1,self.height()-37),7,7)
        painter.end()
        super().paintEvent(event)


def standardize_workspace_menu(menu):
    menu.setMinimumWidth(MENU_WIDTH)
    surface=('background:transparent;border:0;padding:5px;'
             if isinstance(menu,WorkspaceContextMenu) else
             'background:@background.panel@;border:1px solid @border.subtle@;border-radius:7px;padding:5px;')
    apply_style(menu, 'QMenu {color:@text.primary@;'+surface+'''}
        QMenu::item {padding:6px 10px;border:0;border-radius:4px;}
        QMenu::item:selected {background:@control.hover@;}
        QMenu::item:disabled {color:@text.disabled@;}
        QMenu::separator {height:1px;background:@border.subtle@;margin:5px 6px;}
        QWidget#WorkspaceContextQuickActions {background:transparent;border:0;}
        QToolButton {background:@background.surface@;color:@text.primary@;border:1px solid @border.subtle@;border-radius:4px;padding:0;min-width:26px;max-width:26px;min-height:26px;max-height:26px;}
        QToolButton:hover {background:@control.hover@;}
    ''')
    return menu
