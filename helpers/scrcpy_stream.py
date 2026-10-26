"""Live H.264 screen streaming (and low-latency input) via scrcpy-server.

Screenshot polling tops out around 1-5 fps because every frame is a full PNG
encode on the phone. scrcpy-server (Apache-2.0, https://github.com/Genymobile/scrcpy)
runs the phone's hardware video encoder instead, which comfortably delivers
12-60 fps at a fraction of the bandwidth.

How it fits together:

* the pinned ``scrcpy-server`` build is downloaded once (SHA-256 verified)
  and pushed to the phone;
* the server is started over ``adb shell`` with ``tunnel_forward=true``;
* we connect to its abstract socket through the ADB server's smart-socket
  protocol (``host:transport:<serial>`` + ``localabstract:scrcpy_<scid>``),
  which works the same for the host bridge and the container ADB server,
  without ``adb forward`` port bookkeeping;
* one session per device is shared by every viewer (panel, pop-out, several
  browser tabs). Packets are re-framed for the browser, which decodes them
  with WebCodecs. Late joiners get the cached stream config and an encoder
  reset so they start on a fresh key frame;
* the control socket injects touch events directly (much lower latency than
  ``adb shell input``).

Wire format sent to the browser, per packet (big-endian):
    u8 type (1 = session, 2 = codec config, 3 = frame, 4 = keepalive, 5 = hello)
    u8 flags (bit0 = key frame)
    u16 reserved
    u32 payload length
    u64 pts (microseconds; 0 for non-frames)
    payload (session: u32 width, u32 height; hello: viewer id, ascii)

Viewers must call ``ping(viewer_id)`` every few seconds (the panel does it
via the stream API). A stream whose pings stop is ended server-side: the WSGI
bridge does not reliably tell a generator that the browser went away.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import random
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from usr.plugins.droidclaw.helpers.adb_backend import (
    AdbBackend,
    _adb_subprocess_env,
    _load_config,
    _prepare,
    adb_cmd,
    run_adb_result,
)

logger = logging.getLogger("droidclaw")

SCRCPY_VERSION = "5.0.1"
SCRCPY_SERVER_SHA256 = "764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536"
SCRCPY_SERVER_URL = (
    f"https://github.com/Genymobile/scrcpy/releases/download/v{SCRCPY_VERSION}/scrcpy-server-v{SCRCPY_VERSION}"
)
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
SERVER_DIR = _PLUGIN_DIR / "data" / "scrcpy"
DEVICE_SERVER_PATH = "/data/local/tmp/droidclaw-scrcpy-server.jar"

FPS_CHOICES = (12, 24, 30, 40, 60)
IDLE_SHUTDOWN = 15.0
SUBSCRIBER_QUEUE = 240

PKT_SESSION = 1
PKT_CONFIG = 2
PKT_FRAME = 3
PKT_KEEPALIVE = 4
PKT_HELLO = 5
VIEWER_TIMEOUT = 12.0

_HEADER = struct.Struct(">BBHIQ")
_CODEC_H264 = 0x68323634
_FLAG_CONFIG = 1 << 62
_FLAG_KEY = 1 << 61
_PTS_MASK = (1 << 61) - 1

# Control message types (scrcpy server ControlMessage.java)
_CTRL_INJECT_TOUCH = 2
_CTRL_RESET_VIDEO = 17
_ACTION_DOWN, _ACTION_UP, _ACTION_MOVE = 0, 1, 2
_POINTER_FINGER = -2


class StreamError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# scrcpy-server binary
# ---------------------------------------------------------------------------

_download_lock = threading.Lock()


def server_path() -> Path:
    return SERVER_DIR / f"scrcpy-server-v{SCRCPY_VERSION}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_server() -> Path:
    """Download (once) and verify the pinned scrcpy-server build."""
    path = server_path()
    with _download_lock:
        if path.is_file() and _sha256(path) == SCRCPY_SERVER_SHA256:
            return path
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".download")
        try:
            with urllib.request.urlopen(SCRCPY_SERVER_URL, timeout=60) as response, tmp.open("wb") as fh:
                fh.write(response.read())
        except Exception as exc:
            raise StreamError(f"Could not download scrcpy-server {SCRCPY_VERSION}: {exc}") from exc
        if _sha256(tmp) != SCRCPY_SERVER_SHA256:
            tmp.unlink(missing_ok=True)
            raise StreamError("Downloaded scrcpy-server failed its SHA-256 check")
        tmp.replace(path)
        return path


# ---------------------------------------------------------------------------
# ADB smart-socket connection to a device-side abstract socket
# ---------------------------------------------------------------------------

def server_address(backend: AdbBackend) -> tuple[str, int]:
    prefix = backend.command_prefix
    host, port = "127.0.0.1", int(os.environ.get("ANDROID_ADB_SERVER_PORT", "5037") or 5037)
    if "-H" in prefix:
        host = prefix[prefix.index("-H") + 1]
    if "-P" in prefix:
        port = int(prefix[prefix.index("-P") + 1])
    return host, port


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _smart_request(sock: socket.socket, service: str) -> None:
    payload = service.encode("utf-8")
    sock.sendall(f"{len(payload):04x}".encode("ascii") + payload)
    status = _recv_exact(sock, 4)
    if status == b"OKAY":
        return
    if status == b"FAIL":
        length = int(_recv_exact(sock, 4), 16)
        raise ConnectionError(_recv_exact(sock, length).decode("utf-8", "replace"))
    raise ConnectionError(f"unexpected adb reply {status!r}")


def open_device_socket(backend: AdbBackend, serial: str, service: str, timeout: float = 5.0) -> socket.socket:
    host, port = server_address(backend)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        _smart_request(sock, f"host:transport:{serial}")
        _smart_request(sock, service)
    except Exception:
        sock.close()
        raise
    sock.settimeout(None)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


# ---------------------------------------------------------------------------
# Protocol helpers (also used by tests)
# ---------------------------------------------------------------------------

def pack_packet(kind: int, payload: bytes = b"", key: bool = False, pts: int = 0) -> bytes:
    return _HEADER.pack(kind, 1 if key else 0, 0, len(payload), pts) + payload


def touch_message(action: int, x: int, y: int, width: int, height: int, pressure: float = 1.0) -> bytes:
    pressure_u16 = 0xFFFF if pressure >= 1.0 else int(max(0.0, pressure) * 0x10000)
    return struct.pack(
        ">BBqiiHHHii",
        _CTRL_INJECT_TOUCH,
        action,
        _POINTER_FINGER,
        int(x),
        int(y),
        int(width),
        int(height),
        pressure_u16,
        0,  # action button (finger touches carry none)
        0,  # buttons
    )


def reset_video_message() -> bytes:
    return bytes([_CTRL_RESET_VIDEO])


def read_media_packet(sock: socket.socket):
    """Read one session or media packet. Returns (kind, payload, key, pts)."""
    header = _recv_exact(sock, 12)
    if header[0] & 0x80:  # session packet
        _, width, height = struct.unpack(">III", header)
        return PKT_SESSION, struct.pack(">II", width, height), False, 0
    pts_flags, size = struct.unpack(">QI", header)
    payload = _recv_exact(sock, size)
    if pts_flags & _FLAG_CONFIG:
        return PKT_CONFIG, payload, False, 0
    return PKT_FRAME, payload, bool(pts_flags & _FLAG_KEY), pts_flags & _PTS_MASK


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class Subscriber:
    def __init__(self):
        self.id = f"{random.getrandbits(48):012x}"
        self.queue: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE)
        self.waiting_key = True
        self.last_ping = time.monotonic()
        self.closed = False

    def offer(self, packet: bytes, kind: int, key: bool) -> bool:
        """Queue a packet; returns False if the subscriber fell behind."""
        if kind == PKT_FRAME:
            if self.waiting_key and not key:
                return True
            if key:
                self.waiting_key = False
        try:
            self.queue.put_nowait(packet)
            return True
        except queue.Full:
            # Too slow (or gone): drop the backlog and resync on a key frame.
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self.waiting_key = True
            return False


class StreamSession:
    def __init__(self, serial: str, backend: AdbBackend, fps: int, max_size: int, bit_rate: int):
        self.serial = serial
        self.backend = backend
        self.fps = fps
        self.max_size = max_size
        self.bit_rate = bit_rate
        self.scid = random.randint(1, 0x7FFFFFFF)
        self.subscribers: list[Subscriber] = []
        self.lock = threading.Lock()
        self.ctrl_lock = threading.Lock()
        self.video_sock: socket.socket | None = None
        self.control_sock: socket.socket | None = None
        self.process: subprocess.Popen | None = None
        self.session_packet: bytes | None = None
        self.config_packet: bytes | None = None
        self.video_size = (0, 0)
        self.device_name = ""
        self.running = False
        self.starting = True
        self.error = ""
        self.started_at = 0.0
        self.idle_since = time.monotonic()
        self.frames = 0
        self._fps_window: list[float] = []
        self.server_log: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        local = ensure_server()
        self._push_server(local)
        socket_name = f"scrcpy_{self.scid:08x}"
        args = [
            "shell",
            f"CLASSPATH={DEVICE_SERVER_PATH}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            SCRCPY_VERSION,
            f"scid={self.scid:08x}",
            "log_level=info",
            "tunnel_forward=true",
            "audio=false",
            "control=true",
            "video_codec=h264",
            f"max_fps={self.fps}",
            f"max_size={self.max_size}",
            f"video_bit_rate={self.bit_rate}",
            "cleanup=true",
            "power_on=false",
            "clipboard_autosync=false",
        ]
        cmd = adb_cmd(args, device=self.serial, backend=self.backend)
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_adb_subprocess_env(self.backend),
        )
        threading.Thread(target=self._drain_log, name=f"scrcpy-log-{self.serial}", daemon=True).start()

        deadline = time.monotonic() + 8.0
        last_error = None
        while time.monotonic() < deadline:
            try:
                video = open_device_socket(self.backend, self.serial, f"localabstract:{socket_name}")
                video.settimeout(5.0)
                _recv_exact(video, 1)  # dummy byte: proves the server accepted
                self.video_sock = video
                break
            except (OSError, ConnectionError) as exc:
                last_error = exc
                if self.process.poll() is not None:
                    break
                time.sleep(0.2)
        if self.video_sock is None:
            self.stop()
            log = " | ".join(self.server_log[-5:])
            raise StreamError(f"scrcpy-server did not start: {last_error}" + (f" ({log})" if log else ""))

        try:
            self.control_sock = open_device_socket(self.backend, self.serial, f"localabstract:{socket_name}")
            name = _recv_exact(self.video_sock, 64).split(b"\0", 1)[0].decode("utf-8", "replace")
            (codec,) = struct.unpack(">I", _recv_exact(self.video_sock, 4))
        except (OSError, ConnectionError) as exc:
            self.stop()
            raise StreamError(f"scrcpy handshake failed: {exc}") from exc
        if codec != _CODEC_H264:
            self.stop()
            raise StreamError(f"device refused the H.264 stream (codec id {codec:#x})")
        self.video_sock.settimeout(None)
        self.device_name = name
        self.running = True
        self.started_at = time.monotonic()
        threading.Thread(target=self._read_loop, name=f"scrcpy-video-{self.serial}", daemon=True).start()
        threading.Thread(target=self._drain_control, name=f"scrcpy-ctrl-{self.serial}", daemon=True).start()

    def _push_server(self, local: Path) -> None:
        size = local.stat().st_size
        check = run_adb_result(["shell", "stat", "-c", "%s", DEVICE_SERVER_PATH], device=self.serial, timeout=8, backend=self.backend, resolve=False)
        if check["returncode"] == 0 and check["stdout"].strip() == str(size):
            return
        pushed = run_adb_result(["push", str(local), DEVICE_SERVER_PATH], device=self.serial, timeout=60, backend=self.backend, resolve=False)
        if pushed["returncode"] != 0:
            raise StreamError(f"could not push scrcpy-server: {pushed['output']}")

    def _drain_log(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.server_log.append(line)
                del self.server_log[:-40]

    def _drain_control(self) -> None:
        # Device -> client messages (clipboard etc.) are not used; drain them so
        # the server never blocks on a full socket buffer.
        sock = self.control_sock
        try:
            while self.running and sock is not None:
                if not sock.recv(4096):
                    break
        except OSError:
            pass

    def stop(self) -> None:
        self.running = False
        for sock in (self.video_sock, self.control_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.video_sock = self.control_sock = None
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        with self.lock:
            for sub in self.subscribers:
                sub.closed = True
                try:
                    sub.queue.put_nowait(b"")
                except queue.Full:
                    pass

    # -- video -------------------------------------------------------------

    def _read_loop(self) -> None:
        sock = self.video_sock
        try:
            while self.running and sock is not None:
                kind, payload, key, pts = read_media_packet(sock)
                packet = pack_packet(kind, payload, key, pts)
                if kind == PKT_SESSION:
                    self.session_packet = packet
                    self.video_size = struct.unpack(">II", payload)
                elif kind == PKT_CONFIG:
                    self.config_packet = packet
                else:
                    self.frames += 1
                    now = time.monotonic()
                    self._fps_window.append(now)
                    while self._fps_window and now - self._fps_window[0] > 2.0:
                        self._fps_window.pop(0)
                self._broadcast(packet, kind, key)
        except (OSError, ConnectionError) as exc:
            if self.running:
                self.error = f"stream ended: {exc}"
                logger.info("Android Control stream for %s ended: %s", self.serial, exc)
        finally:
            self.stop()

    def _broadcast(self, packet: bytes, kind: int, key: bool) -> None:
        lagging = False
        with self.lock:
            for sub in self.subscribers:
                if not sub.offer(packet, kind, key):
                    lagging = True
        if lagging:
            self.request_keyframe()

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        sub.queue.put_nowait(pack_packet(PKT_HELLO, sub.id.encode("ascii")))
        _viewers[sub.id] = (self, sub)
        with self.lock:
            # Late joiners: replay the current geometry and codec config, then
            # ask the encoder for a fresh key frame.
            for cached in (self.session_packet, self.config_packet):
                if cached:
                    sub.queue.put_nowait(cached)
            self.subscribers.append(sub)
        if self.config_packet:
            self.request_keyframe()
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        _viewers.pop(sub.id, None)
        with self.lock:
            if sub in self.subscribers:
                self.subscribers.remove(sub)
            if not self.subscribers:
                self.idle_since = time.monotonic()

    def viewer_count(self) -> int:
        with self.lock:
            return len(self.subscribers)

    def measured_fps(self) -> float:
        window = list(self._fps_window)
        if len(window) < 2:
            return 0.0
        return round((len(window) - 1) / max(0.001, window[-1] - window[0]), 1)

    # -- control -----------------------------------------------------------

    def _send_control(self, data: bytes) -> bool:
        sock = self.control_sock
        if sock is None or not self.running:
            return False
        try:
            with self.ctrl_lock:
                sock.sendall(data)
            return True
        except OSError:
            return False

    def request_keyframe(self) -> bool:
        return self._send_control(reset_video_message())

    def _video_point(self, nx: float, ny: float) -> tuple[int, int]:
        width, height = self.video_size
        return (
            int(min(max(nx, 0.0), 1.0) * max(width - 1, 0)),
            int(min(max(ny, 0.0), 1.0) * max(height - 1, 0)),
        )

    def tap(self, nx: float, ny: float, hold: float = 0.05) -> bool:
        width, height = self.video_size
        if not width or not height:
            return False
        x, y = self._video_point(nx, ny)
        if not self._send_control(touch_message(_ACTION_DOWN, x, y, width, height)):
            return False
        time.sleep(hold)
        return self._send_control(touch_message(_ACTION_UP, x, y, width, height, 0.0))

    def swipe(self, nx1: float, ny1: float, nx2: float, ny2: float, duration: float = 0.3) -> bool:
        width, height = self.video_size
        if not width or not height:
            return False
        x1, y1 = self._video_point(nx1, ny1)
        x2, y2 = self._video_point(nx2, ny2)
        if not self._send_control(touch_message(_ACTION_DOWN, x1, y1, width, height)):
            return False
        steps = max(4, int(duration / 0.016))
        for i in range(1, steps + 1):
            t = i / steps
            self._send_control(
                touch_message(_ACTION_MOVE, int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t), width, height)
            )
            time.sleep(duration / steps)
        return self._send_control(touch_message(_ACTION_UP, x2, y2, width, height, 0.0))

    def status(self) -> dict:
        return {
            "serial": self.serial,
            "running": self.running,
            "fps_target": self.fps,
            "fps_measured": self.measured_fps(),
            "max_size": self.max_size,
            "video_size": list(self.video_size),
            "viewers": self.viewer_count(),
            "frames": self.frames,
            "error": self.error,
            "device_name": self.device_name,
        }


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

_sessions_lock = threading.Lock()
_sessions: dict[str, StreamSession] = {}
_viewers: dict[str, tuple] = {}


def ping(viewer_id: str) -> bool:
    entry = _viewers.get(viewer_id)
    if not entry:
        return False
    entry[1].last_ping = time.monotonic()
    return True


def close_viewer(viewer_id: str) -> bool:
    entry = _viewers.get(viewer_id)
    if not entry:
        return False
    session, sub = entry
    sub.closed = True
    session.unsubscribe(sub)
    return True
_reaper: threading.Thread | None = None


def _settings(fps=None, max_size=None) -> tuple[int, int, int]:
    cfg = _load_config()
    try:
        fps = int(fps or cfg.get("preview_fps") or 30)
    except (TypeError, ValueError):
        fps = 30
    fps = min(FPS_CHOICES, key=lambda choice: abs(choice - fps))
    try:
        max_size = int(max_size or cfg.get("preview_max_size") or 1024)
    except (TypeError, ValueError):
        max_size = 1024
    max_size = max(320, min(max_size, 2560)) // 8 * 8
    try:
        bit_rate = int(cfg.get("preview_bit_rate") or 4_000_000)
    except (TypeError, ValueError):
        bit_rate = 4_000_000
    return fps, max_size, bit_rate


def resolve(device: str | None) -> tuple[str, AdbBackend]:
    backend, resolution = _prepare([], device or "", None, True)
    serial = resolution.get("resolved_device") or ""
    if not serial:
        raise StreamError(f"No connected device{f' matching {device}' if device else ''}")
    return serial, backend


def get_session(device: str | None, fps=None, max_size=None, start: bool = True) -> StreamSession | None:
    """Return the running session for a device, (re)starting it if needed."""
    serial, backend = resolve(device)
    want_fps, want_size, bit_rate = _settings(fps, max_size)
    with _sessions_lock:
        session = _sessions.get(serial)
        if session and session.running and (fps is None or session.fps == want_fps) and (max_size is None or session.max_size == want_size):
            return session
        if not start:
            return session if session and session.running else None
        if session:
            session.stop()
        session = StreamSession(serial, backend, want_fps, want_size, bit_rate)
        _sessions[serial] = session
    try:
        session.start()
    except Exception:
        with _sessions_lock:
            if _sessions.get(serial) is session:
                del _sessions[serial]
        raise
    finally:
        session.starting = False
    _ensure_reaper()
    return session


def running_session(device: str | None) -> StreamSession | None:
    try:
        return get_session(device, start=False)
    except StreamError:
        return None


def stop_session(device: str | None) -> bool:
    try:
        serial, _ = resolve(device)
    except StreamError:
        return False
    with _sessions_lock:
        session = _sessions.pop(serial, None)
    if session:
        session.stop()
        return True
    return False


def sessions_status() -> list[dict]:
    with _sessions_lock:
        return [s.status() for s in _sessions.values()]


def _reap() -> None:
    while True:
        time.sleep(2.0)
        now = time.monotonic()
        with _sessions_lock:
            for serial, session in list(_sessions.items()):
                with session.lock:
                    # Drop viewers whose browser stopped pinging.
                    for sub in list(session.subscribers):
                        if now - sub.last_ping > VIEWER_TIMEOUT:
                            session.subscribers.remove(sub)
                            _viewers.pop(sub.id, None)
                            sub.closed = True
                    if not session.subscribers and session.idle_since < now - IDLE_SHUTDOWN:
                        idle = True
                    else:
                        idle = False
                        if session.subscribers:
                            session.idle_since = now
                if session.starting:
                    continue  # registered but still connecting; not dead
                if idle or not session.running:
                    session.stop()
                    del _sessions[serial]


def _ensure_reaper() -> None:
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return
    _reaper = threading.Thread(target=_reap, name="droidclaw-stream-reaper", daemon=True)
    _reaper.start()


def iter_stream(session: StreamSession, sub: Subscriber):
    """Blocking generator of browser packets (runs in the WSGI thread)."""
    keepalive = pack_packet(PKT_KEEPALIVE)
    try:
        while not sub.closed and time.monotonic() - sub.last_ping < VIEWER_TIMEOUT:
            try:
                packet = sub.queue.get(timeout=1.0)
            except queue.Empty:
                packet = keepalive
            if not packet:
                break
            yield packet
    finally:
        session.unsubscribe(sub)
