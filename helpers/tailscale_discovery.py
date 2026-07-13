"""Tailscale integration for Android Control device discovery.

This module handles all Tailscale daemon management, peer discovery, and
ADB port probing across the tailnet. It converts discovered peers into
AdbDevice objects for integration with the canonical device list.
"""

from __future__ import annotations

import asyncio
import re
import json
import os
import select
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Imports - handle both standalone and A0-embedded contexts
# ---------------------------------------------------------------------------

def _get_adb_backend():
    """Lazy import - avoids module-level import chain failures."""
    try:
        from usr.plugins.droidclaw.helpers.adb_backend import (
            AdbDevice,
            list_mdns_services,
            run_adb_result,
            select_backend,
        )
    except ImportError:
        try:
            from helpers.adb_backend import (
                AdbDevice,
                list_mdns_services,
                run_adb_result,
                select_backend,
            )
        except ImportError:
            from adb_backend import (
                AdbDevice,
                list_mdns_services,
                run_adb_result,
                select_backend,
            )
    return AdbDevice, run_adb_result, list_mdns_services, select_backend

try:
    import yaml
except Exception:
    yaml = None

try:
    from helpers import plugins
except Exception:
    plugins = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_NAME = "droidclaw"
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_FILE = _PLUGIN_DIR / "default_config.yaml"

TS_BINARY = "/a0/tmp/bin/tailscale"
TS_DAEMON = "/a0/tmp/bin/tailscaled"
TS_STATE_DIR = "/a0/usr/tailscale"
TS_SOCKET_DIR = "/a0/tmp/tailscale"
TS_SOCKET = f"{TS_SOCKET_DIR}/tailscaled.sock"
TS_LOG = f"{TS_SOCKET_DIR}/tailscaled.log"
TS_SOCKS_HOST = "127.0.0.1"
TS_SOCKS_PORT = 1055
TS_DEFAULT_ADB_PORT = 5555
TS_CACHE_TTL = 30
TS_DAEMON_STARTUP_WAIT = 5

# Common binary search locations (in priority order)
_TS_SEARCH_PATHS = [
    TS_BINARY,
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/usr/local/sbin/tailscale",
    "/snap/bin/tailscale",
    "/opt/tailscale/tailscale",
]

_TS_DAEMON_SEARCH_PATHS = [
    TS_DAEMON,
    "/usr/bin/tailscaled",
    "/usr/local/bin/tailscaled",
    "/usr/local/sbin/tailscaled",
    "/usr/sbin/tailscaled",
    "/opt/tailscale/tailscaled",
]

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_ts_status_cache: dict | None = None
_ts_status_cache_time: float = 0.0
_forward_lock = threading.Lock()
_adb_forwards: dict[tuple[str, int], "_TailnetAdbForward"] = {}


class _TailnetAdbForward:
    """Local TCP forward that sends ADB traffic through tailscaled SOCKS5."""

    def __init__(self, target_host: str, target_port: int):
        self.target_host = target_host
        self.target_port = int(target_port)
        self._closed = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(1.0)
        self.local_port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def local_endpoint(self) -> str:
        return f"127.0.0.1:{self.local_port}"

    def close(self) -> None:
        self._closed.set()
        try:
            self._listener.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._bridge, args=(client,), daemon=True).start()

    def _bridge(self, client: socket.socket) -> None:
        remote = None
        try:
            remote = _open_socks5_socket(self.target_host, self.target_port, timeout=10.0)
            client.setblocking(False)
            remote.setblocking(False)
            sockets = [client, remote]
            while not self._closed.is_set():
                readable, _, exceptional = select.select(sockets, [], sockets, 1.0)
                if exceptional:
                    break
                for source in readable:
                    try:
                        data = source.recv(65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        return
                    target = remote if source is client else client
                    target.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (client, remote):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise OSError("SOCKS5 proxy closed the connection")
        chunks.extend(chunk)
    return bytes(chunks)


def _open_socks5_socket(host: str, port: int, timeout: float = 5.0) -> socket.socket:
    sock = socket.create_connection((TS_SOCKS_HOST, TS_SOCKS_PORT), timeout=timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise OSError("tailscaled SOCKS5 proxy rejected authentication")

        try:
            packed_host = socket.inet_pton(socket.AF_INET, host)
            address = b"\x01" + packed_host
        except OSError:
            encoded = host.encode("idna")
            if len(encoded) > 255:
                raise OSError("SOCKS5 target hostname is too long")
            address = b"\x03" + bytes([len(encoded)]) + encoded

        sock.sendall(b"\x05\x01\x00" + address + int(port).to_bytes(2, "big"))
        header = _recv_exact(sock, 4)
        if header[0] != 5 or header[1] != 0:
            raise OSError(f"tailscaled SOCKS5 connect failed with code {header[1]}")
        if header[3] == 1:
            _recv_exact(sock, 4)
        elif header[3] == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        elif header[3] == 4:
            _recv_exact(sock, 16)
        else:
            raise OSError("tailscaled SOCKS5 proxy returned an invalid address type")
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def _socks_proxy_available(timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((TS_SOCKS_HOST, TS_SOCKS_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_adb_forward(host: str, port: int) -> dict:
    """Create or reuse a container-local ADB forward through Tailscale."""
    key = (str(host), int(port))
    with _forward_lock:
        forward = _adb_forwards.get(key)
        if forward is None:
            if not _socks_proxy_available():
                return {
                    "success": False,
                    "message": "tailscaled userspace proxy is not available; restart Android Control and try again",
                }
            forward = _TailnetAdbForward(*key)
            _adb_forwards[key] = forward
        return {
            "success": True,
            "target": f"{key[0]}:{key[1]}",
            "local_endpoint": forward.local_endpoint,
        }


def close_adb_forward(host: str, port: int) -> str:
    key = (str(host), int(port))
    with _forward_lock:
        forward = _adb_forwards.pop(key, None)
    if forward is None:
        return ""
    local_endpoint = forward.local_endpoint
    forward.close()
    return local_endpoint


def forwarded_adb_serial(serial: str) -> str:
    """Translate a public tailnet endpoint to its container-local ADB serial."""
    value = str(serial or "").strip()
    with _forward_lock:
        for (host, port), forward in _adb_forwards.items():
            if value in {host, f"{host}:{port}"}:
                return forward.local_endpoint
    return ""


def public_adb_serial(serial: str) -> str:
    """Translate an internal forward endpoint back to the tailnet endpoint."""
    value = str(serial or "").strip()
    with _forward_lock:
        for (host, port), forward in _adb_forwards.items():
            if value == forward.local_endpoint:
                return f"{host}:{port}"
    return ""


def has_adb_forward(host: str, port: int) -> bool:
    with _forward_lock:
        return (str(host), int(port)) in _adb_forwards


def _as_dict(value) -> dict:
    """Return a dict value or an empty dict for nullable CLI fields."""
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class TailscalePeer:
    """A Tailscale peer device discovered on the tailnet."""

    node_id: str
    hostname: str
    dns_name: str
    os: str
    ip: str
    ip6: str
    online: bool
    last_seen: str
    exit_node: bool
    rx_bytes: int
    tx_bytes: int
    adb_reachable: bool = False
    adb_port: int = TS_DEFAULT_ADB_PORT
    adb_port_source: str = "default"
    adb_port_candidates: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "hostname": self.hostname,
            "dns_name": self.dns_name,
            "os": self.os,
            "ip": self.ip,
            "ip6": self.ip6,
            "online": self.online,
            "last_seen": self.last_seen,
            "exit_node": self.exit_node,
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
            "adb_reachable": self.adb_reachable,
            "adb_port": self.adb_port,
            "adb_port_source": self.adb_port_source,
            "adb_port_candidates": self.adb_port_candidates,
        }

    def to_adb_device(self) -> AdbDevice:
        """Convert to AdbDevice for integration with canonical_devices()."""
        AdbDevice, _, _, _ = _get_adb_backend()
        ip_port = f"{self.ip}:{self.adb_port}"
        port_aliases = [f"{self.ip}:{port}" for port in self.adb_port_candidates if port]
        return AdbDevice(
            serial=ip_port,
            state="device" if self.adb_reachable else "offline",
            model=self.hostname,
            product="tailscale",
            ip=self.ip,
            port=str(self.adb_port),
            aliases=[
                ip_port,
                self.ip,
                self.hostname,
                self.dns_name.rstrip("."),
                f"tailscale:{self.hostname}",
                f"tailscale:{self.node_id}",
            ] + port_aliases,
        )


@dataclass
class TailscaleStatus:
    """Overall Tailscale daemon and tailnet status."""

    installed: bool
    running: bool
    logged_in: bool
    tailnet_name: str
    magic_dns_suffix: str
    self_ip: str
    self_hostname: str
    peer_count: int
    android_peer_count: int
    version: str
    error: str

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "running": self.running,
            "logged_in": self.logged_in,
            "tailnet_name": self.tailnet_name,
            "magic_dns_suffix": self.magic_dns_suffix,
            "self_ip": self.self_ip,
            "self_hostname": self.self_hostname,
            "peer_count": self.peer_count,
            "android_peer_count": self.android_peer_count,
            "version": self.version,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Config Loading (mirrors adb_backend.py pattern)
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    """Load merged config from default_config.yaml and plugin config."""
    defaults = {}
    try:
        if yaml is not None and _DEFAULT_CONFIG_FILE.exists():
            defaults = yaml.safe_load(_DEFAULT_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        defaults = {}

    configured = {}
    try:
        if plugins is not None:
            configured = plugins.get_plugin_config(PLUGIN_NAME) or {}
    except Exception:
        configured = {}

    merged = dict(defaults)
    nested = configured.get("defaults") if isinstance(configured, dict) else None
    if isinstance(nested, dict):
        merged.update(nested)
    if isinstance(configured, dict):
        merged.update({k: v for k, v in configured.items() if k != "defaults"})
    return merged


# ---------------------------------------------------------------------------
# Cache Management
# ---------------------------------------------------------------------------


def invalidate_cache() -> None:
    """Clear the module-level status cache."""
    global _ts_status_cache, _ts_status_cache_time
    _ts_status_cache = None
    _ts_status_cache_time = 0.0


# ---------------------------------------------------------------------------
# Binary Discovery
# ---------------------------------------------------------------------------


def find_tailscale_binary() -> str | None:
    """Find the tailscale CLI binary.

    Checks TS_BINARY first, then PATH, then common locations.
    Returns the path or None if not found.
    """
    # Check defined constant path first
    if os.path.isfile(TS_BINARY) and os.access(TS_BINARY, os.X_OK):
        return TS_BINARY

    # Check if on PATH
    path_binary = shutil.which("tailscale")
    if path_binary:
        return path_binary

    # Check common locations
    for candidate in _TS_SEARCH_PATHS:
        if candidate != TS_BINARY and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


def _find_daemon_binary() -> str | None:
    """Find the tailscaled daemon binary."""
    if os.path.isfile(TS_DAEMON) and os.access(TS_DAEMON, os.X_OK):
        return TS_DAEMON

    path_daemon = shutil.which("tailscaled")
    if path_daemon:
        return path_daemon

    for candidate in _TS_DAEMON_SEARCH_PATHS:
        if candidate != TS_DAEMON and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


# ---------------------------------------------------------------------------
# Daemon Management
# ---------------------------------------------------------------------------


def is_daemon_running() -> bool:
    """Check if tailscaled is running and responding.

    First checks for the process, then verifies the socket responds.
    """
    # Check if socket exists and is alive
    if not os.path.exists(TS_SOCKET):
        # Fallback: check if any tailscaled process is running
        try:
            result = subprocess.run(
                ["pgrep", "-f", "tailscaled"],
                capture_output=True,
                timeout=3,
            )
            return result.returncode == 0
        except Exception:
            return False

    # Verify the daemon responds via the CLI
    binary = find_tailscale_binary()
    if not binary:
        return False

    try:
        result = subprocess.run(
            [binary, "--socket=" + TS_SOCKET, "status"],
            capture_output=True,
            timeout=5,
        )
        # If the daemon is running, status returns 0 (logged in) or non-zero (needs login)
        # but a socket error means it's NOT running
        if result.returncode == 0:
            return True
        # Check stderr for socket vs login issues
        stderr = result.stderr.decode("utf-8", errors="replace").lower()
        socket_errors = (
            "no such file",
            "connection refused",
            "failed to connect",
            "doesn't appear to be running",
            "does not appear to be running",
            "dial unix",
        )
        if any(marker in stderr for marker in socket_errors):
            return False
        if "not connected" in stderr or "login" in stderr or "auth" in stderr:
            return True  # Daemon is running but not logged in
        # Default: if we got output and no socket error, daemon is alive
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def ensure_daemon_running(timeout: int = 15) -> tuple[bool, str]:
    """Start tailscaled if not running.

    Uses userspace-networking mode (no TUN device needed in Docker).
    Returns (success, message).
    """
    # Already running?
    if is_daemon_running():
        return True, "tailscaled already running"

    daemon_bin = _find_daemon_binary()
    if not daemon_bin:
        return False, "tailscaled binary not found"

    # Ensure directories exist
    try:
        os.makedirs(TS_SOCKET_DIR, exist_ok=True)
        os.makedirs(TS_STATE_DIR, exist_ok=True)
    except Exception as e:
        return False, f"Failed to create directories: {e}"

    # Clean up stale socket
    if os.path.exists(TS_SOCKET):
        try:
            os.unlink(TS_SOCKET)
        except Exception:
            pass

    # Start the daemon in userspace-networking mode
    log_fd = None
    try:
        log_fd = open(TS_LOG, "a")
    except Exception:
        pass

    try:
        proc = subprocess.Popen(
            [
                daemon_bin,
                "--tun=userspace-networking",
                f"--socks5-server={TS_SOCKS_HOST}:{TS_SOCKS_PORT}",
                f"--socket={TS_SOCKET}",
                f"--statedir={TS_STATE_DIR}",
            ],
            stdout=log_fd,
            stderr=log_fd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        if log_fd:
            log_fd.close()
        return False, f"Failed to start tailscaled: {e}"

    # Wait for the socket to appear and daemon to respond
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if os.path.exists(TS_SOCKET):
            if is_daemon_running():
                return True, f"tailscaled started (pid={proc.pid})"

    # Timeout
    if log_fd:
        log_fd.close()
    return False, f"tailscaled did not become ready within {timeout}s"


# ---------------------------------------------------------------------------
# CLI Execution
# ---------------------------------------------------------------------------


def _run_tailscale(args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run tailscale CLI with the custom socket path.

    Returns (returncode, stdout, stderr).
    """
    binary = find_tailscale_binary()
    if not binary:
        return -1, "", "tailscale binary not found"

    cmd = [binary, f"--socket={TS_SOCKET}"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        return result.returncode, stdout, stderr
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode("utf-8", errors="replace")
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")
        return -2, stdout, stderr + f"\nCommand timed out after {timeout}s"
    except Exception as e:
        return -3, "", str(e)


# ---------------------------------------------------------------------------
# Status & Peer Discovery
# ---------------------------------------------------------------------------


def get_tailscale_status_json(use_cache: bool = True) -> dict | None:
    """Get parsed `tailscale status --json` output.

    Uses module-level cache with TS_CACHE_TTL.
    """
    global _ts_status_cache, _ts_status_cache_time

    # Check cache
    if use_cache and _ts_status_cache is not None:
        age = time.time() - _ts_status_cache_time
        if age < TS_CACHE_TTL:
            return _ts_status_cache

    # Run the command
    rc, stdout, stderr = _run_tailscale(["status", "--json"], timeout=15)
    if rc != 0:
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None

    # Update cache
    _ts_status_cache = data
    _ts_status_cache_time = time.time()

    return data


def get_tailscale_status() -> TailscaleStatus:
    """Get high-level Tailscale status for UI display."""
    binary = find_tailscale_binary()
    installed = binary is not None

    if not installed:
        return TailscaleStatus(
            installed=False,
            running=False,
            logged_in=False,
            tailnet_name="",
            magic_dns_suffix="",
            self_ip="",
            self_hostname="",
            peer_count=0,
            android_peer_count=0,
            version="",
            error="Tailscale binary not found",
        )

    running = is_daemon_running()
    if not running:
        return TailscaleStatus(
            installed=True,
            running=False,
            logged_in=False,
            tailnet_name="",
            magic_dns_suffix="",
            self_ip="",
            self_hostname="",
            peer_count=0,
            android_peer_count=0,
            version="",
            error="tailscaled not running",
        )

    data = get_tailscale_status_json(use_cache=False)
    if data is None:
        return TailscaleStatus(
            installed=True,
            running=True,
            logged_in=False,
            tailnet_name="",
            magic_dns_suffix="",
            self_ip="",
            self_hostname="",
            peer_count=0,
            android_peer_count=0,
            version="",
            error="Failed to get status from daemon",
        )

    # Extract fields from JSON
    version = data.get("Version", "")

    self_info = _as_dict(data.get("Self"))
    self_hostname = self_info.get("HostName", "")
    self_ips = self_info.get("TailscaleIPs") or []
    self_ip = self_ips[0] if self_ips else ""

    magic_dns = data.get("MagicDNSSuffix") or ""

    # Check if logged in
    backend_state = data.get("BackendState") or ""
    logged_in = backend_state == "Running"

    # Tailnet name
    tailnet_info = _as_dict(data.get("CurrentTailnet"))
    tailnet_name = tailnet_info.get("Name", "")

    # Peers
    peers = _as_dict(data.get("Peer"))
    peer_count = len(peers)

    # Count Android peers
    android_count = 0
    for peer_data in peers.values():
        peer_os = _as_dict(peer_data).get("OS", "").lower()
        if "android" in peer_os:
            android_count += 1

    error = "" if logged_in else f"Backend state: {backend_state}"

    return TailscaleStatus(
        installed=True,
        running=True,
        logged_in=logged_in,
        tailnet_name=tailnet_name,
        magic_dns_suffix=magic_dns,
        self_ip=self_ip,
        self_hostname=self_hostname,
        peer_count=peer_count,
        android_peer_count=android_count,
        version=version,
        error=error,
    )


def _parse_peer(node_id: str, peer_data: dict) -> TailscalePeer:
    """Parse a single peer from the JSON status output."""
    peer_data = _as_dict(peer_data)
    ips = peer_data.get("TailscaleIPs") or []
    ip4 = ""
    ip6 = ""
    for addr in ips:
        if ":" in addr:
            if not ip6:
                ip6 = addr
        else:
            if not ip4:
                ip4 = addr

    return TailscalePeer(
        node_id=str(node_id),
        hostname=peer_data.get("HostName", ""),
        dns_name=peer_data.get("DNSName", ""),
        os=peer_data.get("OS", ""),
        ip=ip4,
        ip6=ip6,
        online=bool(peer_data.get("Online", False)),
        last_seen=peer_data.get("LastSeen", ""),
        exit_node=bool(peer_data.get("ExitNode", False)),
        rx_bytes=int(peer_data.get("RxBytes") or 0),
        tx_bytes=int(peer_data.get("TxBytes") or 0),
    )


def list_tailscale_peers(
    os_filter: list[str] | None = None,
    online_only: bool = False,
    config: dict | None = None,
) -> list[TailscalePeer]:
    """Enumerate Tailscale peers on the tailnet with filtering.

    Args:
        os_filter: If provided, only include peers whose OS matches.
                   e.g. ["android"] to list only Android devices.
        online_only: If True, only include peers that are currently online.
        config: Optional config dict; if provided, reads filter defaults from it.
    """
    # Apply config-driven defaults if config is provided
    if config:
        if os_filter is None and config.get("tailscale_os_filter"):
            os_filter = config["tailscale_os_filter"]
        if not online_only and config.get("tailscale_online_only"):
            online_only = config["tailscale_online_only"]

    if isinstance(os_filter, str):
        os_filter = [os_filter]

    data = get_tailscale_status_json()
    if data is None:
        return []

    peers: list[TailscalePeer] = []
    raw_peers = _as_dict(data.get("Peer"))

    for node_id, peer_data in raw_peers.items():
        peer = _parse_peer(str(node_id), peer_data)

        # Apply OS filter
        if os_filter:
            filter_lower = [o.lower() for o in os_filter]
            if peer.os.lower() not in filter_lower:
                continue

        # Apply online filter
        if online_only and not peer.online:
            continue

        peers.append(peer)

    return peers


# ---------------------------------------------------------------------------
# ADB Port Probing
# ---------------------------------------------------------------------------


def _coerce_port(value, default: int = TS_DEFAULT_ADB_PORT) -> int:
    try:
        port = int(str(value).strip())
    except Exception:
        return default
    if 1 <= port <= 65535:
        return port
    return default


def discover_adb_port_candidates(config: dict | None = None) -> tuple[list[int], dict[int, str]]:
    """Return ADB ports worth probing on Tailscale peers.

    The configured port is the classic `adb tcpip` port Android Control can set.
    mDNS ports come from already-paired Wireless debugging services and may be
    random per device/session.
    """
    if config is None:
        config = _load_config()

    default_port = _coerce_port(config.get("tailscale_adb_port", TS_DEFAULT_ADB_PORT))
    manual_port = config.get("manual_adb_port") or config.get("adb_port_override")
    ports: list[int] = []
    sources: dict[int, str] = {}

    def add_port(value, source: str) -> None:
        port = _coerce_port(value, 0)
        if not port or port in ports:
            return
        ports.append(port)
        sources[port] = source

    add_port(manual_port, "manual")
    add_port(default_port, "classic")

    try:
        _, _, list_mdns_services, select_backend = _get_adb_backend()
        seen_prefixes: set[tuple[str, ...]] = set()
        for prefer in (None, "host", "container"):
            try:
                backend = select_backend(prefer)
                key = tuple(backend.command_prefix)
                if key in seen_prefixes:
                    continue
                seen_prefixes.add(key)
                for service in list_mdns_services(backend):
                    if not service.port:
                        continue
                    source = "wireless" if "tls-connect" in service.service_type else "mdns"
                    add_port(service.port, source)
            except Exception:
                continue
    except Exception:
        pass

    return ports or [default_port], sources


async def probe_adb_port(ip: str, port: int = TS_DEFAULT_ADB_PORT, timeout: float = 2.0) -> bool:
    """Async TCP connect probe to check if ADB port is open.

    Returns True if the port accepts a connection within the timeout.
    """
    if _socks_proxy_available():
        try:
            sock = await asyncio.wait_for(
                asyncio.to_thread(_open_socks5_socket, ip, port, timeout),
                timeout=timeout + 1.0,
            )
            sock.close()
            return True
        except (asyncio.TimeoutError, OSError):
            return False

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return False


async def discover_adb_peers(
    config: dict | None = None,
    probe_ports: bool = True,
) -> list[TailscalePeer]:
    """Discover Tailscale peers that may expose ADB.

    1. Calls list_tailscale_peers() with config-driven filters
    2. Builds ADB port candidates from classic TCP/IP config and mDNS
    3. Probes those ports on each peer and preserves the reachable port
    """
    if config is None:
        config = _load_config()

    # Get config-driven filter settings
    os_filter = config.get("tailscale_os_filter", None)
    online_only = config.get("tailscale_online_only", True)
    adb_port = _coerce_port(config.get("tailscale_adb_port", TS_DEFAULT_ADB_PORT))
    port_candidates, port_sources = discover_adb_port_candidates(config)

    # Discover peers
    peers = list_tailscale_peers(
        os_filter=os_filter,
        online_only=online_only,
        config=config,
    )

    if not peers or not probe_ports:
        # Set default port without probing
        for peer in peers:
            peer.adb_port = adb_port
            peer.adb_port_source = port_sources.get(adb_port, "classic")
            peer.adb_port_candidates = list(port_candidates)
        return peers

    # Probe ADB ports concurrently
    async def _probe_and_set(peer: TailscalePeer) -> None:
        peer.adb_port = port_candidates[0] if port_candidates else adb_port
        peer.adb_port_source = port_sources.get(peer.adb_port, "classic")
        peer.adb_port_candidates = list(port_candidates)
        if peer.online and peer.ip:
            for candidate in port_candidates:
                if await probe_adb_port(peer.ip, candidate):
                    peer.adb_port = candidate
                    peer.adb_port_source = port_sources.get(candidate, "probe")
                    peer.adb_reachable = True
                    return
            peer.adb_reachable = False

    await asyncio.gather(*[_probe_and_set(p) for p in peers])

    return peers


# ---------------------------------------------------------------------------
# IP Resolution
# ---------------------------------------------------------------------------


def resolve_tailscale_ip(hostname: str) -> str | None:
    """Resolve a hostname to its Tailscale IPv4.

    Matches against hostname, DNS name, and node ID.
    Returns the IPv4 or None if not found.
    """
    data = get_tailscale_status_json()
    if data is None:
        return None

    hostname_lower = hostname.lower().rstrip(".")

    # Check all peers
    raw_peers = _as_dict(data.get("Peer"))
    for node_id, peer_data in raw_peers.items():
        peer = _parse_peer(str(node_id), peer_data)
        candidates = [
            peer.hostname.lower(),
            peer.dns_name.lower().rstrip("."),
            peer.ip.lower(),
            peer.ip6.lower(),
            str(node_id).lower(),
        ]
        if hostname_lower in candidates:
            return peer.ip or peer.ip6

    # Also check self (the local node)
    self_info = _as_dict(data.get("Self"))
    self_hostname = self_info.get("HostName", "").lower()
    self_dns = self_info.get("DNSName", "").lower().rstrip(".")
    if hostname_lower in [self_hostname, self_dns]:
        ips = self_info.get("TailscaleIPs") or []
        for addr in ips:
            if ":" not in addr:
                return addr

    return None


# ---------------------------------------------------------------------------
# Main Entry Point for adb_backend Integration
# ---------------------------------------------------------------------------


def get_tailscale_devices_for_canonical(config: dict | None = None) -> list[AdbDevice]:
    AdbDevice, _, _, _ = _get_adb_backend()
    """Main entry point called by adb_backend.py canonical_devices().

    1. Ensures daemon is running
    2. Discovers ADB peers
    3. Converts to AdbDevice objects
    4. Returns empty list on any error (graceful degradation)
    """
    if config is None:
        config = _load_config()

    try:
        # Ensure the daemon is running
        ok, msg = ensure_daemon_running(timeout=TS_DAEMON_STARTUP_WAIT + 10)
        if not ok:
            return []

        # Discover peers with ADB probing
        probe_ports = config.get("tailscale_probe_ports", True)
        peers = asyncio.run(discover_adb_peers(config=config, probe_ports=probe_ports))

        # The normal selector represents connected ADB transports. Tailnet
        # inventory stays in the remote-device list until ADB is forwarded.
        devices = [
            peer.to_adb_device()
            for peer in peers
            if peer.adb_reachable and has_adb_forward(peer.ip, peer.adb_port)
        ]
        return devices

    except Exception:
        # Graceful degradation: return empty list on any error
        return []


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def tailscale_diagnostics() -> dict:
    """Return diagnostic info for the device_status response.

    Provides a comprehensive snapshot of Tailscale state for UI display.
    """
    binary = find_tailscale_binary()
    daemon_binary = _find_daemon_binary()
    running = is_daemon_running()
    status = get_tailscale_status()

    # Get peer summary (without port probing for speed)
    peers = list_tailscale_peers() if running else []

    diag = {
        "binary_found": binary,
        "daemon_binary_found": daemon_binary,
        "socket_path": TS_SOCKET,
        "socket_exists": os.path.exists(TS_SOCKET),
        "state_dir": TS_STATE_DIR,
        "state_dir_exists": os.path.isdir(TS_STATE_DIR),
        "log_path": TS_LOG,
        "running": running,
        "status": status.to_dict(),
        "peer_count": len(peers),
        "peers": [p.to_dict() for p in peers[:20]],  # Cap for response size
        "cache_ttl": TS_CACHE_TTL,
        "cache_age": time.time() - _ts_status_cache_time if _ts_status_cache else None,
    }

    return diag



# ---------------------------------------------------------------------------
# Device Bootstrap (Tailscale Migration)
# ---------------------------------------------------------------------------


def generate_auth_url() -> dict:
    """Generate a Tailscale auth URL for device onboarding.

    Uses `tailscale status` which immediately returns a login URL when
    the node is logged out. This is non-blocking, unlike `tailscale up --qr`
    which blocks waiting for browser auth to complete.

    Returns:
        {"success": True, "auth_url": url, "qr_available": bool} on success.
        {"success": False, "message": error} on failure.
    """
    # Ensure daemon is running before CLI calls
    daemon_ok, daemon_msg = ensure_daemon_running(timeout=20)
    if not daemon_ok:
        return {"success": False, "message": f"Tailscale daemon failed to start: {daemon_msg}"}

    auth_url_pattern = re.compile(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+")

    # Use `tailscale status` — non-blocking, returns login URL immediately when logged out
    rc, stdout, stderr = _run_tailscale(["status"], timeout=10)
    combined = stdout + "\n" + stderr
    match = auth_url_pattern.search(combined)
    if match:
        return {
            "success": True,
            "auth_url": match.group(0),
            "qr_available": True,
        }

    status = get_tailscale_status()
    if status.logged_in:
        return {
            "success": True,
            "auth_url": "",
            "qr_available": False,
            "logged_in": True,
            "message": "Android Control is already authorized on Tailscale.",
        }

    # Logged-out nodes can request a fresh auth URL. Do not run this for an
    # already logged-in node because it can push the container back to NeedsLogin.
    rc2, stdout2, stderr2 = _run_tailscale(["login", "--qr"], timeout=12)
    combined2 = stdout2 + "\n" + stderr2
    match2 = auth_url_pattern.search(combined2)
    if match2:
        return {
            "success": True,
            "auth_url": match2.group(0),
            "qr_available": True,
        }

    return {
        "success": False,
        "message": f"Could not generate auth URL. Daemon status: {combined.strip()[:200]}",
    }

def push_auth_to_device(serial: str, url: str) -> dict:
    """Push a URL to the phone's browser via ADB intent.

    Args:
        serial: ADB device serial.
        url: The auth URL to open on the device.

    Returns:
        {"success": bool, "message": str}
    """
    _, run_adb_result, _, _ = _get_adb_backend()
    result = run_adb_result(
        ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
        device=serial,
        timeout=10,
    )
    return {
        "success": result["returncode"] == 0,
        "message": result.get("output", ""),
    }


def enable_tcp_mode(serial: str, port: int = TS_DEFAULT_ADB_PORT) -> dict:
    AdbDevice, run_adb_result, _, _ = _get_adb_backend()
    """Enable ADB TCP mode on the requested port for wireless ADB.

    Args:
        serial: ADB device serial.
        port: TCP port to request. Defaults to the classic ADB TCP/IP port.

    Returns:
        {"success": bool, "message": str}
    """
    adb_port = _coerce_port(port)
    result = run_adb_result(
        ["tcpip", str(adb_port)],
        device=serial,
        timeout=10,
    )
    return {
        "success": result["returncode"] == 0,
        "message": result.get("output", ""),
        "port": adb_port,
    }


def wait_for_tailnet_join(timeout: int = 60) -> dict:
    """Poll tailscale status for a new Android peer joining the tailnet.

    Captures the current peer set, then polls every 3 seconds for a new
    Android peer that was not present at bootstrap start.

    Args:
        timeout: Maximum seconds to wait (default 60).

    Returns:
        {"success": True, "ip": ip, "hostname": hostname} on join detected.
        {"success": False, "message": error} on timeout.
    """
    invalidate_cache()

    # Capture baseline peer set before bootstrap
    baseline_data = get_tailscale_status_json(use_cache=False)
    if baseline_data is None:
        return {"success": False, "message": "Cannot read tailscale status"}

    baseline_peers = set()
    raw_peers = _as_dict(baseline_data.get("Peer"))
    for node_id, peer_data in raw_peers.items():
        peer = _parse_peer(str(node_id), peer_data)
        if peer.hostname:
            baseline_peers.add(peer.hostname)

    deadline = time.time() + timeout
    poll_interval = 3

    while time.time() < deadline:
        time.sleep(poll_interval)
        invalidate_cache()

        data = get_tailscale_status_json(use_cache=False)
        if data is None:
            continue

        raw_peers = _as_dict(data.get("Peer"))
        for node_id, peer_data in raw_peers.items():
            peer = _parse_peer(str(node_id), peer_data)
            if not peer.hostname:
                continue
            if peer.hostname in baseline_peers:
                continue
            # New peer found - prefer Android, but accept any new peer
            if "android" in peer.os.lower():
                return {
                    "success": True,
                    "ip": peer.ip,
                    "hostname": peer.hostname,
                }

    return {
        "success": False,
        "message": f"Timed out after {timeout}s waiting for device to join tailnet",
    }


def bootstrap_tailscale(serial: str, auth_url: str = "") -> dict:
    """Main orchestrator: upgrade a connected ADB device to Tailscale.

    Two paths:
    1. If device is already on tailnet → enable TCP mode + connect directly
    2. If device is NOT on tailnet → generate auth URL, push to phone,
       show QR/URL as fallback, wait for join, then connect

    Returns auth_url even on failure so UI can show QR fallback.
    """
    AdbDevice, run_adb_result, _, _ = _get_adb_backend()
    import asyncio as _asyncio
    config = _load_config()
    adb_port = _coerce_port(config.get("tailscale_adb_port", TS_DEFAULT_ADB_PORT))

    # Step 0: Ensure daemon is running
    daemon_ok, daemon_msg = ensure_daemon_running(timeout=20)
    if not daemon_ok:
        return {
            "success": False,
            "auth_url": "",
            "tailscale_ip": "",
            "hostname": "",
            "message": f"Tailscale daemon failed to start: {daemon_msg}",
        }

    # Step 1: Check if phone is already on the tailnet
    invalidate_cache()
    try:
        peers = list_tailscale_peers(config=None)
        android_peers = [p for p in peers if p.os == "android" and p.online]
    except Exception:
        android_peers = []

    if android_peers:
        # Device is already on tailnet — skip auth, go straight to connect
        # Enable TCP mode first
        try:
            tcp_result = enable_tcp_mode(serial, adb_port)
        except Exception:
            tcp_result = {"success": False}

        # Try each Android peer
        for peer in android_peers:
            ip_port = f"{peer.ip}:{adb_port}"
            try:
                connect_result = run_adb_result(
                    ["connect", ip_port],
                    timeout=15,
                )
                if connect_result["returncode"] == 0 and "connected" in connect_result.get("output", "").lower():
                    return {
                        "success": True,
                        "auth_url": "",
                        "tailscale_ip": peer.ip,
                        "hostname": peer.hostname,
                        "serial": ip_port,
                        "message": f"Device {peer.hostname} already on tailnet. Connected at {ip_port}",
                    }
            except Exception:
                pass

    # Step 2: Device not on tailnet (or connect failed) — generate auth URL
    if not auth_url:
        auth_result = generate_auth_url()
        auth_url = auth_result.get("auth_url", "")

        if not auth_result["success"]:
            return {
                "success": False,
                "auth_url": "",
                "tailscale_ip": "",
                "hostname": "",
                "message": f"Failed to generate auth URL: {auth_result.get('message', '')}",
            }

    # Step 3: Enable TCP mode
    try:
        enable_tcp_mode(serial, adb_port)
    except Exception:
        pass

    # Step 4: Push auth URL to phone browser (auto-setup)
    push_ok = False
    try:
        push_result = push_auth_to_device(serial, auth_url)
        push_ok = push_result.get("success", False)
    except Exception:
        pass

    # Step 5: Wait for device to join tailnet
    join_result = wait_for_tailnet_join(timeout=60)
    if join_result["success"]:
        tailscale_ip = join_result["ip"]
        hostname = join_result["hostname"]

        # Step 6: Connect ADB over Tailscale IP
        connect_result = run_adb_result(
            ["connect", f"{tailscale_ip}:{adb_port}"],
            timeout=15,
        )

        if connect_result["returncode"] == 0:
            return {
                "success": True,
                "auth_url": auth_url,
                "tailscale_ip": tailscale_ip,
                "hostname": hostname,
                "serial": f"{tailscale_ip}:{adb_port}",
                "message": f"Device {hostname} bootstrapped to Tailscale at {tailscale_ip}:{adb_port}",
            }

    # Step 6 (fallback): Auth generated but device didn't join in time
    # Return auth_url so UI can show QR + manual URL
    push_msg = "Auto-push sent to device." if push_ok else "Auto-push failed."
    return {
        "success": False,
        "auth_url": auth_url,
        "tailscale_ip": "",
        "hostname": "",
        "message": f"{push_msg} Use the QR code or URL below to complete setup manually.",
    }
