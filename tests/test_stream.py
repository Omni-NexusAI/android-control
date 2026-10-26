import asyncio
import base64
import io
import socket
import struct
import threading
import time

import pytest

from usr.plugins.droidclaw.helpers import scrcpy_stream as ss

CONFIG = b"\x00\x00\x00\x01\x67\x64\x00\x1f\xac" + b"\x00\x00\x00\x01\x68\xee"
KEY = b"\x00\x00\x00\x01\x65" + b"K" * 50
DELTA = b"\x00\x00\x00\x01\x41" + b"D" * 20


def _frame(payload, config=False, key=False, pts=0):
    flags = pts
    if config:
        flags = 1 << 62
    elif key:
        flags |= 1 << 61
    return struct.pack(">QI", flags, len(payload)) + payload


class FakeScrcpyAdbServer:
    """Speaks the ADB smart-socket protocol and a scripted scrcpy server."""

    def __init__(self, width=576, height=1024):
        self.width, self.height = width, height
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.services = []
        self.control_bytes = bytearray()
        self.videos = {}
        self.video = None
        self.resets = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _request(self, conn):
        length = int(conn.recv(4), 16)
        data = b""
        while len(data) < length:
            data += conn.recv(length - len(data))
        return data.decode()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        transport = self._request(conn)
        conn.sendall(b"OKAY")
        service = self._request(conn)
        self.services.append((transport, service))
        conn.sendall(b"OKAY")
        with self.lock:
            first = service not in self.videos
            if first:
                self.videos[service] = conn
        if first:
            conn.sendall(b"\x00")  # dummy byte
            return
        self.video = self.videos[service]
        self.control_bytes = bytearray()
        # control socket: now send the video header and an initial burst
        name = b"FakePhone".ljust(64, b"\x00")
        self.video.sendall(name + struct.pack(">I", 0x68323634))
        self.video.sendall(struct.pack(">III", 0x80000000, self.width, self.height))
        self._burst()
        while True:
            data = conn.recv(4096)
            if not data:
                return
            self.control_bytes.extend(data)
            if 17 in data:  # RESET_VIDEO
                self.resets += 1
                self._burst()

    def _burst(self):
        self.video.sendall(_frame(CONFIG, config=True) + _frame(KEY, key=True, pts=1000) + _frame(DELTA, pts=2000) + _frame(DELTA, pts=3000))

    def close(self):
        self.sock.close()
        for conn in self.videos.values():
            conn.close()


@pytest.fixture
def stream_env(fake_adb, tmp_path, monkeypatch):
    from conftest import PLUGIN_CONFIG
    from usr.plugins.droidclaw.helpers import adb_backend

    server = FakeScrcpyAdbServer()
    PLUGIN_CONFIG.update({"adb_backend": "host", "adb_host": "127.0.0.1", "adb_port": server.port})
    adb_backend.invalidate_config_cache()
    adb_backend.invalidate_backend()
    fake_adb.add_device("PHONE")
    fake_adb.state["app_process_seconds"] = 6
    fake_adb.write()
    jar = tmp_path / "scrcpy-server"
    jar.write_bytes(b"jar")
    monkeypatch.setattr(ss, "ensure_server", lambda: jar)
    monkeypatch.setattr(ss, "_sessions", {})
    monkeypatch.setattr(ss, "_viewers", {})
    yield server
    for session in list(ss._sessions.values()):
        session.stop()
    server.close()


def _packets(gen, count, timeout=5.0):
    out = []
    deadline = time.monotonic() + timeout
    buf = b""
    for chunk in gen:
        buf += chunk
        while len(buf) >= 16:
            kind, flags, _, length, pts = struct.unpack(">BBHIQ", buf[:16])
            if len(buf) < 16 + length:
                break
            out.append((kind, flags, buf[16:16 + length], pts))
            buf = buf[16 + length:]
        if len([p for p in out if p[0] != ss.PKT_KEEPALIVE]) >= count or time.monotonic() > deadline:
            break
    return [p for p in out if p[0] != ss.PKT_KEEPALIVE]


def test_touch_message_layout():
    msg = ss.touch_message(0, 10, 20, 576, 1024)
    assert len(msg) == 32
    kind, action, pointer, x, y, w, h, pressure, ab, buttons = struct.unpack(">BBqiiHHHii", msg)
    assert (kind, action, pointer, x, y, w, h, pressure) == (2, 0, -2, 10, 20, 576, 1024, 0xFFFF)


def test_read_media_packet_parses_session_config_and_frames():
    a, b = socket.socketpair()
    a.sendall(struct.pack(">III", 0x80000000, 720, 1600) + _frame(CONFIG, config=True) + _frame(KEY, key=True, pts=42))
    assert ss.read_media_packet(b)[:2] == (ss.PKT_SESSION, struct.pack(">II", 720, 1600))
    assert ss.read_media_packet(b)[:2] == (ss.PKT_CONFIG, CONFIG)
    kind, payload, key, pts = ss.read_media_packet(b)
    assert (kind, payload, key, pts) == (ss.PKT_FRAME, KEY, True, 42)


def test_session_streams_to_viewer_and_injects_touch(stream_env):
    session = ss.get_session("PHONE", fps=60)
    assert session.fps == 60
    services = [svc for _, svc in stream_env.services]
    assert services[0] == services[1] == f"localabstract:scrcpy_{session.scid:08x}"
    assert stream_env.services[0][0] == "host:transport:PHONE"

    sub = session.subscribe()
    packets = _packets(ss.iter_stream(session, sub), 5)
    kinds = [p[0] for p in packets]
    assert kinds[0] == ss.PKT_HELLO
    assert ss.PKT_SESSION in kinds and ss.PKT_CONFIG in kinds
    first_frame = next(p for p in packets if p[0] == ss.PKT_FRAME)
    assert first_frame[1] & 1, "a new viewer must start on a key frame"
    assert session.video_size == (576, 1024)

    assert session.tap(0.5, 0.5)
    deadline = time.monotonic() + 2
    while len(stream_env.control_bytes) < 64 + stream_env.resets and time.monotonic() < deadline:
        time.sleep(0.05)
    raw = bytes(stream_env.control_bytes).replace(bytes([17]), b"")
    down = struct.unpack(">BBqiiHHHii", raw[:32])
    up = struct.unpack(">BBqiiHHHii", raw[32:64])
    assert down[1] == 0 and up[1] == 1
    assert (down[3], down[4]) == (287, 511)


def test_late_viewer_requests_keyframe(stream_env):
    session = ss.get_session("PHONE", fps=30)
    first = session.subscribe()
    _packets(ss.iter_stream(session, first), 4)
    resets_before = stream_env.resets
    second = session.subscribe()
    packets = _packets(ss.iter_stream(session, second), 7)
    assert stream_env.resets > resets_before
    assert any(p[0] == ss.PKT_FRAME and p[1] & 1 for p in packets)


def test_fps_change_restarts_session(stream_env):
    first = ss.get_session("PHONE", fps=24)
    second = ss.get_session("PHONE", fps=60)
    assert first is not second and not first.running and second.fps == 60
    assert ss.get_session("PHONE", fps=58) is second, "fps snaps to the nearest supported value"


def test_viewer_without_pings_is_dropped(stream_env, monkeypatch):
    monkeypatch.setattr(ss, "VIEWER_TIMEOUT", 0.5)
    session = ss.get_session("PHONE")
    sub = session.subscribe()
    start = time.monotonic()
    for _ in ss.iter_stream(session, sub):
        if time.monotonic() - start > 3:
            break
    assert time.monotonic() - start < 2.5
    assert session.viewer_count() == 0


def test_stream_api_returns_streaming_response_and_ping(stream_env):
    from usr.plugins.droidclaw.api.stream import Stream

    response = asyncio.run(Stream().process({"device": "PHONE", "fps": 40}, None))
    assert response.headers["Content-Encoding"] == "identity"
    packets = _packets(response.response, 1)
    viewer = packets[0][2].decode()
    assert asyncio.run(Stream().process({"action": "ping", "viewer": viewer}, None))["success"]
    assert asyncio.run(Stream().process({"action": "close", "viewer": viewer}, None))["success"]
    status = asyncio.run(Stream().process({"action": "status"}, None))
    assert status["sessions"][0]["fps_target"] == 40


def test_stream_api_reports_fallback_on_failure(fake_adb, monkeypatch):
    from usr.plugins.droidclaw.api.stream import Stream

    def boom(*a, **k):
        raise ss.StreamError("no encoder")

    monkeypatch.setattr(ss, "get_session", boom)
    result = asyncio.run(Stream().process({"device": "PHONE"}, None))
    assert result == {"success": False, "fallback": "poll", "message": "no encoder"}


def test_control_api_without_stream_uses_adb_and_rotation(fake_adb, monkeypatch, tmp_path):
    from usr.plugins.droidclaw.api import control
    from usr.plugins.droidclaw.helpers import workflow_engine

    monkeypatch.setattr(workflow_engine, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(workflow_engine, "_recordings", {})
    monkeypatch.setattr(control, "_sizes", {})
    fake_adb.add_device("PHONE")
    fake_adb.state["shell_outputs"] = {"wm size": "Physical size: 1080x2400\n"}
    fake_adb.write()
    workflow_engine._recordings["PHONE"] = workflow_engine.Recording(name="r", device="PHONE")

    api = control.Control()
    result = asyncio.run(api.process({"action": "tap", "device": "PHONE", "x": 0.5, "y": 0.25, "frame_w": 512, "frame_h": 1138}, None))
    assert result["success"] and result["via"] == "adb" and (result["x"], result["y"]) == (539, 599)
    # Landscape frame: device dimensions swap.
    result = asyncio.run(api.process({"action": "tap", "device": "PHONE", "x": 0.5, "y": 0.5, "frame_w": 1138, "frame_h": 512}, None))
    assert (result["x"], result["y"]) == (1199, 539)
    asyncio.run(api.process({"action": "back", "device": "PHONE"}, None))
    steps = workflow_engine._recordings["PHONE"].steps
    assert [s["action"] for s in steps] == ["tap", "tap", "key"]


def test_inline_screenshot_returns_downscaled_jpeg(fake_adb):
    from PIL import Image

    from usr.plugins.droidclaw.api.screenshot import Screenshot

    buf = io.BytesIO()
    Image.new("RGB", (1080, 2400), (10, 20, 30)).save(buf, format="PNG")
    fake_adb.add_device("PHONE")
    fake_adb.state["screencap_b64"] = base64.b64encode(buf.getvalue()).decode()
    fake_adb.write()
    result = asyncio.run(Screenshot().process({"device": "PHONE", "inline": True, "max_edge": 800}, None))
    assert result["success"] and result["data_url"].startswith("data:image/jpeg;base64,")
    img = Image.open(io.BytesIO(base64.b64decode(result["data_url"].split(",", 1)[1])))
    assert max(img.size) == 800 and (result["width"], result["height"]) == (1080, 2400)


def test_server_binary_checksum_is_pinned():
    assert ss.SCRCPY_SERVER_SHA256 == "764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536"
    assert ss.SCRCPY_VERSION in ss.SCRCPY_SERVER_URL
