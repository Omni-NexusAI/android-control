"""Shared ADB backend selection and command helpers for Android Control.

Every ADB call made by the plugin flows through ``run_adb_result`` (sync) or
``run_adb_async`` (async). Both share three caches so that an individual
command does not have to re-probe the world before it runs:

* the selected backend (host bridge or container-local ADB server) is sticky
  for ``BACKEND_TTL`` seconds and is only re-probed early when a command fails
  with a server-level error;
* ``adb devices -l`` / ``adb mdns services`` output is cached for a couple of
  seconds;
* Tailscale peer discovery is refreshed in a background thread instead of on
  the command hot path.

When a command fails because the device transport dropped (wireless ADB going
idle, a Wireless debugging port change, a transient ``offline`` state), the
runner asks ``adb_session.recover_device`` to reconnect and retries the command
once, so the agent does not have to spend tool calls reconnecting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from helpers import plugins
from usr.plugins.droidclaw.helpers.platform_tools import find_adb

try:
    import yaml
except Exception:  # pragma: no cover - Agent Zero normally includes PyYAML
    yaml = None

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_FILE = _PLUGIN_DIR / "default_config.yaml"

CONFIG_TTL = 2.0
BACKEND_TTL = 60.0
DEVICES_TTL = 2.0
MDNS_TTL = 5.0

# Output fragments that mean "the transport to the device is gone" rather than
# "the command itself failed". These are worth a reconnect + single retry.
_TRANSPORT_ERRORS = (
    "device offline",
    "device unauthorized",
    "no devices/emulators found",
    "device not found",
    "device still connecting",
    "error: closed",
    "protocol fault",
    "connection reset",
    "broken pipe",
    "transport error",
    "adb device is not connected",
)
# Output fragments that mean the ADB *server* itself was unreachable.
_SERVER_ERRORS = (
    "cannot connect to daemon",
    "failed to connect to server",
    "connection refused",
    "could not connect to adb server",
    "daemon not running",
)


@dataclass(frozen=True)
class AdbBackend:
    name: str
    command_prefix: list[str]
    message: str = ""


@dataclass
class AdbDevice:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""
    ip: str = ""
    port: str = ""
    aliases: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "state": self.state,
            "model": self.model,
            "product": self.product,
            "device": self.device,
            "transport_id": self.transport_id,
            "ip": self.ip,
            "port": self.port,
            "aliases": self.aliases or [],
        }

    @property
    def wireless(self) -> bool:
        return bool(self.ip) or "._adb" in self.serial


@dataclass
class MdnsService:
    name: str
    service_type: str
    address: str

    @property
    def host(self) -> str:
        if ":" not in self.address:
            return self.address
        return self.address.rsplit(":", 1)[0]

    @property
    def port(self) -> str:
        if ":" not in self.address:
            return ""
        return self.address.rsplit(":", 1)[1]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "service_type": self.service_type,
            "address": self.address,
            "host": self.host,
            "port": self.port,
        }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_config_lock = threading.Lock()
_config_cache: tuple[float, dict] | None = None


def _read_config() -> dict:
    defaults = {}
    try:
        if yaml is not None:
            defaults = yaml.safe_load(_DEFAULT_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        defaults = {}

    try:
        configured = plugins.get_plugin_config(PLUGIN_NAME) or {}
    except Exception:
        configured = {}

    merged = dict(defaults)
    nested = configured.get("defaults") if isinstance(configured, dict) else None
    if isinstance(nested, dict):
        merged.update(nested)
    if isinstance(configured, dict):
        merged.update({key: value for key, value in configured.items() if key != "defaults"})
    return merged


def _load_config() -> dict:
    """Merged plugin config (defaults + user config), cached for CONFIG_TTL."""
    global _config_cache
    now = time.monotonic()
    with _config_lock:
        if _config_cache is not None and now - _config_cache[0] < CONFIG_TTL:
            return dict(_config_cache[1])
    merged = _read_config()
    with _config_lock:
        _config_cache = (now, merged)
    return dict(merged)


def invalidate_config_cache() -> None:
    global _config_cache
    with _config_lock:
        _config_cache = None


# ---------------------------------------------------------------------------
# Device blocking / filtering
# ---------------------------------------------------------------------------

def _save_config_field(key: str, value) -> None:
    """Persist a single config field to the plugin's config.json."""
    import json
    config_path = _PLUGIN_DIR / "config.json"
    try:
        existing = {}
        if config_path.exists():
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        existing[key] = value
        config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Failed to save config field {key}: {exc}")
    invalidate_config_cache()
    invalidate_device_cache()


def _is_wired_device(device: AdbDevice) -> bool:
    """Return True if device is a wired/USB connection (no IP address)."""
    return not device.wireless


def _is_device_blocked(device: AdbDevice, config: dict | None = None) -> bool:
    """Check if a device should be blocked from discovery/streaming."""
    cfg = config if config is not None else _load_config()
    if bool(cfg.get("block_all_wired", False)) and _is_wired_device(device):
        return True
    blocked_serials = set(cfg.get("blocked_devices", []) or [])
    serials_to_check = {device.serial}
    if device.aliases:
        serials_to_check.update(device.aliases)
    return bool(serials_to_check & blocked_serials)


def block_device(serial: str) -> dict:
    """Add a device serial to the blocked list."""
    cfg = _load_config()
    blocked = list(cfg.get("blocked_devices", []) or [])
    if serial not in blocked:
        blocked.append(serial)
        _save_config_field("blocked_devices", blocked)
    return {"success": True, "blocked_devices": blocked, "message": f"Blocked {serial}"}


def unblock_device(serial: str) -> dict:
    """Remove a device serial from the blocked list."""
    cfg = _load_config()
    blocked = list(cfg.get("blocked_devices", []) or [])
    blocked = [s for s in blocked if s != serial]
    _save_config_field("blocked_devices", blocked)
    return {"success": True, "blocked_devices": blocked, "message": f"Unblocked {serial}"}


def set_block_all_wired(enabled: bool) -> dict:
    """Toggle blocking all wired/USB devices."""
    _save_config_field("block_all_wired", bool(enabled))
    return {
        "success": True,
        "block_all_wired": bool(enabled),
        "message": f"Block all wired devices {'enabled' if enabled else 'disabled'}",
    }


def get_block_info() -> dict:
    """Return current device blocking configuration."""
    cfg = _load_config()
    return {
        "block_all_wired": bool(cfg.get("block_all_wired", False)),
        "blocked_devices": list(cfg.get("blocked_devices", []) or []),
    }


# ---------------------------------------------------------------------------
# Backend selection (sticky)
# ---------------------------------------------------------------------------

def _backend_mode(config: Optional[dict] = None) -> str:
    config = config if config is not None else _load_config()
    defaults = config.get("defaults", {}) if isinstance(config.get("defaults"), dict) else {}
    mode = str(config.get("adb_backend") or defaults.get("adb_backend") or "auto")
    return mode.lower().strip()


def _host_settings(config: Optional[dict] = None) -> tuple[str, str]:
    config = config if config is not None else _load_config()
    defaults = config.get("defaults", {}) if isinstance(config.get("defaults"), dict) else {}
    host = str(config.get("adb_host") or defaults.get("adb_host") or "host.docker.internal")
    port = str(config.get("adb_port") or defaults.get("adb_port") or "5037")
    return host, port


def _adb_client(config: Optional[dict] = None) -> dict:
    return find_adb(config if config is not None else _load_config())


def _adb_prefix(config: Optional[dict] = None) -> list[str]:
    adb = _adb_client(config)
    return [adb["path"]] if adb.get("available") and adb.get("path") else ["adb"]


def _container_backend(message: str = "Using container-local ADB server", config: Optional[dict] = None) -> AdbBackend:
    return AdbBackend(name="container", command_prefix=_adb_prefix(config), message=message)


def _host_backend(host: str, port: str, message: str = "", config: Optional[dict] = None) -> AdbBackend:
    return AdbBackend(
        name="host",
        command_prefix=_adb_prefix(config) + ["-H", host, "-P", str(port)],
        message=message or f"Using host ADB server at {host}:{port}",
    )


def _probe(prefix: list[str], timeout: int = 4) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            prefix + ["mdns", "check"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "adb executable was not found"
    except subprocess.TimeoutExpired:
        return False, "adb mdns check timed out"
    except Exception as exc:
        return False, str(exc)

    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    ).strip()
    if result.returncode == 0:
        return True, output
    return False, output or f"adb mdns check exited with {result.returncode}"


def _probe_with_retry(prefix: list[str]) -> tuple[bool, str]:
    """Probe twice before declaring a server unreachable.

    A single slow ``mdns check`` used to flip the session from the host bridge
    to the (device-less) container server, which looked like a disconnect.
    """
    ok, message = _probe(prefix)
    if ok:
        return ok, message
    time.sleep(0.2)
    return _probe(prefix, timeout=6)


def _select_backend_uncached(mode: str, config: dict) -> AdbBackend:
    host, port = _host_settings(config)
    adb = _adb_client(config)
    if not adb.get("available"):
        return _container_backend(f"ADB client is not available: {adb.get('message')}", config)

    host_prefix = _adb_prefix(config) + ["-H", host, "-P", str(port)]
    container_prefix = _adb_prefix(config)

    if mode in {"container", "local"}:
        return _container_backend("Configured to use container-local ADB server", config)

    if mode == "host":
        ok, message = _probe_with_retry(host_prefix)
        if ok:
            return _host_backend(host, port, message, config)
        return _host_backend(host, port, f"Configured host ADB was not reachable: {message}", config)

    if mode in {"auto_container_first", ""}:
        container_ok, container_message = _probe(container_prefix)
        if container_ok:
            return _container_backend(
                f"Using container-local ADB server ({container_message})",
                config,
            )

        ok, message = _probe_with_retry(host_prefix)
        if ok:
            return _host_backend(
                host,
                port,
                f"Container-local ADB was not available ({container_message}); using reachable host ADB ({message})",
                config,
            )

        return _container_backend(
            f"Container-local ADB check failed ({container_message}); host ADB at {host}:{port} also failed ({message})",
            config,
        )

    # auto / auto_host_first / anything else: prefer the host bridge.
    ok, message = _probe_with_retry(host_prefix)
    if ok:
        return _host_backend(host, port, message, config)

    container_ok, container_message = _probe(container_prefix)
    if container_ok:
        return _container_backend(
            f"Host ADB at {host}:{port} was not reachable ({message}); using container-local ADB",
            config,
        )

    return _container_backend(
        f"Host ADB at {host}:{port} was not reachable ({message}); container ADB check failed ({container_message})",
        config,
    )


_backend_lock = threading.Lock()
_backend_cache: dict[tuple, tuple[float, AdbBackend]] = {}


def _backend_key(mode: str, config: dict) -> tuple:
    host, port = _host_settings(config)
    return (mode, host, port, str(config.get("adb_path") or ""))


def select_backend(prefer: str | None = None, force: bool = False) -> AdbBackend:
    """Select the ADB server Android Control should use for all commands.

    The result is cached for ``BACKEND_TTL`` seconds. Call
    ``invalidate_backend()`` (done automatically on server-level errors) to
    re-probe early.
    """
    config = _load_config()
    mode = (prefer or _backend_mode(config)).lower().strip()
    key = _backend_key(mode, config)
    now = time.monotonic()
    if not force:
        with _backend_lock:
            cached = _backend_cache.get(key)
            if cached and now - cached[0] < BACKEND_TTL:
                return cached[1]

    backend = _select_backend_uncached(mode, config)
    with _backend_lock:
        previous = _backend_cache.get(key)
        _backend_cache[key] = (time.monotonic(), backend)
    if previous and previous[1].name != backend.name:
        logger.info("Android Control ADB backend changed: %s -> %s (%s)", previous[1].name, backend.name, backend.message)
        invalidate_device_cache()
    return backend


def invalidate_backend() -> None:
    with _backend_lock:
        _backend_cache.clear()


def adb_cmd(args: list[str], device: str | None = None, backend: AdbBackend | None = None) -> list[str]:
    selected = backend or select_backend()
    cmd = list(selected.command_prefix)
    if device:
        cmd.extend(["-s", device])
    cmd.extend([str(arg) for arg in args])
    return cmd


_env_lock = threading.Lock()
_env_synced_mtime: float | None = None


def _adb_subprocess_env(backend: AdbBackend) -> dict:
    global _env_synced_mtime
    env = os.environ.copy()
    key_dir = _PLUGIN_DIR / "data" / "adb_keys"
    key_file = key_dir / "adbkey"
    if backend.name == "container" and key_file.is_file():
        env["ADB_VENDOR_KEYS"] = str(key_file)
        # Some platform-tools builds only load the standard HOME key when they
        # launch the ADB server. Keep it synchronized with the trusted runtime
        # key without ever placing key material in the repository.
        try:
            mtime = key_file.stat().st_mtime
        except OSError:
            mtime = None
        with _env_lock:
            if mtime is not None and mtime == _env_synced_mtime:
                return env
            try:
                standard_dir = Path.home() / ".android"
                standard_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                standard_key = standard_dir / "adbkey"
                if not standard_key.exists() or standard_key.read_bytes() != key_file.read_bytes():
                    shutil.copy2(key_file, standard_key)
                    standard_key.chmod(0o600)
                public_key = key_dir / "adbkey.pub"
                if public_key.is_file():
                    standard_public = standard_dir / "adbkey.pub"
                    if not standard_public.exists() or standard_public.read_bytes() != public_key.read_bytes():
                        shutil.copy2(public_key, standard_public)
                        standard_public.chmod(0o644)
                _env_synced_mtime = mtime
            except OSError:
                pass
    return env


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _extract_ip_port(value: str) -> tuple[str, str]:
    match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?", value or "")
    if not match:
        return "", ""
    return match.group(1), match.group(2) or ""


def parse_devices(output: str) -> list[AdbDevice]:
    devices: list[AdbDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        state = parts[1]
        fields = {}
        for part in parts[2:]:
            if ":" in part:
                key, value = part.split(":", 1)
                fields[key] = value

        ip, port = _extract_ip_port(serial)
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                model=fields.get("model", ""),
                product=fields.get("product", ""),
                device=fields.get("device", ""),
                transport_id=fields.get("transport_id", ""),
                ip=ip,
                port=port,
                aliases=[],
            )
        )
    return devices


def parse_mdns_services(output: str) -> list[MdnsService]:
    services: list[MdnsService] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of discovered"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        address = parts[-1]
        service_type = parts[-2]
        name = " ".join(parts[:-2])
        if service_type.startswith("_adb"):
            services.append(MdnsService(name=name, service_type=service_type, address=address))
    return services


# ---------------------------------------------------------------------------
# Cached device / mDNS listings
# ---------------------------------------------------------------------------

_list_lock = threading.Lock()
_devices_cache: dict[tuple, tuple[float, list[AdbDevice]]] = {}
_mdns_cache: dict[tuple, tuple[float, list[MdnsService]]] = {}


def invalidate_device_cache() -> None:
    with _list_lock:
        _devices_cache.clear()
        _mdns_cache.clear()


def _copy_devices(devices: list[AdbDevice]) -> list[AdbDevice]:
    return [AdbDevice(**{**d.to_dict(), "aliases": list(d.aliases or [])}) for d in devices]


def list_mdns_services(backend: AdbBackend | None = None, max_age: float = MDNS_TTL) -> list[MdnsService]:
    selected = backend or select_backend()
    key = tuple(selected.command_prefix)
    now = time.monotonic()
    with _list_lock:
        cached = _mdns_cache.get(key)
        if cached and now - cached[0] < max_age:
            return list(cached[1])
    try:
        result = _execute(["mdns", "services"], None, 8, None, selected)
    except Exception:
        return []
    services = parse_mdns_services(result["output"]) if result["returncode"] == 0 else []
    with _list_lock:
        _mdns_cache[key] = (time.monotonic(), services)
    return list(services)


def list_connected_devices(backend: AdbBackend | None = None, max_age: float = DEVICES_TTL) -> list[AdbDevice]:
    selected = backend or select_backend()
    key = tuple(selected.command_prefix)
    now = time.monotonic()
    with _list_lock:
        cached = _devices_cache.get(key)
        if cached and now - cached[0] < max_age:
            return _copy_devices(cached[1])
    try:
        result = _execute(["devices", "-l"], None, 8, None, selected)
    except Exception:
        return []
    if result["returncode"] != 0:
        return []
    devices = parse_devices(result["output"])
    with _list_lock:
        _devices_cache[key] = (time.monotonic(), devices)
    return _copy_devices(devices)


# ---------------------------------------------------------------------------
# Tailscale merge (background-refreshed, never on the hot path)
# ---------------------------------------------------------------------------

_ts_lock = threading.Lock()
_ts_devices: list[AdbDevice] = []
_ts_refreshed_at = 0.0
_ts_refreshing = False


def _refresh_tailscale_devices(config: dict) -> None:
    global _ts_devices, _ts_refreshed_at, _ts_refreshing
    try:
        from usr.plugins.droidclaw.helpers.tailscale_discovery import get_tailscale_devices_for_canonical

        devices = get_tailscale_devices_for_canonical(config)
    except Exception:
        devices = []
    with _ts_lock:
        _ts_devices = list(devices or [])
        _ts_refreshed_at = time.monotonic()
        _ts_refreshing = False


def tailscale_devices(config: dict | None = None, wait: bool = False) -> list[AdbDevice]:
    """Return Tailscale-forwarded ADB devices from cache.

    Refreshes in a background thread when the cache is older than
    ``tailscale_cache_ttl``. With ``wait=True`` (only from worker threads or
    explicit status requests) the refresh runs inline.
    """
    global _ts_refreshing
    cfg = config if config is not None else _load_config()
    try:
        ttl = float(cfg.get("tailscale_cache_ttl", 30) or 30)
    except (TypeError, ValueError):
        ttl = 30.0
    now = time.monotonic()
    with _ts_lock:
        stale = now - _ts_refreshed_at >= ttl
        start = stale and not _ts_refreshing
        if start:
            _ts_refreshing = True
        current = list(_ts_devices)
    if start:
        if wait:
            _refresh_tailscale_devices(cfg)
            with _ts_lock:
                current = list(_ts_devices)
        else:
            threading.Thread(
                target=_refresh_tailscale_devices,
                args=(cfg,),
                name="droidclaw-tailscale-refresh",
                daemon=True,
            ).start()
    return [AdbDevice(**{**d.to_dict(), "aliases": list(d.aliases or [])}) for d in current]


# ---------------------------------------------------------------------------
# Canonical device list + resolution
# ---------------------------------------------------------------------------

def _device_key(device: AdbDevice) -> str:
    if device.ip:
        return f"ip:{device.ip}"
    return f"serial:{device.serial}"


_MDNS_SERIAL = re.compile(r"^adb-(?P<hw>[A-Za-z0-9]+)-[A-Za-z0-9]+(?:\._adb|$)")


def _hardware_serial(device: AdbDevice) -> str:
    """Best-effort hardware serial: USB serial, or the one embedded in mDNS names."""
    for name in [device.serial, *(device.aliases or [])]:
        match = _MDNS_SERIAL.match(name or "")
        if match:
            return match.group("hw")
    if not device.ip and "._adb" not in device.serial:
        return device.serial
    return ""


def canonical_devices(backend: AdbBackend | None = None, wait_tailscale: bool = False) -> list[AdbDevice]:
    selected = backend or select_backend()
    devices = [d for d in list_connected_devices(selected) if d.state == "device"]
    mdns_services = list_mdns_services(selected)
    mdns_by_ip: dict[str, list[MdnsService]] = {}
    for service in mdns_services:
        if service.host:
            mdns_by_ip.setdefault(service.host, []).append(service)

    grouped: dict[str, AdbDevice] = {}
    hw_index: dict[str, str] = {}
    for device in devices:
        # mDNS auto-connected transports are named after the service
        # ("adb-<serial>-<id>._adb-tls-connect._tcp"); recover their IP so
        # they group with ip:port transports of the same phone.
        if not device.ip:
            for service in mdns_services:
                if service.name and device.serial.startswith(service.name):
                    device.ip = service.host
                    device.port = service.port
                    break
        aliases = {device.serial}
        if device.ip:
            aliases.add(device.ip)
            if device.port:
                aliases.add(f"{device.ip}:{device.port}")
        if device.ip in mdns_by_ip:
            for service in mdns_by_ip[device.ip]:
                aliases.add(service.address)
                aliases.add(service.name)
                aliases.add(f"{service.name}.{service.service_type}")

        device.aliases = sorted(aliases)
        key = _device_key(device)
        # The same phone can be attached twice (USB + wireless). Merge on the
        # hardware serial, never on model/product: identical phones must stay
        # separate devices.
        hw = _hardware_serial(device)
        if hw and hw in hw_index and hw_index[hw] in grouped:
            key = hw_index[hw]
        elif hw:
            hw_index[hw] = key
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = device
            continue

        existing_aliases = set(existing.aliases or [])
        existing_aliases.update(device.aliases or [])
        prefer_new = bool(device.ip and not existing.ip)
        if prefer_new:
            device.aliases = sorted(existing_aliases)
            grouped[key] = device
        else:
            existing.aliases = sorted(existing_aliases)

    # Merge Tailscale peers when the container node is on a tailnet. This is
    # intentionally not gated by tailscale_enabled; quick-connect should remain
    # visible and self-describing by default.
    config = _load_config()
    try:
        for ts_dev in tailscale_devices(config, wait=wait_tailscale):
            existing = None
            for dev in grouped.values():
                if dev.ip == ts_dev.ip:
                    existing = dev
                    break
            if existing:
                existing_aliases = set(existing.aliases or [])
                existing_aliases.update(ts_dev.aliases or [])
                existing.aliases = sorted(existing_aliases)
            else:
                grouped[f"tailscale:{ts_dev.ip}"] = ts_dev
    except Exception:
        pass  # Graceful degradation

    result = [device for device in grouped.values() if not _is_device_blocked(device, config)]

    try:
        from usr.plugins.droidclaw.helpers.adb_session import remember_devices

        remember_devices(result)
    except Exception:
        pass
    return result


def resolve_device(device: str | None = None, backend: AdbBackend | None = None) -> dict:
    requested = (device or "").strip()
    if requested.lower() in {"auto", "(auto)", "auto-detect"}:
        requested = ""

    selected = backend or select_backend()
    connected = list_connected_devices(selected)

    def _result(dev: AdbDevice | None, resolved: bool, reason: str, devices: list[AdbDevice] | None) -> dict:
        return {
            "requested_device": device or "",
            "resolved_device": dev.serial if dev else "",
            "device": dev.to_dict() if dev else None,
            "devices": [d.to_dict() for d in (devices or [])],
            "adb_backend": selected.name,
            "adb_backend_message": selected.message,
            "resolved": resolved,
            "reason": reason,
        }

    if requested:
        # Fast path: an exact, online transport serial needs no canonical merge.
        for raw in connected:
            if raw.serial == requested and raw.state == "device":
                return _result(raw, True, "exact", None)

        devices = canonical_devices(selected)
        req_ip, _ = _extract_ip_port(requested)
        for dev in devices:
            aliases = set(dev.aliases or [])
            if requested in aliases or (req_ip and dev.ip == req_ip):
                return _result(dev, True, "alias" if requested in aliases else "matching_ip", devices)

        return _result(None, False, "not_connected", devices)

    devices = canonical_devices(selected)
    if devices:
        return _result(devices[0], True, "auto", devices)
    return _result(None, False, "no_devices", [])


def parse_adb_command(command: str) -> list[str]:
    command = (command or "").strip()
    if not command:
        return []
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if parts and parts[0] == "adb":
        parts = parts[1:]
    if parts and parts[0] == "shell":
        shell_text = command
        if shell_text.startswith("adb "):
            shell_text = shell_text[4:].lstrip()
        if shell_text.startswith("shell "):
            shell_text = shell_text[6:].lstrip()
        return ["shell", shell_text]
    return parts


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

_DEVICE_NOT_FOUND = re.compile(r"device '[^']*' not found")


def is_transport_error(output: str) -> bool:
    text = (output or "").lower()
    if any(fragment in text for fragment in _TRANSPORT_ERRORS) or _DEVICE_NOT_FOUND.search(text):
        return True
    return is_server_error(text)


def is_server_error(output: str) -> bool:
    text = (output or "").lower()
    return any(fragment in text for fragment in _SERVER_ERRORS)


def _prepare(
    args: list[str],
    device: str | None,
    backend: AdbBackend | None,
    resolve: bool,
) -> tuple[AdbBackend, dict]:
    """Pick the backend and resolve the device (may run subprocesses)."""
    selected = backend or select_backend()
    forwarded_serial = ""
    if device:
        try:
            from usr.plugins.droidclaw.helpers.tailscale_discovery import forwarded_adb_serial
            forwarded_serial = forwarded_adb_serial(device)
        except Exception:
            forwarded_serial = ""

    if forwarded_serial:
        selected = select_backend("container")
        resolution = {
            "requested_device": device or "",
            "resolved_device": forwarded_serial,
            "resolved": True,
            "reason": "tailscale_forward",
            "device": None,
        }
    elif resolve and device is not None:
        resolution = resolve_device(device, selected)
    else:
        resolution = {
            "requested_device": device or "",
            "resolved_device": device or "",
            "resolved": bool(device) if device is not None else True,
            "reason": "raw" if device else "none",
            "device": None,
        }
    return selected, resolution


def _result_dict(
    returncode: int,
    stdout,
    stderr: str,
    selected: AdbBackend,
    cmd: list[str],
    device: str | None,
    resolution: dict,
    recovered: bool = False,
) -> dict:
    if isinstance(stdout, bytes):
        out_text = ""
    else:
        out_text = (stdout or "").strip()
        stdout = out_text
    err = (stderr or "").strip()
    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": err,
        "output": "\n".join(part for part in (out_text, err) if part).strip(),
        "backend": selected.name,
        "backend_message": selected.message,
        "cmd": cmd,
        "requested_device": device or "",
        "resolved_device": resolution.get("resolved_device") or "",
        "device_resolution": resolution,
        "recovered": recovered,
    }


def _not_connected(device: str | None, selected: AdbBackend, resolution: dict) -> dict:
    message = f"ADB device is not connected: {device}"
    return _result_dict(1, "", message, selected, [], device, resolution)


def _execute(
    args: list[str],
    resolved_device: str | None,
    timeout: float,
    input_text: str | None,
    selected: AdbBackend,
    binary: bool = False,
) -> dict:
    cmd = adb_cmd(args, device=resolved_device, backend=selected)
    try:
        proc = subprocess.run(
            cmd,
            input=input_text.encode() if (binary and input_text is not None) else input_text,
            text=not binary,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=_adb_subprocess_env(selected),
        )
    except FileNotFoundError:
        message = f"ADB client was not found at command path: {cmd[0] if cmd else 'adb'}"
        return {"returncode": 127, "stdout": b"" if binary else "", "stderr": message, "output": message, "cmd": cmd}
    except subprocess.TimeoutExpired:
        message = f"ADB command timed out after {timeout}s"
        return {"returncode": -1, "stdout": b"" if binary else "", "stderr": message, "output": message, "cmd": cmd, "timeout": True}
    stderr = proc.stderr.decode("utf-8", errors="replace") if binary else (proc.stderr or "")
    stdout = proc.stdout if binary else (proc.stdout or "")
    output = "\n".join(p.strip() for p in ((stdout if not binary else ""), stderr) if p and p.strip())
    return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr, "output": output, "cmd": cmd}


async def _execute_async(
    args: list[str],
    resolved_device: str | None,
    timeout: float,
    input_text: str | None,
    selected: AdbBackend,
    binary: bool = False,
) -> dict:
    cmd = adb_cmd(args, device=resolved_device, backend=selected)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_adb_subprocess_env(selected),
        )
    except FileNotFoundError:
        message = f"ADB client was not found at command path: {cmd[0] if cmd else 'adb'}"
        return {"returncode": 127, "stdout": b"" if binary else "", "stderr": message, "output": message, "cmd": cmd}
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_text.encode() if input_text is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        message = f"ADB command timed out after {timeout}s"
        return {"returncode": -1, "stdout": b"" if binary else "", "stderr": message, "output": message, "cmd": cmd, "timeout": True}
    err = stderr.decode("utf-8", errors="replace")
    out = stdout if binary else stdout.decode("utf-8", errors="replace")
    output = "\n".join(p.strip() for p in ((out if not binary else ""), err) if p and p.strip())
    return {"returncode": proc.returncode, "stdout": out, "stderr": err, "output": output, "cmd": cmd}


def _needs_recovery(raw: dict, device: str | None) -> bool:
    if raw.get("timeout") or raw.get("returncode") in (0, 127):
        return False
    text = raw.get("output") or raw.get("stderr") or ""
    if is_server_error(text):
        return True
    return bool(device) and is_transport_error(text)


def _recover(device: str | None, selected: AdbBackend, error_text: str) -> tuple[AdbBackend, dict | None]:
    """Try to restore the transport; returns (backend, new_resolution|None)."""
    server_down = is_server_error(error_text)
    if not server_down:
        # A dead server shows up as an empty device list, not as an error.
        server_down = not _probe(selected.command_prefix, timeout=3)[0]
    if server_down:
        invalidate_backend()
        invalidate_device_cache()
        selected = select_backend(force=True)
        if not device:
            return selected, {"resolved": True, "resolved_device": "", "reason": "server_reselected"}
    try:
        from usr.plugins.droidclaw.helpers.adb_session import recover_device

        outcome = recover_device(device or "", selected)
    except Exception as exc:
        logger.debug("ADB recovery failed for %s: %s", device, exc)
        return selected, None
    if not outcome.get("success"):
        return selected, None
    _, resolution = _prepare([], device, selected, True)
    if not resolution.get("resolved"):
        return selected, None
    resolution["reason"] = f"recovered:{outcome.get('method', '')}"
    return selected, resolution


def run_adb_result(
    args: list[str],
    device: str | None = None,
    timeout: int = 15,
    input_text: str | None = None,
    backend: AdbBackend | None = None,
    resolve: bool = True,
    binary: bool = False,
    recover: bool = True,
) -> dict:
    """Run an adb command synchronously (use from worker threads)."""
    selected, resolution = _prepare(args, device, backend, resolve)
    if resolve and device is not None and device != "" and not resolution.get("resolved"):
        if not recover:
            return _not_connected(device, selected, resolution)
        selected, recovered = _recover(device, selected, "adb device is not connected")
        if recovered is None:
            return _not_connected(device, selected, resolution)
        resolution = recovered
    elif resolve and device == "" and not resolution.get("resolved"):
        return _not_connected("auto", selected, resolution)

    resolved_device = resolution.get("resolved_device") or None
    raw = _execute(args, resolved_device, timeout, input_text, selected, binary)
    was_recovered = str(resolution.get("reason", "")).startswith("recovered")
    if recover and _needs_recovery(raw, resolved_device):
        selected, recovered = _recover(device or resolved_device, selected, raw.get("output", ""))
        if recovered is not None:
            resolution = recovered
            resolved_device = resolution.get("resolved_device") or None
            raw = _execute(args, resolved_device, timeout, input_text, selected, binary)
            was_recovered = True
    _after_command(args, raw, resolved_device)
    return _result_dict(raw["returncode"], raw["stdout"], raw["stderr"], selected, raw["cmd"], device, resolution, was_recovered)


async def run_adb_async(
    args: list[str],
    device: str | None = None,
    timeout: int = 15,
    input_text: str | None = None,
    backend: AdbBackend | None = None,
    resolve: bool = True,
    binary: bool = False,
    recover: bool = True,
) -> dict:
    """Run an adb command without blocking the event loop.

    Backend selection and device resolution may spawn subprocesses on a cold
    cache, so they run in a worker thread; the command itself is an asyncio
    subprocess that is killed if it exceeds ``timeout``.
    """
    selected, resolution = await asyncio.to_thread(_prepare, args, device, backend, resolve)
    if resolve and device is not None and device != "" and not resolution.get("resolved"):
        if not recover:
            return _not_connected(device, selected, resolution)
        selected, recovered = await asyncio.to_thread(_recover, device, selected, "adb device is not connected")
        if recovered is None:
            return _not_connected(device, selected, resolution)
        resolution = recovered
    elif resolve and device == "" and not resolution.get("resolved"):
        return _not_connected("auto", selected, resolution)

    resolved_device = resolution.get("resolved_device") or None
    raw = await _execute_async(args, resolved_device, timeout, input_text, selected, binary)
    was_recovered = str(resolution.get("reason", "")).startswith("recovered")
    if recover and _needs_recovery(raw, resolved_device):
        selected, recovered = await asyncio.to_thread(
            _recover, device or resolved_device, selected, raw.get("output", "")
        )
        if recovered is not None:
            resolution = recovered
            resolved_device = resolution.get("resolved_device") or None
            raw = await _execute_async(args, resolved_device, timeout, input_text, selected, binary)
            was_recovered = True
    _after_command(args, raw, resolved_device)
    return _result_dict(raw["returncode"], raw["stdout"], raw["stderr"], selected, raw["cmd"], device, resolution, was_recovered)


def _after_command(args: list[str], raw: dict, resolved_device: str | None) -> None:
    if raw.get("returncode") != 0:
        return
    if args and args[0] == "connect":
        _note_connect(args, raw)
    elif resolved_device:
        try:
            from usr.plugins.droidclaw.helpers.adb_session import touch

            touch(resolved_device)
        except Exception:
            pass


def _note_connect(args: list[str], raw: dict) -> None:
    """Remember endpoints the user/agent connected to, for later recovery."""
    invalidate_device_cache()
    if len(args) < 2:
        return
    text = (raw.get("output") or "").lower()
    if "connected to" not in text:
        return
    try:
        from usr.plugins.droidclaw.helpers.adb_session import remember_endpoint

        remember_endpoint(str(args[1]))
    except Exception:
        pass


def get_connected_device_serials(backend: AdbBackend | None = None) -> list[str]:
    return [device.serial for device in canonical_devices(backend)]


def diagnostics() -> dict:
    config = _load_config()
    host, port = _host_settings(config)
    adb = _adb_client(config)
    selected = select_backend()
    adb_prefix = _adb_prefix(config)
    host_ok, host_message = _probe(adb_prefix + ["-H", host, "-P", str(port)])
    container_ok, container_message = _probe(adb_prefix)
    services = ""
    try:
        services = run_adb_result(["mdns", "services"], timeout=8, backend=selected, resolve=False, recover=False)["output"]
    except Exception as exc:
        services = str(exc)

    ts_diag = {}
    if config.get("tailscale_enabled", False):
        try:
            from usr.plugins.droidclaw.helpers.tailscale_discovery import tailscale_diagnostics
            ts_diag = tailscale_diagnostics()
        except Exception:
            ts_diag = {"installed": False, "running": False}

    return {
        "mode": _backend_mode(config),
        "selected": selected.name,
        "selected_message": selected.message,
        "host": host,
        "port": str(port),
        "adb_client": adb,
        "adb_path": adb.get("path") or "",
        "adb_client_available": bool(adb.get("available")),
        "adb_client_message": adb.get("message") or "",
        "host_available": host_ok,
        "host_message": host_message,
        "container_available": container_ok,
        "container_message": container_message,
        "mdns_services": services,
        "tailscale": ts_diag,
    }
