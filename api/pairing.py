"""Wireless ADB pairing and device management."""

import asyncio
import logging
import time

from helpers.api import ApiHandler, Request, Response
from usr.plugins.droidclaw.helpers.adb_backend import (
    canonical_devices,
    diagnostics as adb_diagnostics,
    list_mdns_services,
    run_adb_async,
    select_backend,
)

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"
CONNECT_SERVICE_TYPE = "_adb-tls-connect._tcp"


def _normalize_endpoint(ip: str, port: str = "") -> tuple[str, str, str]:
    ip = str(ip or "").strip()
    port = str(port or "").strip()
    if ip and not port and ":" in ip:
        host, maybe_port = ip.rsplit(":", 1)
        if host and maybe_port.isdigit():
            ip = host.strip()
            port = maybe_port.strip()
    addr = f"{ip}:{port}" if ip and port else ""
    return ip, port, addr


def _find_connect_service(ip: str, backend=None) -> dict:
    ip = str(ip or "").strip()
    if not ip:
        return {}
    for service in list_mdns_services(backend):
        if service.service_type == CONNECT_SERVICE_TYPE and service.host == ip:
            return {
                "connect_ip": service.host,
                "connect_port": service.port,
                "connect_addr": service.address,
            }
    return {}


async def _wait_for_connect_service(ip: str, backend=None, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        service = _find_connect_service(ip, backend)
        if service:
            return service
        await asyncio.sleep(1.0)
    return {}


class Pairing(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        action = input.get("action", "status")

        if action == "pair":
            return await self._pair(input)
        elif action == "connect":
            return await self._connect(input)
        elif action == "disconnect":
            return await self._disconnect(input)
        elif action == "status":
            return await self._status()
        elif action == "start_qr_pairing":
            return await self._start_qr_pairing()
        elif action == "stop_qr_pairing":
            return await self._stop_qr_pairing()
        elif action == "pairing_status":
            return await self._pairing_status()
        elif action == "check_deps":
            return await self._check_deps()
        elif action == "bridge_status":
            return await self._bridge_status()
        else:
            return {
                "success": False,
                "message": f"Unknown action: {action}",
            }

    async def _pair(self, input: dict) -> dict:
        ip, port, addr = _normalize_endpoint(input.get("ip", ""), input.get("port", ""))
        code = input.get("code", "")

        if not ip or not port or not code:
            return {
                "success": False,
                "message": "ip, port, and code are required for pairing",
            }

        try:
            backend = select_backend()
            result = await run_adb_async(["pair", addr, code], timeout=15, backend=backend, resolve=False)
            combined = result["output"]

            if "successfully paired" in combined.lower() or "paired" in combined.lower():
                response = {
                    "success": True,
                    "message": f"Successfully paired with {addr}",
                    "adb_backend": result["backend"],
                    "pair_ip": ip,
                    "pair_port": port,
                    "pair_addr": addr,
                }
                response.update(await _wait_for_connect_service(ip, backend))
                return response
            else:
                return {
                    "success": False,
                    "message": f"Pairing failed: {combined}",
                    "adb_backend": result["backend"],
                }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "message": f"Pairing timed out for {addr}",
            }
        except Exception as e:
            logger.error(f"ADB pair failed: {e}")
            return {"success": False, "message": str(e)}

    async def _connect(self, input: dict) -> dict:
        ip, port, addr = _normalize_endpoint(input.get("ip", ""), input.get("port", ""))

        if not ip or not port:
            return {
                "success": False,
                "message": "ip and port are required. Enter the IP address and Port shown in Android Wireless debugging.",
            }

        try:
            result = await run_adb_async(["connect", addr], timeout=15, resolve=False)
            combined = result["output"]

            if (
                "connected" in combined.lower()
                or "already connected" in combined.lower()
            ):
                return {
                    "success": True,
                    "message": f"Connected to {addr}",
                    "serial": addr,
                    "adb_backend": result["backend"],
                }
            else:
                return {
                    "success": False,
                    "message": f"Connection failed: {combined}",
                    "adb_backend": result["backend"],
                }
        except Exception as e:
            logger.error(f"ADB connect failed: {e}")
            return {"success": False, "message": str(e)}

    async def _disconnect(self, input: dict) -> dict:
        device = input.get("serial", "")

        if not device:
            return {
                "success": False,
                "message": "serial is required (serial or IP:PORT)",
            }

        try:
            result = await run_adb_async(["disconnect", device], timeout=15)
            combined = result["output"]

            if "disconnected" in combined.lower() or not combined:
                return {
                    "success": True,
                    "message": f"Disconnected from {device}",
                    "adb_backend": result["backend"],
                }
            else:
                return {
                    "success": False,
                    "message": f"Disconnect failed: {combined}",
                    "adb_backend": result["backend"],
                }
        except Exception as e:
            logger.error(f"ADB disconnect failed: {e}")
            return {"success": False, "message": str(e)}

    async def _status(self) -> dict:
        try:
            backend = select_backend()
            devices = [device.to_dict() for device in canonical_devices(backend)]
            services = [service.to_dict() for service in list_mdns_services(backend)]

            return {
                "success": True,
                "message": f"{len(devices)} device(s) found",
                "devices": devices,
                "mdns_services": services,
                "adb_backend": backend.name,
                "adb_backend_message": backend.message,
            }
        except Exception as e:
            logger.error(f"ADB status failed: {e}")
            return {"success": False, "message": str(e)}

    async def _start_qr_pairing(self) -> dict:
        """Start the Android ADB QR pairing workflow."""
        try:
            from usr.plugins.droidclaw.helpers.adb_runtime import bootstrap_adb_runtime, bridge_capabilities
            from usr.plugins.droidclaw.helpers.pairing_server import get_pairing_server

            bootstrap_adb_runtime(force=True)
            capabilities = bridge_capabilities(force=False)
            if not capabilities.get("qr_ready"):
                return {
                    "success": False,
                    "message": capabilities.get("qr_message") or "Wireless ADB QR is not ready from this A0 runtime.",
                    "requires_bridge": True,
                    "capabilities": capabilities,
                    "adb_backend": capabilities.get("selected_backend") or "",
                }
            server = get_pairing_server()
            return server.start(timeout=90.0)
        except Exception as e:
            logger.error(f"QR pairing start failed: {e}")
            return {"success": False, "message": str(e)}

    async def _stop_qr_pairing(self) -> dict:
        """Stop the QR pairing server."""
        try:
            from usr.plugins.droidclaw.helpers.pairing_server import get_pairing_server

            return get_pairing_server().stop()
        except Exception as e:
            logger.error(f"QR pairing stop failed: {e}")
            return {"success": False, "message": str(e)}

    async def _pairing_status(self) -> dict:
        """Get the current pairing server status."""
        try:
            from usr.plugins.droidclaw.helpers.pairing_server import get_pairing_server

            status = get_pairing_server().get_status()
            status["success"] = True
            return status
        except Exception as e:
            logger.error(f"QR pairing status failed: {e}")
            return {"success": False, "message": str(e)}

    async def _check_deps(self) -> dict:
        """Check if QR pairing dependencies are available."""
        from usr.plugins.droidclaw.helpers.dependencies import check_dependencies as check_python_dependencies
        from usr.plugins.droidclaw.helpers.adb_runtime import bridge_capabilities, read_adb_health
        from usr.plugins.droidclaw.helpers.pairing_server import check_dependencies as check_adb_dependencies
        from usr.plugins.droidclaw.helpers.platform_tools import find_adb

        python_deps = check_python_dependencies()
        adb_client = find_adb()
        adb_deps = check_adb_dependencies()
        missing = list(dict.fromkeys((python_deps.get("missing") or []) + (adb_deps.get("missing") or [])))
        result = {
            "available": python_deps.get("available", False) and adb_deps.get("available", False),
            "missing": missing,
            "install_command": python_deps.get("install_command") or adb_deps.get("install_command") or "",
            "python_dependencies": python_deps,
            "adb_client": adb_client,
            "adb_dependencies": adb_deps,
            "adb_backend": adb_diagnostics(),
            "adb_health": read_adb_health(),
            "capabilities": bridge_capabilities(force=False),
        }
        result["success"] = True
        return result

    async def _bridge_status(self) -> dict:
        """Return Android Control bridge capability status for QR and USB."""
        from usr.plugins.droidclaw.helpers.adb_runtime import bridge_capabilities

        capabilities = bridge_capabilities(force=True)
        capabilities["success"] = True
        return capabilities
