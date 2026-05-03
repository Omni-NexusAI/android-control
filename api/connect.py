"""Connect to a wireless ADB device."""

import logging

from helpers.api import ApiHandler, Request, Response
from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import run_adb_async

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"


class Connect(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
        ip_port = input.get("ip_port", cfg.get("device", ""))

        if not ip_port:
            return {
                "success": False,
                "message": "ip_port is required (format: IP:PORT)",
            }

        try:
            result = await run_adb_async(["connect", ip_port], timeout=15)
            combined = result["output"]

            if (
                "connected" in combined.lower()
                or "already connected" in combined.lower()
            ):
                return {
                    "success": True,
                    "message": f"Connected to {ip_port}",
                    "serial": ip_port,
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
