"""Small CDP transport. The optional WebSocket dependency is loaded on use only."""

import asyncio
import json
from .browser_process import BrowserUnavailable


class CDP:
    def __init__(self, socket, on_event):
        self.socket, self.on_event = socket, on_event
        self.sequence, self.pending = 0, {}
        self.reader = asyncio.create_task(self._read())

    @staticmethod
    def connector():
        try:
            from websockets.asyncio.client import connect
            from websockets.version import version

            if int(version.split(".")[0]) < 15:
                raise ImportError()
        except ImportError:
            raise BrowserUnavailable(
                "Для браузерного чтения установите websockets>=15 в окружение Toshy."
            ) from None
        return connect

    @classmethod
    async def connect(cls, url, on_event, max_bytes):
        connect = cls.connector()
        socket = await connect(
            url,
            proxy=None,
            compression=None,
            max_size=max_bytes * 6 + 1024 * 1024,
            max_queue=8,
            open_timeout=5,
            close_timeout=1,
            ping_interval=None,
        )
        return cls(socket, on_event)

    async def _read(self):
        try:
            async for raw in self.socket:
                message = json.loads(raw)
                if "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(
                                BrowserUnavailable(
                                    "Браузер не выполнил команду "
                                    + str(message["error"].get("code", ""))
                                )
                            )
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    self.on_event(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        BrowserUnavailable("Соединение с браузером закрыто.")
                    )
            self.pending.clear()

    async def call(self, method, params=None, session=None):
        if self.reader.done():
            raise BrowserUnavailable("Соединение с браузером закрыто.")
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        message = {"id": identity, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            await self.socket.send(json.dumps(message, ensure_ascii=True))
            return await asyncio.wait_for(future, timeout=10)
        finally:
            self.pending.pop(identity, None)

    async def close(self):
        await self.socket.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)
