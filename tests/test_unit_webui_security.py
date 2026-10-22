"""网页服务的 Host/Origin、请求体与图片路径安全边界。"""

from __future__ import annotations

import importlib.util
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from conftest import PROJECT_ROOT


def _webui():
    spec = importlib.util.spec_from_file_location("webui_security_under_test", PROJECT_ROOT / "webui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _server(module, app=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}", thread


def _status(url: str, *, data: bytes | None = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_host_origin_and_post_guards():
    w = _webui()
    server, base, thread = _server(w)
    try:
        status, _, _ = _status(base + "/", headers={"Host": "evil.example"})
        assert status == 403

        status, _, _ = _status(
            base + "/api/user",
            data=b'{"user_id":"15547"}',
            headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        assert status == 403

        status, _, _ = _status(
            base + "/api/user",
            data=b'{"user_id":"15547"}',
            headers={"Content-Type": "application/json"},
        )
        assert status == 403

        status, _, _ = _status(
            base + "/api/user",
            data=b'{"user_id":"15547"}',
            headers={"Content-Type": "text/plain", "X-Requested-With": "integrated-ai"},
        )
        assert status == 415
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_security_headers_disable_inline_script():
    w = _webui()
    server, base, thread = _server(w)
    try:
        status, headers, _ = _status(base + "/")
        assert status == 200
        csp = headers.get("Content-Security-Policy", "")
        assert "script-src 'self';" in csp
        assert "script-src 'self' 'unsafe-inline'" not in csp
        assert headers.get("Permissions-Policy")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_negative_content_length_is_rejected_before_body_read():
    w = _webui()

    class DummyApp:
        @staticmethod
        def rate_limit(path, client_ip):
            return True, 0

    server, base, thread = _server(w, DummyApp())
    host = base.removeprefix("http://")
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            sock.sendall(
                (
                    "POST /api/chat HTTP/1.1\r\n"
                    f"Host: {host}\r\n"
                    "Content-Type: application/json\r\n"
                    "X-Requested-With: integrated-ai\r\n"
                    "Content-Length: -1\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
            )
            response = sock.recv(4096).decode("utf-8", "replace")
        assert " 400 " in response.splitlines()[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_windows_path_and_svg_payload_are_rejected(tmp_path):
    w = _webui()
    assert not w._valid_item_id(r"C:\Windows\win.ini")
    assert not w._valid_item_id(r"\\attacker\share\x")
    assert not w._valid_item_id("../../config/settings.py")
    assert w._sniff_image_mime(b"<svg><script>alert(1)</script></svg>") is None
    assert w._sniff_image_mime(b"not an image") is None
    first = w._image_cache_path(tmp_path, "85123A", ".jpg")
    second = w._image_cache_path(tmp_path, "../../config", ".jpg")
    assert first.parent == tmp_path.resolve()
    assert second.parent == tmp_path.resolve()
    assert first.name != second.name
