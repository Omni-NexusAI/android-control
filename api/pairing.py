"""Wireless ADB pairing and device management."""

import asyncio
import logging

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
        else:
            return {
                "success": False,
                "message": f"Unknown action: {action}",
            }

    async def _pair(self, input: dict) -> dict:
        ip = input.get("ip", "")
        port = input.get("port", "")
        code = input.get("code", "")

        if not ip or not port or not code:
            return {
                "success": False,
                "message": "ip, port, and code are required for pairing",
            }

        addr = f"{ip}:{port}"

        try:
            result = await run_adb_async(["pair", addr, code], timeout=15)
            combined = result["output"]

            if "successfully paired" in combined.lower() or "paired" in combined.lower():
                return {
                    "success": True,
                    "message": f"Successfully paired with {addr}",
                    "adb_backend": result["backend"],
                }
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
        ip = input.get("ip", "")
        port = str(input.get("port", "5555"))

        if not ip:
            return {
                "success": False,
                "message": "ip is required",
            }

        addr = f"{ip}:{port}"

        try:
            result = await run_adb_async(["connect", addr], timeout=15)
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
            from usr.plugins.droidclaw.helpers.pairing_server import get_pairing_server

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
        from usr.plugins.droidclaw.helpers.pairing_server import check_dependencies as check_adb_dependencies

        python_deps = check_python_dependencies()
        adb_deps = check_adb_dependencies()
        missing = list(dict.fromkeys((python_deps.get("missing") or []) + (adb_deps.get("missing") or [])))
        result = {
            "available": python_deps.get("available", False) and adb_deps.get("available", False),
            "missing": missing,
            "install_command": python_deps.get("install_command") or adb_deps.get("install_command") or "",
            "python_dependencies": python_deps,
            "adb_dependencies": adb_deps,
            "adb_backend": adb_diagnostics(),
        }
        result["success"] = True
        return result
