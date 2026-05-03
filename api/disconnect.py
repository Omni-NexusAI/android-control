"""Disconnect a wireless ADB device."""

import logging

from helpers.api import ApiHandler, Request, Response
from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import run_adb_async

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"


class Disconnect(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        ip_port = input.get("ip_port", "")

        try:
            if ip_port:
                result = await run_adb_async(["disconnect", ip_port], timeout=15)
                return {
                    "success": True,
                    "message": f"Disconnected {ip_port}",
                    "adb_backend": result["backend"],
                }
            else:
                result = await run_adb_async(["disconnect"], timeout=15)
                return {
                    "success": True,
                    "message": "Disconnected all devices",
                    "adb_backend": result["backend"],
                }
        except Exception as e:
            logger.error(f"ADB disconnect failed: {e}")
            return {"success": False, "message": str(e)}
