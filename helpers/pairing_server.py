"""Android Control ADB QR pairing workflow.

Android's QR pairing flow is host-driven after scan:
1. The host generates a WIFI:T:ADB QR string with a requested service name.
2. The phone scans it and publishes that service as _adb-tls-pairing._tcp.
3. The host discovers the phone service with adb mDNS and runs adb pair.
4. The host discovers the phone connect service and runs adb connect.
"""

from __future__ import annotations

import logging
import random
import socket
import string
import threading
import time
from dataclasses import dataclass
from typing import Optional

from usr.plugins.droidclaw.helpers.adb_backend import (
    AdbBackend,
    diagnostics as adb_diagnostics,
    run_adb_result,
    select_backend,
)

logger = logging.getLogger("droidclaw")

PAIRING_SERVICE_TYPE = "_adb-tls-pairing._tcp"
CONNECT_SERVICE_TYPES = ("_adb-tls-connect._tcp", "_adb._tcp")


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


def generate_service_name() -> str:
    """Generate an Android Studio-style QR pairing service name."""
    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    return f"studio-{suffix}"


def generate_password() -> str:
    """Generate a QR pairing password safe for the WIFI:T:ADB payload."""
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=12))


def generate_qr_content(service_name: str, password: str) -> str:
    return f"WIFI:T:ADB;S:{service_name};P:{password};;"


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
        if not name or not service_type.startswith("_adb"):
            continue

        services.append(MdnsService(name=name, service_type=service_type, address=address))
    return services


def check_dependencies() -> dict:
    diag = adb_diagnostics()
    preflight = qr_backend_preflight(diag)
    available = preflight["available"]
    selected_message = diag.get("selected_message") or ""
    adb_client = diag.get("adb_client") or {}
    missing = []
    install_command = ""
    if not adb_client.get("available"):
        missing.append("adb client")
        install_command = "Install Android platform-tools or let Android Control download plugin-owned platform-tools"
    elif not available:
        missing.append("adb mdns")
        install_command = "Start/restart an ADB server with mDNS support"
    return {
        "available": available,
        "missing": missing,
        "install_command": install_command,
        "adb_backend": diag,
        "qr_preflight": preflight,
        "message": selected_message,
    }


def qr_backend_preflight(diag: Optional[dict] = None) -> dict:
    """Return whether QR pairing has a backend likely to see Wireless ADB mDNS."""
    diag = diag or adb_diagnostics()
    adb_client = diag.get("adb_client") or {}
    selected = diag.get("selected") or ""
    selected_message = diag.get("selected_message") or ""
    mdns_output = diag.get("mdns_services") or ""
    host_available = bool(diag.get("host_available"))
    container_available = bool(diag.get("container_available"))
    if not adb_client.get("available"):
        return {
            "available": False,
            "selected": selected,
            "selected_message": selected_message,
            "host_available": False,
            "host_message": "",
            "container_available": False,
            "container_message": "",
            "selected_has_adb_services": False,
            "mdns_services": mdns_output,
            "adb_client": adb_client,
            "message": adb_client.get("message") or "ADB client is not available.",
        }

    selected_available = (
        (selected == "host" and host_available)
        or (selected == "container" and container_available)
    )
    selected_has_adb_services = "_adb" in mdns_output

    available = selected_available
    message = selected_message or "ADB backend is available"
    if not selected_available:
        message = "ADB mDNS is not available from the selected Android Control backend."
    elif selected == "container" and not selected_has_adb_services:
        message = (
            "Container-local ADB is running. No Wireless ADB mDNS services are visible yet; "
            "Android Control will wait for the phone to publish its pairing service after QR scan."
        )

    return {
        "available": available,
        "selected": selected,
        "selected_message": selected_message,
        "host_available": host_available,
        "host_message": diag.get("host_message") or "",
        "container_available": container_available,
        "container_message": diag.get("container_message") or "",
        "selected_has_adb_services": selected_has_adb_services,
        "mdns_services": mdns_output,
        "adb_client": adb_client,
        "message": message,
    }


class PairingServer:
    """Stateful QR pairing session manager used by Android Control's API handler."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._running = False
        self._phase = "idle"
        self._result: Optional[str] = None
        self._message = ""
        self._service_name = ""
        self._password = ""
        self._qr_content = ""
        self._pairing_addr = ""
        self._connect_addr = ""
        self._device_serial = ""
        self._start_time = 0.0
        self._timeout = 90.0
        self._last_services: list[dict] = []
        self._last_mdns_output = ""
        self._last_adb_output = ""
        self._diagnostics: dict = {}
        self._backend: Optional[AdbBackend] = None
        self._backend_name = ""
        self._backend_message = ""

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def password(self) -> str:
        return self._password

    @property
    def pairing_result(self) -> Optional[str]:
        return self._result

    def start(self, timeout: float = 90.0) -> dict:
        with self._lock:
            if self._running:
                return {
                    "success": False,
                    "message": "QR pairing is already running",
                    **self.get_status(),
                }

            deps = check_dependencies()
            if not deps["available"]:
                preflight = deps.get("qr_preflight") or {}
                return {
                    "success": False,
                    "message": preflight.get("message") or "ADB mDNS is not available from any Android Control backend",
                    "requires_install": "adb mdns",
                    "dependency_status": deps,
                    "adb_backend": preflight,
                }

            self._backend = select_backend()
            self._backend_name = self._backend.name
            self._backend_message = self._backend.message
            self._diagnostics = deps.get("qr_preflight") or {}
            self._cancel.clear()
            self._running = True
            self._phase = "waiting_for_scan"
            self._result = None
            self._message = "Scan the QR code; waiting for the phone's Wireless ADB pairing service."
            self._service_name = generate_service_name()
            self._password = generate_password()
            self._qr_content = generate_qr_content(self._service_name, self._password)
            self._pairing_addr = ""
            self._connect_addr = ""
            self._device_serial = ""
            self._start_time = time.time()
            self._timeout = timeout
            self._last_services = []
            self._last_mdns_output = self._diagnostics.get("mdns_services") or ""
            self._last_adb_output = ""

            self._thread = threading.Thread(target=self._run_workflow, daemon=True)
            self._thread.start()

            logger.info("Android Control QR session started for %s", self._service_name)
            return {
                "success": True,
                "service_name": self._service_name,
                "password": self._password,
                "qr_content": self._qr_content,
                "timeout": self._timeout,
                "phase": self._phase,
                "message": self._message,
                "adb_backend": self._backend_name,
                "adb_backend_message": self._backend_message,
                "qr_adb_mode": "raw",
                "diagnostics": self._diagnostics,
            }

    def stop(self) -> dict:
        self._cancel.set()
        with self._lock:
            was_running = self._running
            self._running = False
            if was_running and self._phase not in {"connected", "error", "timeout"}:
                self._phase = "cancelled"
                self._result = "cancelled"
                self._message = "QR pairing cancelled"
        return {"success": True, "message": self._message or "QR pairing stopped", "result": self._result}

    def get_status(self) -> dict:
        with self._lock:
            elapsed = round(time.time() - self._start_time, 1) if self._start_time else 0
            remaining = max(0, round(self._timeout - elapsed, 1)) if self._running else 0
            return {
                "running": self._running,
                "phase": self._phase,
                "result": self._result,
                "message": self._message,
                "service_name": self._service_name,
                "pairing_addr": self._pairing_addr,
                "connect_addr": self._connect_addr,
                "device_serial": self._device_serial,
                "elapsed": elapsed,
                "remaining": remaining,
                "timeout": self._timeout,
                "services": self._last_services,
                "last_mdns_output": self._last_mdns_output,
                "last_adb_output": self._last_adb_output,
                "adb_backend": self._backend_name,
                "adb_backend_message": self._backend_message,
                "qr_adb_mode": "raw",
                "diagnostics": self._diagnostics,
            }

    def _run_workflow(self):
        try:
            pairing_service = self._wait_for_pairing_service()
            if not pairing_service:
                self._finish(
                    "timeout",
                    "timeout",
                    (
                        "Phone Wireless ADB pairing service was not discovered. "
                        f"Android Control is using the {self._backend_name or 'selected'} ADB backend. "
                        "The A0 container did not see the phone's Wireless ADB mDNS service. "
                        "Check that the phone and A0 container are on a network path where mDNS multicast is visible. "
                        f"Last mDNS output: {self._last_mdns_output or 'none'}"
                    ),
                )
                return

            self._set_phase(
                "pairing",
                f"Found phone pairing service at {pairing_service.address}; running adb pair.",
                pairing_addr=pairing_service.address,
            )

            pair = _adb_pair(pairing_service.address, self._password, self._backend)
            if not _adb_success(pair["output"], ("successfully paired", "already paired", "paired")):
                self._finish(
                    "error",
                    "error",
                    f"ADB pair failed: {pair['output'] or 'no output'}",
                )
                return

            self._set_phase("connecting", "Pairing accepted; waiting for phone connect service.")
            connect_service = self._wait_for_connect_service(pairing_service.host)
            if connect_service:
                candidates = [connect_service.address]
            else:
                candidates = [f"{pairing_service.host}:5555"]
                self._set_phase(
                    "connecting",
                    "Connect service not discovered yet; trying fallback port 5555.",
                )

            connect_result = None
            for addr in candidates:
                if self._cancel.is_set():
                    self.stop()
                    return
                self._set_phase("connecting", f"Connecting to {addr}.", connect_addr=addr)
                connect_result = _run_adb(["connect", addr], timeout=20, backend=self._backend)
                if _adb_success(connect_result["output"], ("connected", "already connected")):
                    self._finish_connected(addr, connect_result["output"])
                    return

            self._finish(
                "error",
                "error",
                f"ADB connect failed: {(connect_result or {}).get('output') or 'connect service not found'}",
            )
        except Exception as e:
            logger.exception("Android Control QR pairing workflow failed")
            self._finish("error", "error", f"QR pairing failed: {e}")

    def _wait_for_pairing_service(self) -> Optional[MdnsService]:
        deadline = self._start_time + self._timeout
        while time.time() < deadline and not self._cancel.is_set():
            services = _list_mdns_services()
            self._record_services(services)
            for service in services:
                if (
                    service.name == self._service_name
                    and service.service_type == PAIRING_SERVICE_TYPE
                ):
                    return service
            time.sleep(1.0)
        return None

    def _wait_for_connect_service(self, preferred_host: str) -> Optional[MdnsService]:
        deadline = min(time.time() + 35.0, self._start_time + self._timeout)
        fallback: Optional[MdnsService] = None
        while time.time() < deadline and not self._cancel.is_set():
            services = _list_mdns_services()
            self._record_services(services)
            for service in services:
                if service.service_type not in CONNECT_SERVICE_TYPES:
                    continue
                if service.host == preferred_host:
                    return service
                if fallback is None:
                    fallback = service
            time.sleep(1.0)
        return fallback

    def _record_services(self, services: list[MdnsService]):
        with self._lock:
            self._last_services = [
                {
                    "name": service.name,
                    "service_type": service.service_type,
                    "address": service.address,
                }
                for service in services
            ]

    def _record_adb_result(self, result: dict, args: list[str]):
        output = result.get("output") or ""
        with self._lock:
            self._last_adb_output = output
            if len(args) >= 2 and args[0] == "mdns" and args[1] == "services":
                self._last_mdns_output = output

    def _record_mdns_output(self, output: str):
        with self._lock:
            self._last_mdns_output = output

    def _set_phase(self, phase: str, message: str, **fields):
        with self._lock:
            self._phase = phase
            self._message = message
            if "pairing_addr" in fields:
                self._pairing_addr = fields["pairing_addr"]
            if "connect_addr" in fields:
                self._connect_addr = fields["connect_addr"]

    def _finish_connected(self, addr: str, output: str):
        serial = _first_connected_device() or addr
        with self._lock:
            self._running = False
            self._phase = "connected"
            self._result = "connected"
            self._connect_addr = addr
            self._device_serial = serial
            self._message = f"Connected to {serial}: {output}".strip()

    def _finish(self, phase: str, result: str, message: str):
        with self._lock:
            self._running = False
            self._phase = phase
            self._result = result
            self._message = message


def _run_adb(
    args: list[str],
    timeout: int = 15,
    input_text: str | None = None,
    backend: AdbBackend | None = None,
) -> dict:
    result = run_adb_result(
        args,
        timeout=timeout,
        input_text=input_text,
        backend=backend,
        resolve=False,
    )
    try:
        get_pairing_server()._record_adb_result(result, args)
    except Exception:
        pass
    return result


def _adb_pair(address: str, password: str, backend: AdbBackend | None = None) -> dict:
    result = _run_adb(["pair", address, password], timeout=25, backend=backend)
    if _adb_success(result["output"], ("successfully paired", "already paired", "paired")):
        return result
    if "usage" in result["output"].lower() or "unknown" in result["output"].lower():
        return _run_adb(["pair", address], timeout=25, input_text=f"{password}\n", backend=backend)
    return result


def _adb_success(output: str, needles: tuple[str, ...]) -> bool:
    lowered = (output or "").lower()
    return any(needle in lowered for needle in needles)


def _list_mdns_services() -> list[MdnsService]:
    adb_output = ""
    try:
        result = _run_adb(["mdns", "services"], timeout=8, backend=get_pairing_server()._backend)
        adb_output = result["output"]
    except Exception as e:
        logger.warning("adb mdns services failed: %s", e)
    services = parse_mdns_services(adb_output)
    zeroconf_services = _discover_zeroconf_services(timeout=0.8)
    combined: dict[tuple[str, str, str], MdnsService] = {}
    for service in [*services, *zeroconf_services]:
        combined[(service.name, service.service_type, service.address)] = service
    if zeroconf_services:
        zc_lines = [
            f"{service.name}\t{service.service_type}\t{service.address}"
            for service in zeroconf_services
        ]
        try:
            get_pairing_server()._record_mdns_output(
                "\n".join(
                    part
                    for part in (
                        adb_output,
                        "zeroconf discovered services:",
                        "\n".join(zc_lines),
                    )
                    if part
                )
            )
        except Exception:
            pass
    return list(combined.values())


def _discover_zeroconf_services(timeout: float = 0.8) -> list[MdnsService]:
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except Exception:
        return []

    service_types = [
        f"{PAIRING_SERVICE_TYPE}.local.",
        *[f"{service_type}.local." for service_type in CONNECT_SERVICE_TYPES],
    ]
    found: list[MdnsService] = []
    lock = threading.Lock()

    def _instance_name(full_name: str, service_type: str) -> str:
        suffix = f".{service_type}"
        return full_name[: -len(suffix)] if full_name.endswith(suffix) else full_name.split(".", 1)[0]

    def _addr_text(info) -> str:
        addresses = getattr(info, "addresses", None) or []
        if not addresses:
            parsed = getattr(info, "parsed_addresses", lambda: [])()
            return parsed[0] if parsed else ""
        try:
            return socket.inet_ntop(socket.AF_INET, addresses[0])
        except Exception:
            try:
                return socket.inet_ntop(socket.AF_INET6, addresses[0])
            except Exception:
                return ""

    class _Listener(ServiceListener):
        def add_service(self, zeroconf, service_type, name):  # type: ignore[override]
            self._record(zeroconf, service_type, name)

        def update_service(self, zeroconf, service_type, name):  # type: ignore[override]
            self._record(zeroconf, service_type, name)

        def remove_service(self, zeroconf, service_type, name):  # type: ignore[override]
            return None

        def _record(self, zeroconf, service_type, name):
            info = zeroconf.get_service_info(service_type, name, timeout=500)
            if not info:
                return
            host = _addr_text(info)
            port = str(getattr(info, "port", "") or "")
            if not host or not port:
                return
            base_type = service_type.replace(".local.", "")
            service = MdnsService(
                name=_instance_name(name, service_type),
                service_type=base_type,
                address=f"{host}:{port}",
            )
            with lock:
                found.append(service)

    zeroconf = Zeroconf()
    try:
        listener = _Listener()
        browsers = [ServiceBrowser(zeroconf, service_type, listener) for service_type in service_types]
        time.sleep(timeout)
        for browser in browsers:
            try:
                browser.cancel()
            except Exception:
                pass
    except Exception:
        return []
    finally:
        try:
            zeroconf.close()
        except Exception:
            pass
    return found


def _first_connected_device() -> str:
    try:
        result = _run_adb(["devices", "-l"], timeout=8, backend=get_pairing_server()._backend)
    except Exception:
        return ""
    for raw_line in result["output"].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return ""


_pairing_server: Optional[PairingServer] = None


def get_pairing_server() -> PairingServer:
    global _pairing_server
    if _pairing_server is None:
        _pairing_server = PairingServer()
    return _pairing_server
