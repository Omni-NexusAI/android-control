"""Execute an ADB command and return result."""

import asyncio
import logging

from helpers.api import ApiHandler, Request, Response
from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import parse_adb_command, run_adb_async

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"


class AdbCommand(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        command = input.get("command", "")
        device = input.get("device", None)
        timeout = int(input.get("timeout", 30))

        if not command:
            return {"success": False, "message": "command is required"}

        timeout = min(timeout, 30)
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
        args = parse_adb_command(command)
        if not args:
            return {"success": False, "message": "command is empty"}

        global_commands = {"devices", "connect", "disconnect", "pair", "mdns", "version", "start-server", "kill-server"}
        target_device = None if args[0] in global_commands else (device if device is not None else cfg.get("device", ""))

        try:
            result = await run_adb_async(args, device=target_device, timeout=timeout)

            return {
                "success": result["returncode"] == 0,
                "stdout": result["stdout"],
                "stderr": result["stderr"] if result["stderr"] else "",
                "returncode": result["returncode"],
                "adb_backend": result["backend"],
                "requested_device": result["requested_device"],
                "resolved_device": result["resolved_device"],
                "device_resolution": result["device_resolution"],
            }
        except asyncio.TimeoutError:
            return {"success": False, "message": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"success": False, "message": str(e)}

