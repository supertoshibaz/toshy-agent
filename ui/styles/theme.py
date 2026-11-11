"""Shared shell QSS; specialized document and scrollbar rules retain their ownership."""
from pathlib import Path
from ui.themes.colors import resolve_values

EDITOR_TEXT_SIZE = 14

_TEMPLATE = """



/* =========================================================================
   БАЗОВАЯ НЕЙТРАЛЬНАЯ ГРАФИТОВО-СЕРАЯ ПАЛИТРА
   ========================================================================= */
QWidget {
    background-color: transparent;
    color: @text.primary@;
    font-size: 12px;
}

/* =========================================================================
   СКРОЛЛБАРЫ В СТИЛЕ OBSIDIAN (ГРАФИТОВЫЙ ПИЛЛ БЕЗ СТРЕЛОК)
   ========================================================================= */
QScrollBar:vertical {
    border: none;
    background: transparent;
    width: 8px;
    margin: 0px;
}

QScrollBar::handle:vertical {
    background: #2e2e2e;
    min-height: 28px;
    border-radius: 4px;
    margin: 2px 1px;
}

QScrollBar::handle:vertical:hover {
    background: @accent.primary@;
}

QScrollBar::handle:vertical:pressed {
    background: @accent.primary@;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
    width: 0px;
    border: none;
    background: none;
}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}

QScrollBar:horizontal {
    border: none;
    background: transparent;
    height: 8px;
    margin: 0px;
}

QScrollBar::handle:horizontal {
    background: #2e2e2e;
    min-width: 28px;
    border-radius: 4px;
    margin: 1px 2px;
}

QScrollBar::handle:horizontal:hover {
    background: @accent.primary@;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
    height: 0px;
    border: none;
    background: none;
}

QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none;
}

/* Верхняя панель */
#TopBar {
    background-color: @background.window@;
    border-bottom: 1px solid @border.subtle@;
    min-height: 40px;
    max-height: 40px;
}

QPushButton, QToolButton {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    color: @text.primary@;
    padding: 4px 8px;
    font-weight: 500;
}

QPushButton:hover, QToolButton:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
}

QPushButton:pressed, QToolButton:pressed {
    background-color: @control.pressed@;
    border-color: @border.subtle@;
}

/* Кнопки App, About, Help */
#TopBarAppBtn, #TopBarAboutBtn, #TopBarHelpBtn, #TopBarExtraBtn {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    font-size: 11px;
    color: @text.primary@;
}

#TopBarAppBtn:hover, #TopBarAboutBtn:hover, #TopBarHelpBtn:hover, #TopBarExtraBtn:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
    color: @text.primary@;
}

/* Вкладки рабочих областей */
#TopBar QTabBar::tab {
    background: transparent;
    color: @text.disabled@;
    padding: 8px 18px;
    border-bottom: 2px solid transparent;
    font-weight: bold;
    font-size: 12px;
    min-width: 65px;
}

#TopBar QTabBar::tab:hover {
    color: @text.primary@;
    background: @control.hover@;
}

#TopBar QTabBar::tab:selected {
    color: @text.primary@;
    background: @control.selected@;
    border-bottom: 2px solid @text.primary@;
}

/* Док-панели */
QTabWidget::pane {
    border: 1px solid @border.subtle@;
    background: @background.window@;
}

QTabWidget > QTabBar::tab {
    background: @background.window@;
    color: @text.secondary@;
    padding: 5px 8px;
    margin-right: 2px;
    border-top-left-radius: 3px;
    border-top-right-radius: 3px;
    border: 1px solid @border.subtle@;
    border-bottom: none;
    font-size: 11px;
    min-width: 75px;
    max-width: 95px;
}

QTabWidget > QTabBar::tab:hover:!selected {
    background: @control.hover@;
    color: @text.primary@;
}

QTabWidget > QTabBar::tab:selected {
    background: @control.selected@;
    color: @text.primary@;
    border-color: @border.subtle@;
    border-bottom: none;
}

/* Разделители окон */
QSplitter::handle {
    background-color: @background.window@;
}
QSplitter::handle:hover {
    background-color: @control.hover@;
}

/* Нижняя панель */
#BottomPanel {
    background-color: @background.window@;
    border-top: 1px solid @border.subtle@;
}

#BottomPanelHeader {
    background-color: @background.window@;
    border-top: 1px solid @border.subtle@;
    border-bottom: 1px solid @border.subtle@;
}

#BottomPanelHeader:hover {
    background-color: @background.window@;
}

#BottomPanelTitle {
    color: @text.primary@;
    background: transparent;
    font-weight: bold;
    font-size: 12px;
}

/* =========================================================================
   ШАПКА И ВКЛАДКИ ОТКРЫТЫХ ФАЙЛОВ (СТИЛЬ OBSIDIAN)
   ========================================================================= */
#FileEditorHeaderBar {
    background-color: @background.window@;
    border-bottom: 1px solid @border.subtle@;
    min-height: 36px;
    max-height: 36px;
}

#FileTabBar {
    background-color: transparent;
    border: none;
    min-height: 36px;
    max-height: 36px;
}

#FileTabBar::tab {
    background-color: @background.window@;
    color: @text.disabled@;
    padding: 0px 4px 0px 12px;
    border-right: 1px solid @border.subtle@;
    border-top: 2px solid transparent;
    border-bottom: none;
    font-size: 12px;
    min-height: 32px;
    max-height: 32px;
    min-width: 145px;
    max-width: 145px;
}

#FileTabBar::tab:selected {
    background-color: @control.selected@;
    color: @text.primary@;
    border-top: 2px solid transparent;
    border-right: 1px solid @border.subtle@;
    border-bottom: none;
    font-weight: 500;
}

#FileTabBar::tab:hover:!selected {
    background-color: @control.hover@;
    color: @text.primary@;
}

/* Кнопка добавления новой вкладки '+' */
#NewTabButton {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 3px;
    color: @text.disabled@;
    font-size: 14px;
    font-weight: bold;
    min-width: 20px;
    max-width: 20px;
    min-height: 20px;
    max-height: 20px;
    padding: 0px;
}

#NewTabButton:hover {
    background-color: @control.hover@;
    border: 1px solid @border.subtle@;
    color: @text.primary@;
}

/* Кнопка закрытия вкладки (строгий квадрат 16x16) */
#TabCloseButton {
    background: transparent;
    border: none;
    width: 16px;
    height: 16px;
    min-width: 16px;
    max-width: 16px;
    min-height: 16px;
    max-height: 16px;
    margin: 0px 6px 0px 2px;
    padding: 0px;
}

/* Строка пути (хлебные крошки) под шапкой */
#FileDocToolsBar {
    background-color: @background.window@;
    border-bottom: 1px solid @border.subtle@;
}

#FileEditorToolBtn {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    color: @text.secondary@;
    min-height: 22px;
    padding: 0 6px;
}

#FileEditorToolBtn:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
    color: @text.primary@;
}

#FileEditorToolBtn:checked {
    background-color: @control.selected@;
    border-color: @accent.primary@;
    color: @text.primary@;
}

#FileDocToolbar {
    background-color: @background.panel@;
    border: none;
}

#FileDocBreadcrumb {
    color: @text.secondary@;
    font-size: 11px;
    background: transparent;
}

/* Стек рабочей зоны */
#FileEditorStack {
    background-color: @background.window@;
    border: none;
}

/* Текстовый редактор VSCode */
#VSCodeEditor {
    background-color: #121212;
    color: @text.primary@;
    font-size: 14px;
    border: none;
    padding: 0px;
}

/* Плавающая кнопка 'Копировать' на блоках кода */
#CodeBlockCopyBtn {
    background-color: @background.surface@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    color: @text.secondary@;
    font-size: 10px;
    padding: 0px;
}

#CodeBlockCopyBtn:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
    color: @text.primary@;
}

/* =========================================================================
   ВИДЖЕТ ПРОВОДНИКА И КОНТЕКСТНОЕ МЕНЮ
   ========================================================================= */
#FileManagerWidget {
    background-color: @background.window@;
}

#FileManagerSessionLabel {
    color: @text.secondary@;
    font-weight: bold;
    font-size: 11px;
    padding-left: 2px;
}

#FileManagerPathContainer {
    background-color: @background.window@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
}

#FileManagerPathInput {
    background-color: transparent;
    color: @text.primary@;
    font-size: 11px;
    border: none;
    padding: 2px 4px;
}

#FileManagerBrowseBtn {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 3px;
    color: @text.primary@;
    padding: 0px;
    font-size: 13px;
    min-width: 26px;
    max-width: 26px;
    min-height: 26px;
    max-height: 26px;
}

#FileManagerBrowseBtn:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
}

#FileManagerTreeView {
    background-color: @background.window@;
    color: @text.primary@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    font-size: 11px;
}

#FileManagerTreeView::item {
    padding: 3px 0;
}

#FileManagerTreeView::item:hover {
    background-color: @control.hover@;
    color: @text.primary@;
}

#FileManagerTreeView::item:selected {
    background-color: @control.selected@;
    color: @text.primary@;
}

/* Контекстное меню проводника ПКМ */
#FileManagerContextMenu {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 6px;
    padding: 4px;
}

#FileManagerContextMenu::item {
    padding: 6px 22px 6px 12px;
    color: @text.primary@;
    border-radius: 4px;
}

#FileManagerContextMenu::item:selected {
    background-color: @control.selected@;
    color: @text.primary@;
}

#FileManagerContextMenu::separator {
    height: 1px;
    background-color: @background.surface@;
    margin: 4px 6px;
}

/* Плейсхолдеры */
#FileManagerPlaceholderFrame, #NoFilesPlaceholderFrame {
    background-color: @background.window@;
    border: 1px dashed @border.subtle@;
    border-radius: 4px;
}

#FileManagerPlaceholderLabel, #NoFilesPlaceholderLabel {
    color: @text.disabled@;
    font-size: 12px;
    font-weight: bold;
    font-style: italic;
}

#PlaceholderNewTabButton {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    color: @text.primary@;
    font-size: 16px;
    font-weight: bold;
}

#PlaceholderNewTabButton:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
    color: @text.primary@;
}

/* Чат */
#ChatScrollArea, #ChatMessagesContainer {
    background-color: @background.window@;
}

#ChatInputContainer {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 6px;
}

#ChatInputEdit {
    background-color: transparent;
    color: @text.primary@;
    font-size: 13px;
    border: none;
}

#ChatSendBtn {
    background-color: @border.subtle@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    color: @text.primary@;
    font-weight: bold;
    font-size: 13px;
}

#ChatSendBtn:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
}

#UserBubble {
    background-color: @border.subtle@;
    border: 1px solid @border.subtle@;
    border-radius: 8px;
    margin-left: 60px;
}

#AssistantBubble {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 8px;
    margin-right: 60px;
}

#BubbleSender {
    color: @text.secondary@;
    font-weight: bold;
    font-size: 11px;
    margin-bottom: 2px;
}

#BubbleText {
    color: @text.primary@;
    font-size: 13px;
    line-height: 1.4;
}

#SystemMessageLabel {
    color: #eab308;
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 12px;
    margin: 6px 40px;
}

/* Выпадающий список языков */
QComboBox {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    padding: 3px 8px;
    color: @text.primary@;
    min-width: 55px;
}

QComboBox::drop-down {
    border: none;
}

QComboBox QAbstractItemView {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    selection-background-color: @control.selected@;
    selection-color: @text.primary@;
}

/* Меню выбора позиции дока */
#DockPositionPopup {
    background-color: @background.window@;
    border: 1px solid @border.subtle@;
    border-radius: 6px;
}

#DockPositionTitle {
    color: @text.primary@;
    font-size: 12px;
    font-weight: bold;
    padding: 6px 8px;
}

/* =========================================================================
   ЧАТ В СТИЛЕ GOOGLE AI STUDIO (ПРОЗРАЧНЫЙ ГРАФИТОВЫЙ СТИЛЬ)
   ========================================================================= */
#ChatStudioScrollArea, #ChatTurnsContainer {
    background-color: transparent;
    border: none;
}

/* Карточки сообщений (фон панели) */
#AssistantTurnCard {
    background-color: transparent;
    border: none;
    border-radius: 8px;
}

#ChatMessageTextLabel {
    color: @text.primary@;
    font-size: 14px;
    line-height: 1.22;
    background-color: transparent;
    border: none;
}

/* Карточка блока кода */
#ChatCodeBlockCard {
    background-color: @background.input@;
    border: 1px solid @border.subtle@;
    border-radius: 6px;
    margin: 4px 0px;
}

#ChatCodeBlockHeader {
    background-color: @background.surface@;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    border-bottom: 1px solid @border.subtle@;
}

#ChatCodeCopyBtn {
    background-color: @control.hover@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    color: @text.secondary@;
    font-size: 10px;
    padding: 2px 6px;
}

#ChatCodeCopyBtn:hover {
    background-color: @control.hover@;
    color: @text.primary@;
}

#ChatCodeBlockBody {
    background-color: transparent;
    color: @text.primary@;
    border: none;
    padding: 6px 10px;
}

/* Аккордеон размышлений */
#ThoughtAccordion {
    background-color: @background.window@;
    border: 1px solid @border.subtle@;
    border-left: 3px solid @accent.primary@;
    border-radius: 6px;
    margin: 4px 0px;
}

#ThoughtHeaderBtn {
    background: transparent;
    border: none;
    text-align: left;
    padding: 0px;
    color: @text.primary@;
    font-size: 12px;
    font-weight: bold;
}

#ThoughtHeaderBtn:hover {
    background: transparent;
}

#ThoughtMetaBadge {
    background-color: @control.hover@;
    color: @text.secondary@;
    font-size: 10px;
    font-weight: 500;
    padding: 2px 6px;
    border-radius: 4px;
}

#ThoughtTextLabel {
    color: @text.secondary@;
    font-family: 'Fira Code', monospace;
    font-size: 11px;
    line-height: 1.4;
}

/* Карточка вызова инструмента */
#ToolExecutionCard {
    background-color: transparent;
    border: none;
}

/* Инпут-панель */
#ChatStudioInputCard {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 10px;
}

#ChatStudioInputCard:focus-within {
    border-color: @border.subtle@;
}

#ChatInputTextEdit {
    background-color: transparent;
    color: @text.primary@;
    font-size: 14px;
    border: none;
    padding: 2px;
}

/* Тумблер мыслей */
#ChatThoughtsToggle {
    color: @text.secondary@;
    font-size: 11px;
    spacing: 6px;
    background-color: transparent;
}

#ChatThoughtsToggle::indicator {
    width: 14px;
    height: 14px;
    border-radius: 3px;
    border: 1px solid @border.subtle@;
    background-color: @background.window@;
}

#ChatThoughtsToggle::indicator:checked {
    background-color: @control.selected@;
    border-color: @accent.primary@;
}

/* Кнопка отправки / остановки */
#ChatStudioSendBtn {
    background-color: @background.surface@;
    border: 1px solid @border.subtle@;
    border-radius: 6px;
    color: @text.primary@;
    font-weight: bold;
    font-size: 12px;
    padding: 6px 14px;
}

#ChatStudioSendBtn:hover {
    background-color: @control.hover@;
}

#ChatStudioStopBtn {
    background-color: #7f1d1d;
    border: 1px solid #ef4444;
    border-radius: 6px;
    color: @text.primary@;
    font-weight: bold;
    font-size: 12px;
    padding: 6px 14px;
}

#ChatStudioStopBtn:hover {
    background-color: #991b1b;
}

#SystemMessageStudioLabel {
    color: #fbbf24;
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 12px;
    margin: 4px 20px;
}

#ThoughtMessageCard {
    background-color: transparent;
    border: none;
    border-radius: 8px;
}

#UserTurnCard QLabel,
#AssistantTurnCard QLabel,
#ThoughtMessageCard QLabel,
#ChatMessageTextLabel {
    background: transparent;
    background-color: transparent;
    border: none;
}

/* Карточка пользователя: заметно светлее */
#UserTurnCard {
    background-color: transparent;
    border: none;
    border-radius: 8px;
}

#UserMessageBubble {
    background-color: @background.surface@;
    border: 1px solid @border.subtle@;
    border-radius: 8px;
}

#ThoughtBodyContainer,
#ThoughtBodyContainer > QLabel {
    background-color: transparent;
    background: transparent;
    border: none;
}

/* Строка действий под сообщением */
#MessageActions {
    background: transparent;
    border: none;
}

#MessageActionBtn {
    border: none;
    background: transparent;
    color: @text.disabled@;
    font-size: 12px;
    font-weight: bold;
    padding: 0px;
    border-radius: 3px;
    min-width: 24px;
    max-width: 24px;
    min-height: 24px;
    max-height: 24px;
}

#MessageActionBtn:hover {
    background-color: @control.hover@;
    color: @text.primary@;
}

#MessageActionBtn:checked {
    background-color: @control.selected@;
}

/* Кнопки Add и Tags на панели ввода */
#ChatInputAuxBtn {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 5px;
    color: @text.secondary@;
    font-size: 11px;
    font-weight: 500;
    padding: 4px 10px;
}

#ChatInputAuxBtn:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
    color: @text.primary@;
}

/* Контекстное меню ⋮ */
#ChatMessageContextMenu {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 6px;
    padding: 4px;
    font-size: 12px;
}

#ChatMessageContextMenu::item {
    padding: 6px 16px 6px 10px;
    color: @text.primary@;
    border-radius: 4px;
}

#ChatMessageContextMenu::item:selected {
    background-color: @control.selected@;
    color: @text.primary@;
}

/* Инлайн-редактор сообщения */
#ChatInlineEditor {
    background-color: @background.window@;
    color: @text.primary@;
    border: 1px solid @accent.primary@;
    border-radius: 5px;
    font-size: 14px;
    padding: 6px;
    min-height: 70px;
}

#ChatInlineSaveBtn {
    background-color: @accent.primary@;
    color: @text.primary@;
    border: none;
    border-radius: 4px;
    font-weight: bold;
    font-size: 11px;
    padding: 4px 12px;
}

#ChatInlineSaveBtn:hover {
    background-color: @control.hover@;
}

#ChatInlineCancelBtn {
    background-color: @background.surface@;
    color: @text.secondary@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    font-size: 11px;
    padding: 4px 12px;
}

#ChatInlineCancelBtn:hover {
    background-color: @control.hover@;
    color: @text.primary@;
}

/* Кнопки переключения режима памяти */
#MemoryModeBtn {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 8px;
    color: @text.secondary@;
    font-size: 12px;
    font-weight: 500;
    padding: 12px 20px;
    min-width: 140px;
    min-height: 48px;
}

#MemoryModeBtn:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
    color: @text.primary@;
}

#MemoryModeBtn:checked {
    background-color: @control.selected@;
    border: 2px solid @accent.primary@;
    color: @text.primary@;
    font-weight: bold;
}

/* Бейдж знака вопроса (?) */
#MemoryHelpBadge {
    background-color: @background.surface@;
    color: @accent.primary@;
    border-radius: 9px;
    font-size: 11px;
    font-weight: bold;
    min-width: 18px;
    max-width: 18px;
    min-height: 18px;
    max-height: 18px;
    qproperty-alignment: AlignCenter;
}

#MemoryHelpBadge:hover {
    background-color: @control.hover@;
    color: @text.primary@;
}

/* Дерево сессий */
#SessionsTreeWidget {
    background-color: @background.window@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    color: @text.primary@;
    font-size: 11px;
}

#SessionsTreeWidget::item {
    padding: 4px 0;
}

#SessionsTreeWidget::item:hover {
    background-color: @control.hover@;
}

#SessionsTreeWidget::item:selected {
    background-color: @control.selected@;
    color: @text.primary@;
}

/* Поле инлайн-редактирования сессии/ветки в строке */
#InlineSessionRename {
    background-color: @background.window@;
    color: @text.primary@;
    border: 1px solid @accent.primary@;
    border-radius: 3px;
    padding: 0px 4px;
    font-size: 11px;
}

/* Прозрачный контейнер инлайн-редактора */
#ChatInlineEditorBox {
    background: transparent;
    background-color: transparent;
    border: none;
}

/* Поле ввода текста */
#ChatInlineEditor {
    background-color: @background.window@;
    color: @text.primary@;
    border: 1px solid @accent.primary@;
    border-radius: 5px;
    font-size: 14px;
    padding: 6px;
    min-height: 70px;
}

/* Яркая непрозрачная кнопка Сохранить */
QPushButton#ChatInlineSaveBtn {
    background-color: @accent.primary@;
    color: @text.primary@;
    border: 1px solid @accent.primary@;
    border-radius: 4px;
    font-weight: bold;
    font-size: 11px;
    padding: 4px 14px;
}

QPushButton#ChatInlineSaveBtn:hover {
    background-color: @control.hover@;
    border-color: @accent.primary@;
}

/* Кнопка Отмена */
QPushButton#ChatInlineCancelBtn {
    background-color: @background.surface@;
    color: @text.secondary@;
    border: 1px solid @border.subtle@;
    border-radius: 4px;
    font-size: 11px;
    padding: 4px 14px;
}

QPushButton#ChatInlineCancelBtn:hover {
    background-color: @control.hover@;
    color: @text.primary@;
}

/* Persistent sidebar toggles in the top bar (Obsidian-style placement). */
#TopBarPanelToggleBtn {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    color: @text.secondary@;
    font-size: 12px;
    font-weight: bold;
    padding: 0px;
}

#TopBarPanelToggleBtn:hover {
    background-color: @control.hover@;
    border-color: @border.subtle@;
    color: @text.primary@;
}

/* Панель тегов над полем ввода */
#TagsBarContainer {
    background-color: @background.window@;
    border: 1px solid @border.subtle@;
    border-radius: 8px;
}

/* Кнопка добавления нового тега (+) */
#TagAddBtn {
    background-color: @background.panel@;
    border: 1px solid @border.subtle@;
    border-radius: 12px;
    color: @text.secondary@;
    font-size: 13px;
    font-weight: bold;
}

#TagAddBtn:hover {
    background-color: @control.hover@;
    color: @text.primary@;
    border-color: @accent.primary@;
}

/* Крестик удаления тега (✕) */
#TagChipDelBtn {
    background: transparent;
    border: none;
    color: @text.disabled@;
    font-size: 9px;
    font-weight: bold;
    padding: 0px;
}

#TagChipDelBtn:hover {
    color: #ef4444;
}

/* Инлайн-редактор названия тега */
#TagChipEditor {
    background-color: @background.window@;
    color: @text.primary@;
    border: 1px solid @accent.primary@;
    border-radius: 8px;
    font-size: 11px;
    padding: 1px 4px;
    max-width: 90px;
}

/* Плашка тега в карточке сообщения пользователя (полностью прозрачная рамка) */
#UserMessageTagBadge {
    background-color: transparent;
    border: none;
}

/* Нижняя панель: вкладки визуально повторяют вкладки верхней панели */
#BottomPanelTabs {
    background: transparent;
}

#BottomPanelTabs::tab {
    background: transparent;
    color: @text.disabled@;
    padding: 8px 18px;
    border: none;
    border-top: 2px solid transparent;
    font-weight: bold;
    font-size: 12px;
    min-width: 65px;
}

#BottomPanelTabs::tab:hover {
    color: @text.primary@;
    background: transparent;
}

#BottomPanelTabs::tab:selected {
    color: @text.primary@;
    background: transparent;
    border-top: 2px solid @text.primary@;
}

#BottomLiveLogView {
    background: @background.window@;
    color: @text.primary@;
    border: none;
    padding: 8px;
}

#BottomGraphPage {
    background: @background.window@;
}



QMainWindow { background: @background.window@; }
QPushButton, QToolButton { background: @background.surface@; color: @text.primary@;
 border: 1px solid transparent; border-radius: 6px; padding: 4px 8px; }
QPushButton:hover, QToolButton:hover { background: @control.hover@; border-color: @border.subtle@; }
QPushButton:pressed, QToolButton:pressed { background: @control.pressed@; }
QPushButton:checked, QToolButton:checked { background: @control.selected@; }
QPushButton:focus, QToolButton:focus { border: 1px solid @border.focus@; }
QPushButton:disabled, QToolButton:disabled { color: @text.disabled@; }
QPushButton[iconOnly="true"], QToolButton[iconOnly="true"] { background: transparent; border: 1px solid transparent; padding: 2px; }
QPushButton[iconOnly="true"]:hover, QToolButton[iconOnly="true"]:hover { background: @control.hover@; border-color: @border.subtle@; }
QPushButton[iconOnly="true"]:checked, QToolButton[iconOnly="true"]:checked { background: @control.selected@; }
QPushButton[iconOnly="true"]:focus, QToolButton[iconOnly="true"]:focus { border-color: @border.focus@; }
QPushButton#ChatInlineSaveBtn, QPushButton#ChatInlineCancelBtn,
QPushButton#ChatInlineSaveBtn:hover, QPushButton#ChatInlineCancelBtn:hover {
 background: @background.surface@; color: @text.primary@; border: 1px solid transparent; border-radius:6px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QAbstractSpinBox {
 background: @background.input@; color: @text.primary@; border:1px solid @border.subtle@; border-radius:6px; padding:4px 6px;
 selection-background-color:@control.selected@; selection-color:@text.primary@; }
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus { border-color:@border.focus@; }
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled { color:@text.disabled@; }
QComboBox QAbstractItemView { background:@background.panel@; color:@text.primary@; selection-background-color:@control.selected@; }
QCheckBox, QRadioButton { color:@text.primary@; spacing:6px; background:transparent; }
QCheckBox:disabled, QRadioButton:disabled { color:@text.disabled@; }
QCheckBox::indicator { width:14px; height:14px; border-radius:3px; border:1px solid @border.subtle@; background:@background.input@; }
QCheckBox::indicator:checked { background:@accent.subtle@; border-color:@accent.primary@; image:url(@checkmark@); }
QCheckBox::indicator:focus { border-color:@border.focus@; }
QMenu, #FileManagerContextMenu, #ChatMessageContextMenu {
 background:@background.panel@; color:@text.primary@; border:1px solid @border.subtle@; border-radius:8px; padding:4px; }
QMenu::item, #FileManagerContextMenu::item, #ChatMessageContextMenu::item { padding:5px 18px 5px 10px; border-radius:4px; }
QMenu::item:selected, #FileManagerContextMenu::item:selected, #ChatMessageContextMenu::item:selected { background:@control.hover@; }
QMenu::item:disabled { color:@text.disabled@; }
QMenu::separator { height:1px; background:@border.subtle@; margin:4px 6px; }
QToolTip { background:@background.surface@; color:@text.primary@; border:1px solid @border.subtle@; padding:5px; }
QTreeView, QListView, QTableView { background:@background.panel@; color:@text.primary@; border:none; outline:none; }
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected { background:@control.selected@; color:@text.primary@; }
QTreeView::item:hover, QListView::item:hover { background:@control.hover@; }
QHeaderView::section { background:@background.surface@; color:@text.secondary@; border:none; padding:5px; }
QGroupBox { background:@background.panel@; border:none; border-radius:8px; margin-top:12px; padding-top:12px; }
QGroupBox::title { color:@text.secondary@; subcontrol-origin:margin; left:10px; }
QTabWidget::pane { border:none; background:@background.panel@; border-radius:8px; top:6px; }
QTabBar { background:transparent; }
QTabBar::tab { background:transparent; color:@text.secondary@; border:none; border-radius:4px; margin:0px 3px 3px 0px; padding:5px 9px; }
QTabBar::tab:selected { background:@control.selected@; color:@text.primary@; }
QTabBar::tab:hover:!selected { background:@control.hover@; }
QTabWidget[toshyDock="true"]::pane { border:none; border-radius:8px; top:6px; background:@background.panel@; }
QTabBar[toshyDockTabs="true"] { background:transparent; }
QTabBar[toshyDockTabs="true"]::tab { margin-bottom:6px; background:transparent; border:none; }
QTabBar[toshyDockTabs="true"]::tab:selected { background:@background.surface@; }
#StartupSettingsPanel, #StartupSettingsContent, #StartupSettingsPageBody, #StartupSettingsRows { background:@background.panel@; }
#StartupSettingsNavigation { background:@background.window@; border:none; border-radius:8px; }
#StartupSettingsHeading { color:@text.primary@; font-size:14px; font-weight:600; }
#StartupSettingsSummary, #StartupSettingsDescription { color:@text.secondary@; }
#SettingRow { border:none; background:transparent; padding:4px; }
#ToshyDialogSurface { background:@background.panel@; border:1px solid @border.subtle@; border-radius:8px; }
#ToshyDialogTitleBar { background:transparent; }
#ToshyDialogTitle { color:@text.primary@; font-weight:600; background:transparent; }
#ToshyDialogContent { background:transparent; }
QTreeView QScrollBar::handle:vertical, QScrollArea#StartupSettingsPage QScrollBar::handle:vertical { background:@border.subtle@; }
QTreeView QScrollBar::handle:vertical:hover, QScrollArea#StartupSettingsPage QScrollBar::handle:vertical:hover { background:@text.secondary@; }

QLabel { background:transparent; }
QPlainTextEdit[uiField="true"], QTextEdit[uiField="true"] {
 background:@background.input@; color:@text.primary@; border:1px solid @border.subtle@; border-radius:6px; padding:6px;
 selection-background-color:@control.selected@; selection-color:@text.primary@;
}
QPlainTextEdit[uiField="true"]:focus, QTextEdit[uiField="true"]:focus {border-color:@border.focus@;}
QDialog {background:transparent;}


/* Workspace tabs attach to their respective window edges. */
#TopBar QTabBar#TopWorkspaceTabs::tab {
 background:transparent;color:@text.secondary@;margin:0;padding:0px 18px;
 border:0;border-radius:0;border-bottom-left-radius:7px;border-bottom-right-radius:7px;
 min-height:40px;max-height:40px;min-width:65px;
}
#TopBar QTabBar#TopWorkspaceTabs::tab:selected {background:@control.selected@;color:@text.primary@;border:0;}
#TopBar QTabBar#TopWorkspaceTabs::tab:hover:!selected {background:@control.hover@;color:@text.primary@;}
QTabBar#BottomPanelTabs::tab {
 background:transparent;color:@text.secondary@;margin:0;padding:0px 18px;
 border:0;border-radius:0;border-top-left-radius:4px;border-top-right-radius:4px;
 min-height:36px;max-height:36px;min-width:65px;
}
QTabBar#BottomPanelTabs::tab:selected {background:@control.selected@;color:@text.primary@;border:0;}
QTabBar#BottomPanelTabs::tab:hover:!selected {background:@control.hover@;color:@text.primary@;}
/* Dock columns and the pane/tab join remain distinct from the workspace. */
QWidget#DockColumnContainer {background:@background.window@;border:1px solid @border.subtle@;border-radius:8px;}
QTabWidget[toshyDock="true"]::pane {
 background:@background.panel@;border:1px solid @border.subtle@;border-radius:6px;
 border-top-left-radius:0px;top:0px;
}
QTabBar[toshyDockTabs="true"] {background:transparent;border:0;}
QTabBar[toshyDockTabs="true"]::tab {
 background:@background.surface@;color:@text.secondary@;margin:0px 2px 0px 0px;
 padding:5px 9px;border:1px solid @border.subtle@;border-bottom:0;
 border-radius:0;border-top-left-radius:4px;border-top-right-radius:4px;
}
QTabBar[toshyDockTabs="true"]::tab:selected {background:@background.panel@;color:@text.primary@;}
QTabBar[toshyDockTabs="true"]::tab:hover:!selected {background:@control.hover@;color:@text.primary@;}
QWidget[toshyDockPanel="true"] {background:@background.panel@;}
#TopBar QToolButton[iconOnly="true"], QWidget[toshyDockPanel="true"] QToolButton[iconOnly="true"],
QWidget[toshyDockPanel="true"] QPushButton[iconOnly="true"] {
 background:@background.surface@;border:1px solid @border.subtle@;border-radius:6px;padding:0px;
 min-width:0;min-height:0;
}
#TopBar QToolButton[iconOnly="true"]:hover, QWidget[toshyDockPanel="true"] QToolButton[iconOnly="true"]:hover,
QWidget[toshyDockPanel="true"] QPushButton[iconOnly="true"]:hover {background:@control.hover@;border-color:@accent.primary@;}
#TopBar QToolButton[iconOnly="true"]:pressed, QWidget[toshyDockPanel="true"] QToolButton[iconOnly="true"]:pressed,
QWidget[toshyDockPanel="true"] QPushButton[iconOnly="true"]:pressed {background:@control.pressed@;}
#TopBar QToolButton[iconOnly="true"] {min-width:26px;max-width:26px;min-height:26px;max-height:26px;}
QPushButton#ChatInlineSaveBtn:hover, QPushButton#ChatInlineCancelBtn:hover {background:@control.hover@;}
/* Regular file tabs are contiguous; FileTabBar paints document status fills. */
#FileEditorHeaderBar {min-height:36px;max-height:36px;}
QTabBar#FileTabBar {min-height:32px;max-height:32px;background:transparent;border:0;}
QTabBar#FileTabBar::tab {
 background:@background.surface@;color:@text.primary@;margin:0;padding:0px 4px 0px 12px;
 border:0;border-radius:0;min-height:32px;max-height:32px;
}
QTabBar#FileTabBar::tab:selected {background:@background.surface@;border:0;border-radius:0;}
/* Recessed tree/settings space uses the same surface as inactive file tabs. */
#FileManagerTreeView, #SessionsTreeWidget, #StartupSettingsTree,
#StartupSettingsNavigation, #StartupSettingsContent, #StartupSettingsPageBody,
#StartupSettingsRows, #StartupSettingsStack, #StartupSettingsNoResults {
 background:@background.surface@;color:@text.primary@;
}
#StartupSettingsNavigation {border:1px solid @border.subtle@;border-radius:6px;}
#FileManagerTreeView, #SessionsTreeWidget {border:1px solid @border.subtle@;border-radius:6px;}
/* Chat toolbar buttons have no idle fill; active popups are visible. */
QPushButton#ChatInputAuxBtn, QToolButton#ChatInputAuxBtn {
 background:transparent;border:1px solid transparent;border-radius:6px;padding:0;
 min-width:0;min-height:0;
}
QPushButton#ChatInputAuxBtn:hover, QToolButton#ChatInputAuxBtn:hover {background:@control.hover@;border-color:transparent;}
QPushButton#ChatInputAuxBtn[popupActive="true"], QToolButton#ChatInputAuxBtn[popupActive="true"],
QPushButton#ChatInputAuxBtn[active="true"] {background:@control.selected@;border-color:transparent;}
QPushButton#ThoughtHeaderBtn, QPushButton#ThoughtHeaderBtn:hover, QPushButton#ThoughtHeaderBtn:pressed,
QPushButton#ThoughtHeaderBtn:focus, QToolButton#ThoughtToggleBtn, QToolButton#ThoughtToggleBtn:hover,
QToolButton#ThoughtToggleBtn:pressed {background:transparent;border:0;padding:0;min-width:0;min-height:0;}
#ThoughtMetaBadge {background:transparent;}
QToolButton#TagChipDelBtn {min-width:0;min-height:0;max-width:18px;max-height:18px;padding:0;border:0;background:transparent;}
QToolButton#MessageActionBtn {
 min-width:0;min-height:0;max-width:24px;max-height:24px;padding:0;border:0;background:transparent;border-radius:4px;
}
QToolButton#MessageActionBtn:hover {background:@control.hover@;}
QToolButton#MessageActionBtn:checked {background:@control.selected@;}
QToolTip {background:@tooltip.background@;color:@text.primary@;border:1px solid @border.subtle@;border-radius:6px;padding:6px;}
/* Keep handle corners inside the painted box; margins would clip the caps. */
QScrollBar:vertical {width:8px;}
QScrollBar:horizontal {height:8px;}
QScrollBar::handle:vertical {border-radius:4px;margin:0;}
QScrollBar::handle:horizontal {border-radius:4px;margin:0;}
QSlider::groove:horizontal {height:4px;background:@border.subtle@;border-radius:2px;}
QSlider::handle:horizontal {width:14px;margin:-5px 0;background:@text.secondary@;border:1px solid @border.subtle@;border-radius:7px;}
QSlider::handle:horizontal:hover {background:@accent.primary@;}

#ChatStudioScrollArea QScrollBar:vertical, #TerminalFeed QScrollBar:vertical {
    border: none;
    background: transparent;
    width: 8px;
    margin: 0px;
}

#ChatStudioScrollArea QScrollBar::handle:vertical, #TerminalFeed QScrollBar::handle:vertical {
    background: #2e2e2e;
    min-height: 28px;
    border-radius: 4px;
    margin: 2px 1px;
}

#ChatStudioScrollArea QScrollBar::handle:vertical:hover, #TerminalFeed QScrollBar::handle:vertical:hover {
    background: @accent.primary@;
}

#ChatStudioScrollArea QScrollBar::handle:vertical:pressed, #TerminalFeed QScrollBar::handle:vertical:pressed {
    background: @accent.primary@;
}

#ChatStudioScrollArea QScrollBar::add-line:vertical, #ChatStudioScrollArea QScrollBar::sub-line:vertical, #TerminalFeed QScrollBar::add-line:vertical, #TerminalFeed QScrollBar::sub-line:vertical {
    height: 0px;
    width: 0px;
    border: none;
    background: none;
}

#ChatStudioScrollArea QScrollBar::add-page:vertical, #ChatStudioScrollArea QScrollBar::sub-page:vertical, #TerminalFeed QScrollBar::add-page:vertical, #TerminalFeed QScrollBar::sub-page:vertical {
    background: none;
}

#ChatStudioScrollArea QScrollBar:horizontal, #TerminalFeed QScrollBar:horizontal {
    border: none;
    background: transparent;
    height: 8px;
    margin: 0px;
}

#ChatStudioScrollArea QScrollBar::handle:horizontal, #TerminalFeed QScrollBar::handle:horizontal {
    background: #2e2e2e;
    min-width: 28px;
    border-radius: 4px;
    margin: 1px 2px;
}

#ChatStudioScrollArea QScrollBar::handle:horizontal:hover, #TerminalFeed QScrollBar::handle:horizontal:hover {
    background: @accent.primary@;
}

#ChatStudioScrollArea QScrollBar::add-line:horizontal, #ChatStudioScrollArea QScrollBar::sub-line:horizontal, #TerminalFeed QScrollBar::add-line:horizontal, #TerminalFeed QScrollBar::sub-line:horizontal {
    width: 0px;
    height: 0px;
    border: none;
    background: none;
}

#ChatStudioScrollArea QScrollBar::add-page:horizontal, #ChatStudioScrollArea QScrollBar::sub-page:horizontal, #TerminalFeed QScrollBar::add-page:horizontal, #TerminalFeed QScrollBar::sub-page:horizontal {
    background: none;
}

QToolButton#FileManagerBrowseBtn, QToolButton#SessionsNewBtn {
 min-width:24px;max-width:24px;min-height:24px;max-height:24px;padding:0;
 border:1px solid @border.subtle@;border-radius:6px;background:@background.surface@;
}
QToolButton#FileManagerBrowseBtn:hover, QToolButton#SessionsNewBtn:hover {
 background:@control.hover@;border-color:@accent.primary@;
}
QSlider::groove:vertical {width:4px;background:@border.subtle@;border-radius:2px;}
QSlider::handle:vertical {height:14px;margin:0 -5px;background:@text.secondary@;border:1px solid @border.subtle@;border-radius:7px;}
QSlider::handle:vertical:hover {background:@accent.primary@;}

#StartupSettingRow, #StartupSettingText {background:transparent;border:0;}

/* The workspace is distinct from the common top/editor tab-strip surface. */
QMainWindow, #ApplicationSurface, #WorkspaceSurface {background:@workspace.background@;}
#TopBar, #FileEditorHeaderBar {background:@background.window@;}
#BottomPanelContent, #BottomGraphPage, #BottomModulesPage {background:@workspace.background@;}
#BottomPanelHeader, #BottomPanelHeader:hover {background:@background.panel@;}
QTabBar#BottomPanelTabs::tab {min-height:36px;max-height:36px;}
QToolButton#MessageActionBtn {border-radius:5px;}
QPushButton#ChatInputAuxBtn, QToolButton#ChatInputAuxBtn,
QPushButton#ChatStudioSendBtn, QPushButton#ChatStudioStopBtn,
QToolButton#ChatStudioSendBtn, QToolButton#ChatStudioStopBtn {border-radius:6px;}
QToolButton#FileEditorToolBtn[iconOnly="true"] {padding:0;}
#StartupSettingsPanel, #StartupSettingsNavigation {border:1px solid @border.subtle@;}
#FileEditorAgentButton {padding:0;}

#VSCodeEditor, #ReadOnlyMarkdownEditor {background:@background.window~+0.025@;color:@text.primary@;}
#CenteredEditorHost {background:@workspace.background@;}
#ChatTurnsContainer {background:transparent;}
QTabBar[toshyDockTabs="true"]::tab:focus {border-color:@border.subtle@;}

/* One shared border per edge; workspace fills span the full inner header. */
#BottomPanel {border:0;}
#BottomPanelHeader, #BottomPanelHeader:hover {
 border-top:1px solid @border.subtle@;border-bottom:1px solid @border.subtle@;
 border-left:0;border-right:0;
}
QTabBar#BottomPanelTabs::tab {min-height:39px;max-height:39px;border-radius:0;border-top-left-radius:7px;border-top-right-radius:7px;}
QToolButton#BottomPanelToggleBtn {
 padding:0;min-width:26px;max-width:26px;min-height:26px;max-height:26px;
 border:1px solid @border.subtle@;border-radius:6px;background:@background.surface@;
}
QToolButton#BottomPanelToggleBtn:hover {background:@control.hover@;border-color:@accent.primary@;}
QTabBar[toshyDockTabs="true"]::tab:selected {background:@control.selected@;color:@text.primary@;}
QToolButton#TopBarAppBtn[popupActive="true"] {background:@control.selected@;}
QPushButton#ChatContinueBtn {
 background:@background.surface@;color:@text.primary@;border:1px solid @border.subtle@;
 border-radius:6px;padding:5px 12px;min-height:24px;
}
QPushButton#ChatContinueBtn:hover {background:@control.hover@;border-color:@accent.primary@;}
QPushButton#ChatContinueBtn:disabled {color:@text.disabled@;}
QToolButton#ErrorCopyBtn {
 background:transparent;border:1px solid transparent;border-radius:6px;padding:0;
 min-width:26px;max-width:26px;min-height:26px;max-height:26px;
}
QToolButton#ErrorCopyBtn:hover {background:@control.hover@;}
QPushButton#TaskIconBtn {
 background:@background.surface@;border:1px solid @border.subtle@;border-radius:6px;padding:0;
 min-width:26px;max-width:26px;min-height:26px;max-height:26px;
}
QPushButton#TaskIconBtn:hover {background:@control.hover@;border-color:@accent.primary@;}
QSplitter#MainDockSplitter::handle {background:transparent;border:0;}
QSplitter#MainDockSplitter::handle:hover {background:@control.hover@;}
#ChatWelcomePanel {background:@background.panel@;border:1px solid @border.subtle@;border-radius:10px;}

/* The top bar already draws the seam when the lower panel is fully raised. */
#BottomPanelHeader[sharesTopBorder="true"] {border-top:0;}
#BottomPanelHeader[sharesTopBorder="true"] QTabBar#BottomPanelTabs::tab {min-height:40px;max-height:40px;}


QToolButton#ChatWelcomeSettingsToggle {background:transparent;color:@text.secondary@;border:1px solid transparent;border-radius:6px;padding:5px 9px;}
QToolButton#ChatWelcomeSettingsToggle:hover {background:@control.hover@;color:@text.primary@;}
QToolButton#ChatWelcomeSettingsToggle:checked {background:@control.selected@;color:@text.primary@;}
QWidget#ChatWelcomeSettings {background:transparent;border:0;}
QToolButton#TerminalToolbarButton {min-width:26px;max-width:26px;min-height:26px;max-height:26px;padding:0;background:@background.surface@;border:1px solid @border.subtle@;border-radius:6px;}
QToolButton#TerminalToolbarButton:hover {background:@control.hover@;border-color:@accent.primary@;}
QToolButton#TerminalToolbarButton:checked {background:@control.selected@;}

QToolButton#PipelineManagerToggle {background:@background.surface@;border:1px solid @border.subtle@;border-radius:6px;padding:0;}
QToolButton#PipelineManagerToggle:hover {background:@control.hover@;border-color:@accent.primary@;}
QPushButton#ChatInputAuxBtn, QToolButton#ChatInputAuxBtn {border:1px solid @border.subtle@;padding:0;min-width:30px;max-width:30px;min-height:30px;max-height:30px;}
QPushButton#ChatInputAuxBtn:hover, QToolButton#ChatInputAuxBtn:hover {border-color:@accent.primary@;}
QScrollArea#ChatWelcomeArea, #ChatWelcomeArea > QWidget > QWidget, #ChatWelcomeBottomSpace {background:transparent;border:0;}
QSlider#PaintValueSlider {min-height:20px;max-height:20px;}
QSlider#PaintValueSlider::groove:horizontal {height:4px;border:0;}
QSlider#PaintValueSlider::handle:horizontal {width:12px;height:12px;margin:-5px 0;border:1px solid @border.subtle@;border-radius:7px;}

QAbstractSpinBox[toshyNumericButtons="true"] {padding-right:38px;}
QTabBar::scroller {width:18px;}
QToolButton#FileManagerImportBtn {padding:0;min-width:24px;max-width:24px;min-height:24px;max-height:24px;border:1px solid @border.subtle@;background:@background.surface@;border-radius:6px;}
QToolButton#FileManagerImportBtn:hover {background:@control.hover@;border-color:@accent.primary@;}

QToolButton#SubagentToolButton {
 min-width:26px;max-width:26px;min-height:26px;max-height:26px;
 padding:0;border:1px solid @border.subtle@;border-radius:6px;background:@background.surface@;
}
QToolButton#SubagentToolButton:hover {background:@control.hover@;border-color:@accent.primary@;}
QToolButton#SubagentToolButton:checked {background:@control.selected@;border-color:@accent.primary@;}
QToolButton#MessageActionBtn {min-width:24px;max-width:24px;min-height:24px;max-height:24px;padding:0;border:0;border-radius:5px;}
QToolButton#MessageActionBtn:hover {background:@control.hover@;border-radius:5px;}
QToolButton#TaskReviewAction {padding:6px 12px;border:1px solid @border.subtle@;border-radius:6px;}
QToolButton#TaskReviewAction:hover {background:@control.hover@;}
QTabBar::tear {image:none;width:0;border:0;background:transparent;}
QTabBar::scroller {width:20px;background:transparent;border:0;}
QToolButton#TerminalToolbarButton {min-width:26px;max-width:26px;min-height:26px;max-height:26px;padding:0;}


/* Composer scrolling uses the file-tree neutral thumb. */
QTextEdit#ChatInputTextEdit QScrollBar::handle:vertical {background:@border.subtle@;border-radius:4px;}
QTextEdit#ChatInputTextEdit QScrollBar::handle:vertical:hover {background:@text.secondary@;}
"""

def stylesheet(theme):
    result = resolve_values(_TEMPLATE, theme)
    tooltip_background = theme['colors']['background.window' if theme['foundation'] == 'dark' else 'control.pressed']
    result = result.replace('@tooltip.background@', tooltip_background)
    checkmark = Path(__file__).resolve().parents[1] / 'assets' / 'tabler' / ('check-light.svg' if theme['foundation'] == 'light' else 'check-dark.svg')
    result = result.replace('@checkmark@', checkmark.as_posix())
    return result

# Compatibility for external UI extensions that imported the former constant.
from ui.themes.resources import load, ROOT
DARK_THEME_QSS = stylesheet(load(ROOT / 'dark.json'))
