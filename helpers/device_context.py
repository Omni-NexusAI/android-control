"""Per-agent device targeting and device leases for multi-device control.

Rules every Android Control tool follows to pick a phone:

1. an explicit ``device`` argument;
2. the device this agent bound itself to (``adb_device`` action ``select``);
3. the ``device`` setting from the plugin config (resolved per agent/project);
4. the only connected device.

If several phones are connected and none of the above applies, the tool fails
with the list of devices instead of silently driving whichever phone ADB
listed first; with parallel subordinates that silent default made two agents
fight over the same phone.

Leases stop two agents from driving the same phone at the same time. They are
renewed on every action and expire after ``LEASE_TTL`` seconds of inactivity,
so a crashed or finished agent never blocks a device for long. Panel/API
actions (no agent) are never blocked.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass

from usr.plugins.droidclaw.helpers.adb_backend import canonical_devices, resolve_device

AGENT_DEVICE_KEY = "droidclaw_device"
LEASE_TTL = 90.0
LEASE_WAIT = 10.0
SCREENSHOT_DIR = "/a0/tmp/droidclaw_screenshots"
SCREENSHOT_MAX_AGE = 3600
SCREENSHOT_MAX_FILES = 300


class DeviceSelectionError(RuntimeError):
    pass


def _is_auto(value) -> bool:
    return str(value or "").strip().lower() in {"", "auto", "(auto)", "auto-detect"}


def bound_device(agent) -> str:
    if agent is None:
        return ""
    try:
        return str(agent.get_data(AGENT_DEVICE_KEY) or "")
    except Exception:
        return str(getattr(agent, "data", {}).get(AGENT_DEVICE_KEY, "") or "")


def bind_device(agent, serial: str) -> None:
    if agent is None:
        return
    try:
        agent.set_data(AGENT_DEVICE_KEY, serial)
    except Exception:
        agent.data[AGENT_DEVICE_KEY] = serial


def _config_device(agent) -> str:
    try:
        from helpers import plugins

        cfg = plugins.get_plugin_config("droidclaw", agent=agent) or {}
    except Exception:
        cfg = {}
    return str(cfg.get("device") or "")


def describe_devices(devices) -> str:
    rows = []
    for dev in devices:
        label = dev.model or dev.product or ""
        rows.append(f"- {dev.serial}" + (f" ({label})" if label else ""))
    return "\n".join(rows)


def _resolve_target(agent, explicit) -> tuple[str, str]:
    """Return (serial, source). Raises DeviceSelectionError."""
    candidates = []
    if not _is_auto(explicit):
        candidates.append((str(explicit).strip(), "argument"))
    else:
        bound = bound_device(agent)
        if bound:
            candidates.append((bound, "bound"))
        else:
            configured = _config_device(agent)
            if configured and not _is_auto(configured):
                candidates.append((configured, "config"))

    for requested, source in candidates:
        resolution = resolve_device(requested)
        if resolution.get("resolved_device"):
            return resolution["resolved_device"], source
        devices = canonical_devices()
        hint = describe_devices(devices) or "(none)"
        raise DeviceSelectionError(
            f"Android device '{requested}' ({source}) is not connected and could not be reconnected.\n"
            f"Connected devices:\n{hint}"
        )

    devices = canonical_devices()
    if len(devices) == 1:
        return devices[0].serial, "only"
    if not devices:
        raise DeviceSelectionError(
            "No Android device is connected. Connect or pair a phone first (adb_device action connect/pair)."
        )
    raise DeviceSelectionError(
        "Multiple Android devices are connected; choose one. Pass \"device\" with one of the serials below, "
        "or bind this agent once with adb_device action \"select\".\n" + describe_devices(devices)
    )


async def target_device(agent, explicit=None) -> str:
    """Resolve which phone this agent should act on (see module docstring)."""
    serial, _ = await asyncio.to_thread(_resolve_target, agent, explicit)
    return serial


def target_device_sync(agent, explicit=None) -> str:
    return _resolve_target(agent, explicit)[0]


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------

@dataclass
class Lease:
    holder: str
    label: str
    expires: float


_lease_lock = threading.Lock()
_leases: dict[str, Lease] = {}


def holder_id(agent) -> str:
    if agent is None:
        return ""
    ctx = getattr(agent, "context", None)
    ctx_id = getattr(ctx, "id", "") or ""
    return f"{ctx_id}:{getattr(agent, 'agent_name', '') or id(agent)}"


def holder_label(agent) -> str:
    if agent is None:
        return "panel"
    ctx = getattr(agent, "context", None)
    name = getattr(agent, "agent_name", "") or "agent"
    ctx_id = getattr(ctx, "id", "") or ""
    return f"{name} (chat {ctx_id})" if ctx_id else name


def _try_acquire(serial: str, holder: str, label: str, ttl: float) -> Lease | None:
    """Acquire/renew; returns the blocking lease if someone else holds it."""
    now = time.monotonic()
    with _lease_lock:
        current = _leases.get(serial)
        if current and current.holder != holder and current.expires > now:
            return current
        _leases[serial] = Lease(holder=holder, label=label, expires=now + ttl)
        return None


async def acquire_device(agent, serial: str, ttl: float = LEASE_TTL, wait: float = LEASE_WAIT) -> None:
    """Claim ``serial`` for ``agent``; raises DeviceSelectionError if busy."""
    holder = holder_id(agent)
    if not holder or not serial:
        return
    label = holder_label(agent)
    deadline = time.monotonic() + wait
    while True:
        blocking = _try_acquire(serial, holder, label, ttl)
        if blocking is None:
            return
        if time.monotonic() >= deadline:
            remaining = max(0, int(blocking.expires - time.monotonic()))
            raise DeviceSelectionError(
                f"Android device {serial} is being controlled by {blocking.label}. "
                f"Use a different device, or wait (its claim lapses {remaining}s after its last action)."
            )
        await asyncio.sleep(0.5)


def release_device(agent, serial: str | None = None) -> list[str]:
    holder = holder_id(agent)
    released = []
    with _lease_lock:
        for key, lease in list(_leases.items()):
            if lease.holder == holder and (serial is None or key == serial):
                released.append(key)
                del _leases[key]
    return released


def lease_table() -> dict[str, dict]:
    now = time.monotonic()
    with _lease_lock:
        return {
            serial: {"holder": lease.label, "expires_in": round(lease.expires - now, 1)}
            for serial, lease in _leases.items()
            if lease.expires > now
        }


async def claim(agent, explicit=None) -> str:
    """target_device + acquire_device in one step (what tools call)."""
    serial = await target_device(agent, explicit)
    await acquire_device(agent, serial)
    return serial


# ---------------------------------------------------------------------------
# Collision-free local files
# ---------------------------------------------------------------------------

def safe_serial(serial: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", serial or "device")[:64]


def screenshot_path(serial: str, prefix: str = "screen", ext: str = "png") -> str:
    """Unique per-device path; prunes old screenshots opportunistically."""
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    _prune(SCREENSHOT_DIR)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(
        SCREENSHOT_DIR, f"{prefix}_{safe_serial(serial)}_{stamp}_{uuid.uuid4().hex[:6]}.{ext}"
    )


_last_prune = 0.0


def _prune(directory: str) -> None:
    global _last_prune
    now = time.time()
    if now - _last_prune < 60:
        return
    _last_prune = now
    try:
        entries = []
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                entries.append((os.path.getmtime(path), path))
            except OSError:
                continue
        entries.sort(reverse=True)
        for index, (mtime, path) in enumerate(entries):
            if index >= SCREENSHOT_MAX_FILES or now - mtime > SCREENSHOT_MAX_AGE:
                try:
                    os.remove(path)
                except OSError:
                    pass
    except OSError:
        pass


def remote_temp_path(name: str, ext: str) -> str:
    """Unique on-device temp path so concurrent dumps never clobber each other."""
    return f"/data/local/tmp/droidclaw_{name}_{uuid.uuid4().hex[:8]}.{ext}"
