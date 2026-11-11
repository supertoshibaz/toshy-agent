"""A folded chat part with lazy tabs for independent subagent answers."""
from ui.i18n import bind_caption, add_caption_tab, tr, set_text
from ui.styles.binding import apply_style


import copy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTabWidget, QSizePolicy
from ui.views.chat_view import ThoughtMessageTurn, MarkdownMessageWidget


class SubagentMessageTurn(ThoughtMessageTurn):
    is_subagent_block = True

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer.stop()
        set_text(self.header_btn, 'ui.subagents_panel.subagents_cf58')
        self.results = []
        self.tabs = QTabWidget(self.body_container)
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.body_layout.addWidget(self.tabs)
        self.tabs.currentChanged.connect(self._render_current)
        self.is_expanded = False
        self.body_container.hide()
        self.finish_thinking()
        from ui.views.chat_presentation import install_actions

        install_actions(self, "thought")
        set_text(self.edit_btn, 'ui.chat_subagents.expand_collapse_subagent_responses_cdb9', setter='setToolTip')

    def set_results(self, results):
        selected = self.tabs.currentIndex()
        self.results = copy.deepcopy(results)
        self.tabs.blockSignals(True)
        while self.tabs.count() > len(self.results):
            page = self.tabs.widget(self.tabs.count() - 1)
            self.tabs.removeTab(self.tabs.count() - 1)
            page.deleteLater()
        names = {
            "queued": tr('ui.caption.1c0ab751460b'),
            "running": tr('ui.caption.bd280ebe5bda'),
            "completed": tr('ui.caption.57778825e18b'),
            "error": tr('ui.caption.44d4090ae9e4'),
            "stopped": tr('ui.caption.df14a196ee9a'),
        }
        for index, result in enumerate(self.results):
            if index >= self.tabs.count():
                page = QWidget(self.tabs)
                layout = QVBoxLayout(page)
                layout.setContentsMargins(0, 4, 0, 0)
                page.task = QLabel(page)
                page.task.setWordWrap(True)
                page.task.setTextFormat(Qt.PlainText)
                page.task.setTextInteractionFlags(Qt.TextSelectableByMouse)
                apply_style(page.task, "color:@text.secondary@;")
                layout.addWidget(page.task)
                page.channels = QTabWidget(page)
                page.channels.setDocumentMode(True)
                page.channels.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
                page.answer = MarkdownMessageWidget(page.channels)
                page.thought = MarkdownMessageWidget(
                    page.channels, text_color="#a1a1aa"
                )
                add_caption_tab(page.channels, page.answer, tr('ui.caption.5cfcac6ad022'))
                add_caption_tab(page.channels, page.thought, tr('ui.caption.5bdd2c962b84'))
                page.channels.currentChanged.connect(self._render_current)
                layout.addWidget(page.channels)
                page.rendered = None
                page.rendered_channel = -1
                add_caption_tab(self.tabs, page, "")
            status = names.get(result.get("status"), result.get("status", ""))
            bind_caption(self.tabs, str(result.get("name") or result.get("agent_id") or "Субагент"), setter='setTabText', index=index)
            bind_caption(self.tabs, status, setter='setTabToolTip', index=index)
        if selected >= 0:
            self.tabs.setCurrentIndex(min(selected, self.tabs.count() - 1))
        self.tabs.blockSignals(False)
        self.finish_thinking()
        self._render_current()

    def update_result(self, result):
        results = copy.deepcopy(self.results)
        index = next(
            (i for i, r in enumerate(results) if r.get("id") == result.get("id")),
            len(results),
        )
        if index == len(results):
            results.append(dict(result))
        else:
            results[index] = dict(result)
        self.set_results(results)

    def _render_current(self, *_):
        index = self.tabs.currentIndex()
        for number in range(self.tabs.count()):
            self.tabs.widget(number).setSizePolicy(
                QSizePolicy.Expanding,
                QSizePolicy.Preferred if number == index else QSizePolicy.Ignored,
            )
        self.tabs.updateGeometry()
        if not self.is_expanded:
            return
        index = self.tabs.currentIndex()
        if not 0 <= index < len(self.results):
            return
        page, result = self.tabs.widget(index), self.results[index]
        channel = page.channels.currentIndex()
        for number in range(page.channels.count()):
            page.channels.widget(number).setSizePolicy(
                QSizePolicy.Expanding,
                QSizePolicy.Preferred if number == channel else QSizePolicy.Ignored,
            )
        page.channels.updateGeometry()
        if page.rendered == result and page.rendered_channel == channel:
            return
        status = {
            "queued": tr('ui.caption.0e709ee7fcdb'),
            "running": tr('ui.caption.599ca07b8dd2'),
            "completed": tr('ui.caption.44e2df8d3e17'),
            "error": tr('ui.file_manager_widget.error_bf22'),
            "stopped": tr('core_status_offline'),
        }.get(result.get("status"), "")
        bind_caption(page.task, str(result.get("task") or "")
            + "\n"
            + status
            + ("\n" + str(result["error"]) if result.get("error") else ""))
        view = page.thought if channel else page.answer
        text = str(result.get("thoughts" if channel else "output") or "")
        if result.get("status") == "running":
            view.queue_markdown(text)
        else:
            view.set_markdown(text)
        page.rendered = copy.deepcopy(result)
        page.rendered_channel = channel
        self.updateGeometry()

    def _materialize_thought_body(self):
        self._render_current()

    def export_segments(self):
        return [{"type": "subagents", "results": copy.deepcopy(self.results)}]

    def finish_thinking(self, elapsed_seconds=None):
        self._timer.stop()
        results = getattr(self, "results", [])
        active = sum(r.get("status") in {"queued", "running"} for r in results)
        failed = sum(r.get("status") in {"error", "stopped"} for r in results)
        text = str(len(results))
        if active:
            text += tr('ui.caption.67f42b1edeb7', v0=active)
        if failed:
            text += tr('ui.caption.e39fda04dd16', v0=failed)
        bind_caption(self.meta_badge, text)
        self.sync_activity()

    def resume_thinking(self):
        self.finish_thinking()

    def pause_tools(self):
        for result in self.results:
            if result.get("status") in {"queued", "running"}:
                result["status"] = "stopped"
        self.set_results(self.results)

    def _toggle_edit_mode(self):
        # A tool result is immutable; the answer/whole group remains editable.
        self.toggle_collapse(preserve_view=True)
