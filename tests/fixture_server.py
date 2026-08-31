"""Local HTTP fixture server on an OS-assigned port. No network leaves the box."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        fixtures = self.server.fixtures  # type: ignore[attr-defined]
        fixtures.requests.append((self.path, dict(self.headers)))
        route = fixtures.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        status, content_type, body = route
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


class FixtureServer:
    def __init__(self):
        self.routes: dict[str, tuple[int, str, bytes]] = {}
        self.requests: list[tuple[str, dict]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def set(self, path: str, body: bytes | str, status: int = 200, content_type: str = "application/json") -> None:
        if isinstance(body, str):
            body = body.encode()
        self.routes[path] = (status, content_type, body)

    def allow_all_robots(self) -> None:
        self.set("/robots.txt", "User-agent: *\nAllow: /\n", content_type="text/plain")

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "FixtureServer":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fixtures = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
