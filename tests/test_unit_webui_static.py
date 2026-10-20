"""网页静态资源必须从 web/ 提供，而不是继续塞在 webui.py 里。"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from webui import make_handler


def _get(port: int, path: str) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_static_page_assets_are_served():
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port

        status, ctype, body = _get(port, "/")
        assert status == 200
        assert "text/html" in ctype
        assert b'id="pgrid"' in body

        status, ctype, body = _get(port, "/styles.css")
        assert status == 200
        assert "text/css" in ctype
        assert b"--brand" in body

        status, ctype, body = _get(port, "/app.js")
        assert status == 200
        assert "javascript" in ctype
        assert b"function openCompare" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_static_route_rejects_path_traversal():
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            _get(server.server_port, "/../webui.py")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("path traversal must not be served")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
