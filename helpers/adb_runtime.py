"""Runtime bootstrap and health checks for Android Control's bundled ADB."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_DATA_DIR = _PLUGIN_DIR / "data"
_HEALTH_FILE = _DATA_DIR / "adb_health.json"
_LOCK = threading.Lock()
_BOOTSTRAPPED = False
_LAST_ATTEMPT = 0.0


def _run(cmd: list[str], timeout: int = 15) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        return {
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "output": "\n".join(part for part in (stdout, stderr) if part).strip(),
            "cmd": cmd,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": -1,
            "stdout": exc.stdout.decode("utf-8", errors="replace").strip() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            "stderr": "ADB command timed out",
            "output": "ADB command timed out",
            "cmd": cmd,
        }
    except Exception as exc:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
            "output": str(exc),
            "cmd": cmd,
        }


def _device_lines(output: str) -> list[str]:
    return [
        line.strip()
        for line in (output or "").splitlines()
        if line.strip() and not line.startswith("List of devices")
    ]


def _write_health(state: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _HEALTH_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(_HEALTH_FILE)


def read_adb_health() -> dict:
    try:
        return json.loads(_HEALTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def bridge_capabilities(force: bool = False) -> dict:
    """Return QR/USB readiness for the current host/container bridge state."""
    if force:
        health = bootstrap_adb_runtime(force=True)
    else:
        health = read_adb_health() or bootstrap_adb_runtime(force=False)

    try:
        from usr.plugins.droidclaw.helpers.adb_backend import diagnostics

        diag = diagnostics()
    except Exception as exc:
        diag = {"error": str(exc)}

    container = health.get("container") or {}
    host_available = bool(diag.get("host_available"))
    container_available = bool(diag.get("container_available") or container.get("daemon_running"))
    selected = diag.get("selected") or ""
    mdns_output = diag.get("mdns_services") or (container.get("mdns_services") or {}).get("output") or ""
    container_mdns_visible = bool(container.get("mdns_visible") or "_adb" in mdns_output)
    usb_visible = bool(container.get("usb_or_device_visible"))

    host_devices_visible = False
    if host_available:
        adb = diag.get("adb_client") or {}
        adb_path = adb.get("path") or ""
        host = diag.get("host") or "host.docker.internal"
        port = str(diag.get("port") or "5037")
        if adb_path:
            host_devices = _run([adb_path, "-H", host, "-P", port, "devices", "-l"], timeout=8)
            host_devices_visible = any(
                "\tdevice" in line or " device" in line
                for line in _device_lines(host_devices.get("output", ""))
            )
        else:
            host_devices = {}
    else:
        host_devices = {}

    qr_ready = bool(host_available or container_mdns_visible)
    usb_ready = bool(host_available or usb_visible or host_devices_visible)
    requirements_ready = bool(qr_ready and usb_ready)

    qr_message = (
        "Wireless ADB QR is ready through the host ADB bridge."
        if host_available
        else "Wireless ADB QR needs a backend that can see Android mDNS services. Enable the host ADB bridge or run A0 with LAN mDNS visibility."
    )
    if container_mdns_visible and not host_available:
        qr_message = "Wireless ADB QR may work through container-local mDNS visibility."

    usb_message = (
        "USB detection is ready through the host ADB bridge."
        if host_available
        else "USB detection needs the host ADB bridge or Docker USB passthrough into the A0 container."
    )
    if usb_visible and not host_available:
        usb_message = "USB detection is ready through container USB visibility."

    banner = ""
    if not requirements_ready:
        banner = (
            "Android Control installed its bundled ADB client, but QR pairing and wired USB "
            "need device visibility from A0. Enable the host ADB bridge for Windows Docker, "
            "or use container networking/USB passthrough on systems that support it."
        )

    return {
        "qr_ready": qr_ready,
        "usb_ready": usb_ready,
        "requirements_ready": requirements_ready,
        "host_bridge_available": host_available,
        "container_adb_available": container_available,
        "mdns_visible": bool(host_available or container_mdns_visible),
        "container_mdns_visible": container_mdns_visible,
        "usb_visible": bool(usb_visible or host_devices_visible),
        "host_usb_visible": host_devices_visible,
        "selected_backend": selected,
        "qr_message": qr_message,
        "usb_message": usb_message,
        "banner": banner,
        "diagnostics": diag,
        "health": health,
        "host_devices": host_devices,
    }


def bootstrap_adb_runtime(force: bool = False) -> dict:
    """Ensure plugin deps exist and the container-local ADB daemon is running."""
    global _BOOTSTRAPPED, _LAST_ATTEMPT

    now = time.time()
    if _BOOTSTRAPPED and not force:
        return read_adb_health()
    if not force and now - _LAST_ATTEMPT < 30:
        return read_adb_health()

    with _LOCK:
        now = time.time()
        if _BOOTSTRAPPED and not force:
            return read_adb_health()
        if not force and now - _LAST_ATTEMPT < 30:
            return read_adb_health()
        _LAST_ATTEMPT = now

        state: dict = {
            "success": False,
            "timestamp": now,
            "adb_client": {},
            "container": {},
            "backend": {},
            "message": "",
        }

        try:
            from usr.plugins.droidclaw.helpers.dependencies import ensure_runtime_dependencies

            state["dependencies"] = ensure_runtime_dependencies(include_adb=True)
        except Exception as exc:
            state["message"] = f"Dependency bootstrap failed: {exc}"
            _write_health(state)
            return state

        from usr.plugins.droidclaw.helpers.platform_tools import find_adb

        adb = find_adb()
        state["adb_client"] = adb
        if not adb.get("available") or not adb.get("path"):
            state["message"] = adb.get("message") or "ADB client is unavailable"
            _write_health(state)
            return state

        prefix = [adb["path"]]
        start = _run(prefix + ["start-server"], timeout=15)
        devices = _run(prefix + ["devices", "-l"], timeout=10)
        mdns_check = _run(prefix + ["mdns", "check"], timeout=10)
        mdns_services = _run(prefix + ["mdns", "services"], timeout=10)
        device_lines = _device_lines(devices.get("output", ""))

        state["container"] = {
            "start_server": start,
            "devices": devices,
            "mdns_check": mdns_check,
            "mdns_services": mdns_services,
            "daemon_running": start["returncode"] == 0 or mdns_check["returncode"] == 0,
            "usb_or_device_visible": any("\tdevice" in line or " device" in line for line in device_lines),
            "mdns_visible": "_adb" in (mdns_services.get("output") or ""),
        }

        try:
            from usr.plugins.droidclaw.helpers.adb_backend import diagnostics

            state["backend"] = diagnostics()
        except Exception as exc:
            state["backend_error"] = str(exc)

        state["success"] = bool(state["container"].get("daemon_running"))
        state["message"] = (
            "Container-local ADB is running"
            if state["success"]
            else "Container-local ADB did not start; see command diagnostics"
        )
        _BOOTSTRAPPED = state["success"]
        _write_health(state)
        return state
