"""Connection memory, transport recovery and keepalive for Android Control.

Wireless ADB transports drop for mundane reasons: the phone's Wi-Fi radio
sleeps, Android rotates the Wireless debugging port, or the ADB server marks a
transport ``offline`` for a moment. Previously every drop surfaced to the agent
as "device not connected" and cost it several tool calls to reconnect.

This module remembers every endpoint a device was reachable at and, when a
command fails with a transport error, reconnects (preferring the live mDNS port
for the same phone) before the command is retried once by ``adb_backend``.
A light keepalive pings wireless devices that are actively in use so their
transport does not idle out between agent steps.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from usr.plugins.droidclaw.helpers.adb_backend import (
    AdbBackend,
    AdbDevice,
    _execute,
    _extract_ip_port,
    _load_config,
    invalidate_device_cache,
    list_connected_devices,
    list_mdns_services,
    select_backend,
)

logger = logging.getLogger("droidclaw")

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_STATE_FILE = _PLUGIN_DIR / "data" / "known_devices.json"

RECOVERY_COOLDOWN = 4.0
RECOVERY_WAIT = 4.0
KEEPALIVE_IDLE_WINDOW = 600.0

_lock = threading.RLock()
_records: dict[str, dict] | None = None
_dirty = False
_last_saved = 0.0
_recovery_locks: dict[str, threading.Lock] = {}
_recent_recovery: dict[str, tuple[float, dict]] = {}
_activity: dict[str, float] = {}
_keepalive_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load() -> dict[str, dict]:
    global _records
    if _records is None:
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            _records = {r["key"]: r for r in data.get("devices", []) if isinstance(r, dict) and r.get("key")}
        except Exception:
            _records = {}
    return _records


def _save(force: bool = False) -> None:
    global _dirty, _last_saved
    if not _dirty and not force:
        return
    now = time.time()
    if not force and now - _last_saved < 10:
        return
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"devices": list(_load().values())}, indent=2), encoding="utf-8")
        tmp.replace(_STATE_FILE)
        _dirty = False
        _last_saved = now
    except OSError as exc:
        logger.debug("Could not persist known devices: %s", exc)


def _key_for(ip: str, serial: str) -> str:
    return f"ip:{ip}" if ip else f"serial:{serial}"


def _upsert(key: str, *, ip: str = "", endpoint: str = "", aliases=(), model: str = "") -> None:
    global _dirty
    records = _load()
    record = records.setdefault(key, {"key": key, "ip": ip, "endpoints": [], "aliases": [], "model": ""})
    changed = False
    if ip and record.get("ip") != ip:
        record["ip"] = ip
        changed = True
    if endpoint:
        endpoints = [e for e in record.get("endpoints", []) if e != endpoint]
        if not record.get("endpoints") or record["endpoints"][0] != endpoint:
            changed = True
        record["endpoints"] = ([endpoint] + endpoints)[:5]
    alias_set = set(record.get("aliases", []))
    new_aliases = alias_set | {a for a in aliases if a}
    if new_aliases != alias_set:
        record["aliases"] = sorted(new_aliases)[:40]
        changed = True
    if model and record.get("model") != model:
        record["model"] = model
        changed = True
    if changed:
        record["last_seen"] = time.time()
        _dirty = True


def remember_devices(devices: list[AdbDevice]) -> None:
    """Record every endpoint/alias seen for the currently connected devices."""
    with _lock:
        for dev in devices:
            if not dev.wireless:
                continue
            endpoint = f"{dev.ip}:{dev.port}" if dev.ip and dev.port else ""
            _upsert(
                _key_for(dev.ip, dev.serial),
                ip=dev.ip,
                endpoint=endpoint,
                aliases=[dev.serial, *(dev.aliases or [])],
                model=dev.model,
            )
        _save()


def remember_endpoint(endpoint: str) -> None:
    ip, port = _extract_ip_port(endpoint)
    if not ip:
        return
    with _lock:
        _upsert(_key_for(ip, endpoint), ip=ip, endpoint=f"{ip}:{port}" if port else "", aliases=[endpoint])
        _save(force=True)


def known_devices() -> list[dict]:
    with _lock:
        return [dict(r) for r in _load().values()]


def _find_record(requested: str) -> dict | None:
    req_ip, _ = _extract_ip_port(requested)
    with _lock:
        for record in _load().values():
            if requested == record.get("key") or requested in record.get("aliases", []):
                return dict(record)
            if req_ip and record.get("ip") == req_ip:
                return dict(record)
    return None


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def _online(requested: str, backend: AdbBackend, record: dict | None) -> AdbDevice | None:
    req_ip, _ = _extract_ip_port(requested)
    aliases = set(record.get("aliases", [])) if record else set()
    ip = req_ip or (record or {}).get("ip", "")
    for dev in list_connected_devices(backend, max_age=0):
        if dev.state != "device":
            continue
        if dev.serial == requested or dev.serial in aliases:
            return dev
        if ip and dev.ip == ip:
            return dev
    return None


def _candidate_endpoints(requested: str, backend: AdbBackend, record: dict | None) -> list[str]:
    candidates: list[str] = []
    ip = (record or {}).get("ip") or _extract_ip_port(requested)[0]
    names = {a.split("._adb")[0] for a in (record or {}).get("aliases", []) if "._adb" in a or a.startswith("adb-")}
    # The live Wireless debugging port (mDNS) beats anything remembered, since
    # Android picks a new port every time Wireless debugging restarts.
    for service in list_mdns_services(backend, max_age=0):
        if "connect" not in service.service_type and "_adb._tcp" not in service.service_type:
            continue
        if (ip and service.host == ip) or (service.name and service.name in names):
            candidates.append(service.address)
    if ":" in requested and _extract_ip_port(requested)[1]:
        candidates.append(requested)
    candidates.extend((record or {}).get("endpoints", []))
    seen: set[str] = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


def _wait_online(requested: str, backend: AdbBackend, record: dict | None, timeout: float) -> AdbDevice | None:
    deadline = time.monotonic() + timeout
    while True:
        dev = _online(requested, backend, record)
        if dev or time.monotonic() >= deadline:
            return dev
        time.sleep(0.4)


def recover_device(requested: str, backend: AdbBackend | None = None) -> dict:
    """Try to bring ``requested`` back online. Thread-safe, rate-limited."""
    requested = (requested or "").strip()
    backend = backend or select_backend()
    key = requested or "(auto)"
    with _lock:
        lock = _recovery_locks.setdefault(key, threading.Lock())
    with lock:
        recent = _recent_recovery.get(key)
        if recent and time.monotonic() - recent[0] < RECOVERY_COOLDOWN:
            # Another caller just recovered (or failed to): reuse that outcome
            # if the device is still in the same state.
            if recent[1].get("success") and _online(requested, backend, _find_record(requested)):
                return recent[1]
            if not recent[1].get("success"):
                return recent[1]
        outcome = _recover(requested, backend)
        _recent_recovery[key] = (time.monotonic(), outcome)
        level = logging.INFO if outcome.get("success") else logging.WARNING
        logger.log(level, "Android Control ADB recovery for %s: %s", key, outcome.get("message"))
        return outcome


def _recover(requested: str, backend: AdbBackend) -> dict:
    invalidate_device_cache()
    record = _find_record(requested) if requested else None

    # 1. Transient: the transport may already be back (or was "connecting").
    dev = _wait_online(requested, backend, record, 1.0) if requested else None
    if dev:
        return {"success": True, "method": "transient", "serial": dev.serial, "message": f"{dev.serial} is online"}

    if not requested:
        devices = [d for d in list_connected_devices(backend, max_age=0) if d.state == "device"]
        if devices:
            return {"success": True, "method": "transient", "serial": devices[0].serial, "message": "device available"}

    # 2. Offline transports: ask the server to reset them.
    raw_states = {d.serial: d.state for d in list_connected_devices(backend, max_age=0)}
    if any(state == "offline" for state in raw_states.values()):
        _execute(["reconnect", "offline"], None, 8, None, backend)
        invalidate_device_cache()
        dev = _wait_online(requested, backend, record, 2.5) if requested else None
        if dev:
            return {"success": True, "method": "reconnect_offline", "serial": dev.serial, "message": f"Reset offline transport for {dev.serial}"}

    # 3. Wireless: reconnect to the best known endpoint.
    attempted: list[str] = []
    for endpoint in _candidate_endpoints(requested, backend, record) if requested else []:
        attempted.append(endpoint)
        stale = [s for s, st in raw_states.items() if st in ("offline", "unauthorized") and _extract_ip_port(s)[0] == _extract_ip_port(endpoint)[0]]
        for serial in stale:
            _execute(["disconnect", serial], None, 5, None, backend)
        result = _execute(["connect", endpoint], None, 8, None, backend)
        text = (result.get("output") or "").lower()
        if "connected to" in text or "already connected" in text:
            invalidate_device_cache()
            dev = _wait_online(requested, backend, record, RECOVERY_WAIT)
            if not dev:
                dev = _wait_online(endpoint, backend, record, 1.0)
            if dev:
                remember_endpoint(endpoint)
                return {
                    "success": True,
                    "method": "reconnect",
                    "serial": dev.serial,
                    "endpoint": endpoint,
                    "message": f"Reconnected {requested} via {endpoint}",
                }

    # 4. USB re-enumeration (cable wiggle, adb restart on the host).
    if requested and not _extract_ip_port(requested)[0]:
        dev = _wait_online(requested, backend, record, RECOVERY_WAIT)
        if dev:
            return {"success": True, "method": "usb_wait", "serial": dev.serial, "message": f"{dev.serial} re-appeared"}

    detail = f" (tried {', '.join(attempted)})" if attempted else ""
    return {"success": False, "method": "none", "message": f"Could not restore ADB transport for {requested or 'auto'}{detail}"}


# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------

def touch(serial: str) -> None:
    """Mark a device as actively used (keeps the keepalive pinging it)."""
    if not serial:
        return
    with _lock:
        _activity[serial] = time.monotonic()
    _ensure_keepalive()


def _keepalive_interval() -> float:
    cfg = _load_config()
    if not cfg.get("adb_keepalive", True):
        return 0.0
    try:
        return max(5.0, float(cfg.get("adb_keepalive_interval", 20) or 20))
    except (TypeError, ValueError):
        return 20.0


def _keepalive_loop() -> None:
    while True:
        interval = _keepalive_interval() or 30.0
        time.sleep(interval)
        if not _keepalive_interval():
            continue
        now = time.monotonic()
        with _lock:
            active = [s for s, t in _activity.items() if now - t < KEEPALIVE_IDLE_WINDOW]
            for serial in [s for s, t in _activity.items() if now - t >= KEEPALIVE_IDLE_WINDOW]:
                _activity.pop(serial, None)
        if not active:
            continue
        try:
            backend = select_backend()
        except Exception:
            continue
        for serial in active:
            if not (_extract_ip_port(serial)[0] or "._adb" in serial):
                continue  # USB transports don't idle out.
            result = _execute(["shell", "echo", "1"], serial, 6, None, backend)
            if result.get("returncode") != 0:
                logger.info("Android Control keepalive: %s unresponsive, recovering", serial)
                try:
                    recover_device(serial, backend)
                except Exception:
                    pass


def _ensure_keepalive() -> None:
    global _keepalive_thread
    if _keepalive_thread is not None and _keepalive_thread.is_alive():
        return
    with _lock:
        if _keepalive_thread is not None and _keepalive_thread.is_alive():
            return
        _keepalive_thread = threading.Thread(target=_keepalive_loop, name="droidclaw-adb-keepalive", daemon=True)
        _keepalive_thread.start()
