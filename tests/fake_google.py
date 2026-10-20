"""In-process stand-in for YouTube's resumable upload endpoint.

Behaves like the real protocol where it matters for resume logic: a chunk is only
accepted at the exact committed offset (otherwise it answers 308 with the real
Range), `bytes */total` probes report progress, and a finished upload answers 200
with the video resource. Faults are injectable per data-PUT.
"""
from __future__ import annotations

from aiohttp import web


class FakeGoogle:
    def __init__(self, keep_data: bool = True):
        self.keep_data = keep_data
        self.data = bytearray()
        self.committed = 0
        self.inits = 0
        self.data_puts = 0
        self.probes = 0
        self.chunk_starts: list[int] = []
        self.max_body = 0
        self.dead = False                          # every session 404s
        self.killed: set[int] = set()              # individual sessions that 404
        self.complete = False
        self.fail_on_put: dict[int, str] = {}     # nth data PUT -> "503" | "drop"
        self.always_308_no_range = False
        self.after_commit = None                   # async hook(n) after chunk n is stored
        self.base = ""
        self._runner: web.AppRunner | None = None

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> str:
        app = web.Application(client_max_size=1024 ** 3)
        app.router.add_post("/upload", self._init)
        app.router.add_put("/session/{sid}", self._put)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return f"{self.base}/upload"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # -- handlers ------------------------------------------------------------
    async def _init(self, request: web.Request) -> web.Response:
        self.inits += 1
        self.data = bytearray()
        self.committed = 0
        self.complete = False
        return web.Response(headers={"Location": f"{self.base}/session/{self.inits}"})

    def _status(self) -> web.Response:
        if self.complete:
            return web.json_response({"id": "vid123"})
        if self.committed > 0:
            return web.Response(status=308, headers={"Range": f"bytes=0-{self.committed - 1}"})
        return web.Response(status=308)

    def _commit(self, body: bytes) -> None:
        self.committed += len(body)
        if self.keep_data:
            self.data += body

    async def _put(self, request: web.Request) -> web.Response:
        sid = int(request.match_info["sid"])
        if self.dead or sid in self.killed or sid != self.inits:
            return web.Response(status=404)
        content_range = request.headers["Content-Range"]
        body = await request.read()
        if content_range.startswith("bytes */"):
            self.probes += 1
            return self._status()

        span, total = content_range[len("bytes "):].split("/")
        start, _end = (int(x) for x in span.split("-"))
        self.data_puts += 1
        n = self.data_puts
        self.max_body = max(self.max_body, len(body))
        if self.always_308_no_range:
            return web.Response(status=308)
        if start != self.committed:
            return self._status()
        self.chunk_starts.append(start)

        fault = self.fail_on_put.get(n)
        if fault:
            self._commit(body[: len(body) // 2])   # half the chunk lands before the failure
            if fault == "drop":
                request.transport.close()
            return web.Response(status=503)

        self._commit(body)
        if self.after_commit:
            await self.after_commit(n)
        if self.committed == int(total):
            self.complete = True
            return web.json_response({"id": "vid123"})
        return self._status()
