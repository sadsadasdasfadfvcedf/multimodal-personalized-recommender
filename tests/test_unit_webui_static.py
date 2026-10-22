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
        assert "识图推荐".encode() in body
        assert b'id="feedbackModal"' in body
        assert b'id="likedShelf"' in body
        assert b'id="autoRelated"' in body

        status, ctype, body = _get(port, "/styles.css")
        assert status == 200
        assert "text/css" in ctype
        assert b"--brand" in body

        status, ctype, body = _get(port, "/app.js")
        assert status == 200
        assert "javascript" in ctype
        assert b"function openCompare" in body
        assert "vision:{i:'📷'".encode() in body
        assert b"visionBusy" in body
        assert b"visionOnline" in body
        assert b"privacyNotice" in body
        assert b"setDeviceAllowed" in body
        assert b"openDislikeConfirm" in body
        assert b"apply_feedback:false" in body
        assert b"isVisualQuery" in body
        assert b"visionAsk" in body
        assert b"speechSynthesis" in body
        assert b"pauseLiveGesture" in body
        assert b"payload.focus_item_id = String(focusItemId)" in body
        assert b"shelfTopK" in body
        assert b"renderLikedShelf" in body
        assert b"loadAutoSimilar" in body
        assert "语音 + 识图完成".encode() in body
        assert "查看与这件相似的商品".encode() not in body
        assert b"data-liked-similar" not in body
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
