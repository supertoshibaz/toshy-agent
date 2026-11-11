"""Cancellable long polling on Qt's network loop; no worker threads held on exit."""
import json
from PySide6.QtCore import QObject, QByteArray, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkProxy
from core.security import auth_headers
from ui.core_bridge.process_manager import core_process


class FeedClient(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.network.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
        self.reply = None

    def cancel(self):
        reply, self.reply = self.reply, None
        if reply:
            reply.abort(); reply.deleteLater()

    def post(self, body, success, failure, *, path='feed', timeout=25000):
        self.cancel()
        request = QNetworkRequest(QUrl(core_process.base_url + '/v1/modules/terminal_sandbox/service/' + path))
        request.setHeader(QNetworkRequest.ContentTypeHeader, 'application/json')
        request.setTransferTimeout(timeout)
        for key,value in auth_headers().items():
            request.setRawHeader(key.encode(), value.encode())
        reply = self.network.post(request,QByteArray(json.dumps(body).encode()))
        self.reply = reply
        def finished():
            if reply is not self.reply:
                return
            self.reply = None
            try:
                data = bytes(reply.readAll()).decode('utf-8')
                value = json.loads(data)
                status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0
                if not 200 <= status < 300:
                    raise RuntimeError(value.get('detail') or reply.errorString())
                success(value)
            except Exception as error:
                failure(error)
            finally:
                reply.deleteLater()
        reply.finished.connect(finished)
