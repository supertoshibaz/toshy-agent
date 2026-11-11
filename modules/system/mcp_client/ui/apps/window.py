"""A removable standalone MCP Apps host with a sandboxed HTML guest."""
from ui.i18n import bind_caption, tr, localize, set_text

import html
import json
import re
from urllib.parse import urlsplit
from PySide6.QtCore import QObject, QUrl, Qt, Signal, Slot, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QVBoxLayout, QLabel)
from ui.widgets.dialogs import ToolDialog as QDialog
from ui.widgets.dialogs import MessageBox as QMessageBox
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
    QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView
from ui.core_bridge.async_runner import AsyncRunner

BASE = '/v1/modules/mcp_client/service/apps/'
_windows = {}

# Guest code never runs in the frame containing QWebChannel. The opaque-origin
# sandbox is essential: it prevents direct access to the host's bridge.
HOST = '''<!doctype html><html><head><meta charset="utf-8">
<style>html,body,iframe{margin:0;width:100%;height:100%;border:0;background:#18181b;color:#eee}</style>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script></head><body>
<iframe id="guest" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
<script>
const guest=document.getElementById('guest'); let bridge=null, payload=null, initialized=false;
window.toshyLoad = data => { payload=data; if(bridge)guest.srcdoc=data.html; };
window.toshySend = message => guest.contentWindow.postMessage(message,'*');
function initialData(){if(initialized||!payload)return; initialized=true;
 toshySend({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{arguments:payload.arguments}});
 toshySend({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:payload.result});}
new QWebChannel(qt.webChannelTransport, channel => {
 bridge=channel.objects.host; if(payload)guest.srcdoc=payload.html;
 bridge.reply.connect(raw=>{const response=JSON.parse(raw);toshySend(response);});
});
window.addEventListener('message', event=>{
 if(event.source!==guest.contentWindow || !bridge)return;
 const message=event.data;
 if(!message||message.jsonrpc!=='2.0'||typeof message!=='object')return;
 if(message.id==='toshy-teardown' && !message.method)return;
 if(!message.method)return;
 if(message.method==='ui/notifications/initialized'){initialData();return;}
 const raw=JSON.stringify(message); if(raw.length>1048576)return;
 bridge.send(raw);
});
</script></body></html>'''


def domains(values):
    result = []
    for value in values or []:
        if isinstance(value, str) and re.fullmatch(r'(?:https|wss)://(?:\*\.)?[A-Za-z0-9.-]+(?::[0-9]{1,5})?', value):
            result.append(value)
    return result


def guest_document(document, ui):
    csp = ui.get('csp') or {}
    resources = ' '.join(domains(csp.get('resourceDomains')))
    connect = ' '.join(domains(csp.get('connectDomains'))) or "'none'"
    frames = ' '.join(domains(csp.get('frameDomains'))) or "'none'"
    policy = ("default-src 'none'; script-src 'unsafe-inline' " + resources + "; "
        "style-src 'unsafe-inline' " + resources + "; img-src data: blob: " + resources + "; "
        "font-src data: " + resources + "; media-src data: blob: " + resources + "; "
        "connect-src " + connect + "; frame-src " + frames + "; base-uri 'none'; form-action 'none'; object-src 'none'")
    # The first policy applies before any server-supplied markup or script.
    return '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">' + document


class NetworkBoundary(QWebEngineUrlRequestInterceptor):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.origins = []

    def interceptRequest(self, info):
        url = info.requestUrl().toString()
        parsed = urlsplit(url)
        if parsed.scheme in {'data', 'blob', 'about'} or url == 'qrc:///qtwebchannel/qwebchannel.js':
            return
        if parsed.scheme not in {'https', 'wss'} or parsed.username or parsed.password:
            info.block(True); return
        origin = parsed.scheme + '://' + parsed.netloc
        for pattern in self.origins:
            expected = urlsplit(pattern.replace('*.', 'wildcard.'))
            if '*.' not in pattern and origin == pattern:
                return
            if '*.' in pattern:
                suffix = expected.hostname.removeprefix('wildcard.')
                if parsed.scheme == expected.scheme and parsed.hostname.endswith('.' + suffix) and parsed.port == expected.port:
                    return
        info.block(True)


class GuestPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, kind, is_main_frame):
        if is_main_frame:
            return url.scheme() in {'about', 'data', 'qrc'}
        return url.scheme() in {'about', 'https'}

    def createWindow(self, kind):
        return None


class Bridge(QObject):
    reply = Signal(str)

    def __init__(self, window):
        super().__init__(window)
        self.window = window

    @Slot(str)
    def send(self, raw):
        window = self.window
        if window.closing or len(raw) > 1048576:
            return
        try:
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                return
            method = message.get('method')
            identity = message.get('id')
            if method == 'ui/open-link':
                address = str((message.get('params') or {}).get('url') or '')
                parsed = urlsplit(address)
                if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Допускаются только HTTP(S)-ссылки')
                answer = QMessageBox.question(window, tr('ui.window.open_a_link_from_mcp_apps_a9d9'), address)
                if answer != QMessageBox.Yes:
                    raise ValueError('Переход отменён пользователем')
                QDesktopServices.openUrl(QUrl(address))
                self.reply.emit(json.dumps({'jsonrpc': '2.0', 'id': identity, 'result': {}}))
                return
            if method == 'ui/request-display-mode':
                self.reply.emit(json.dumps({'jsonrpc': '2.0', 'id': identity, 'result': {'mode': 'inline'}}))
                return
            def success(response):
                if response is not None and not window.closing:
                    if method == 'ui/initialize' and 'result' in response:
                        response['result']['hostContext'].update(
                            containerDimensions={'width': window.web.width(), 'height': window.web.height()},
                            platform='desktop', styles={'variables': {'--color-background-primary': '#18181b',
                                '--color-text-primary': '#f4f4f5', '--font-sans': 'sans-serif'}})
                    self.reply.emit(json.dumps(response, ensure_ascii=False))
            def failed(error):
                if identity is not None and not window.closing:
                    self.reply.emit(json.dumps({'jsonrpc': '2.0', 'id': identity,
                        'error': {'code': -32603, 'message': str(error)}}))
            window.runner.submit(lambda: window.client.request('POST', BASE + window.identity + '/message',
                message, window.scope), success, failed)
        except (ValueError, TypeError) as error:
            if 'message' in locals() and isinstance(message, dict) and message.get('id') is not None:
                self.reply.emit(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                    'error': {'code': -32602, 'message': str(error)}}))


class AppWindow(QDialog):
    def __init__(self, client, identity, scope, parent=None):
        super().__init__(parent)
        self.client, self.identity, self.scope = client, identity, dict(scope)
        self.closing = False
        self.setAttribute(Qt.WA_DeleteOnClose)
        set_text(self, 'ui.window.mcp_apps_6656', setter='setWindowTitle')
        self.resize(1000, 740)
        self.runner = AsyncRunner(self)
        self.layout = QVBoxLayout(self)
        self.status = localize(QLabel('', self), 'ui.window.loading_mcp_application_d051')
        self.status.setWordWrap(True)
        self.layout.addWidget(self.status)
        self.runner.submit(lambda: self.client.request('GET', BASE + identity, scope=self.scope), self.loaded,
            lambda error: bind_caption(self.status, str(error)))

    def loaded(self, document):
        if self.closing:
            return
        bind_caption(self, 'MCP Apps · ' + document['title'], setter='setWindowTitle')
        self.web = QWebEngineView(self)
        self.profile = QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
        self.boundary = NetworkBoundary(self.profile)
        csp = document['ui'].get('csp') or {}
        self.boundary.origins = domains(csp.get('resourceDomains')) + domains(csp.get('connectDomains')) + domains(csp.get('frameDomains'))
        self.profile.setUrlRequestInterceptor(self.boundary)
        self.profile.downloadRequested.connect(lambda download: download.cancel())
        self.page = GuestPage(self.profile, self.web)
        self.web.setPage(self.page)
        self.page.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, False)
        self.page.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        self.page.settings().setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, False)
        self.page.featurePermissionRequested.connect(lambda origin, feature:
            self.page.setFeaturePermission(origin, feature, QWebEnginePage.PermissionDeniedByUser))
        self.channel = QWebChannel(self.page)
        self.bridge = Bridge(self)
        self.channel.registerObject('host', self.bridge)
        self.page.setWebChannel(self.channel)
        payload = {key: document[key] for key in ('arguments', 'result')}
        payload['html'] = guest_document(document['html'], document['ui'])
        def ready(ok):
            if ok and not self.closing:
                self.page.runJavaScript('window.toshyLoad(' + json.dumps(payload, ensure_ascii=True) + ')')
                self.status.hide()
        self.page.loadFinished.connect(ready)
        self.layout.addWidget(self.web, 1)
        self.web.setHtml(HOST, QUrl('qrc:///toshy-mcp-host/'))

    def closeEvent(self, event):
        if not self.closing:
            self.closing = True
            if hasattr(self, 'page'):
                self.page.runJavaScript("window.toshySend({jsonrpc:'2.0',id:'toshy-teardown',method:'ui/resource-teardown',params:{reason:'Window closed'}})")
            client, identity, scope = self.client, self.identity, dict(self.scope)
            self.runner.submit(lambda: client.request('POST', BASE + identity + '/close', {}, scope))
            event.ignore()
            QTimer.singleShot(250, self.close)
            return
        if hasattr(self, 'page'):
            self.page.setWebChannel(None)
            self.page.deleteLater()
            self.profile.deleteLater()
        super().closeEvent(event)


def open_app(client, identity, scope, parent=None):
    key = identity
    if key in _windows:
        _windows[key].raise_(); _windows[key].activateWindow(); return _windows[key]
    window = AppWindow(client, identity, scope, parent)
    _windows[key] = window
    window.destroyed.connect(lambda: _windows.pop(key, None))
    window.show()
    return window
