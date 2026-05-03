"""Capture screenshot from device and return image path."""

import asyncio
import os
import time
import logging

from helpers.api import ApiHandler, Request, Response
from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import adb_cmd, resolve_device, select_backend

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"
SCREENSHOT_DIR = "/a0/tmp/droidclaw_screenshots"


class Screenshot(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        device = input.get("device", None)
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}

        if not device:
            device = cfg.get("device", "")

        backend = select_backend()
        resolution = resolve_device(device if device is not None else "", backend)
        if not resolution.get("resolved_device"):
            return {
                "success": False,
                "message": "No device connected" if not device else f"ADB device is not connected: {device}",
                "requested_device": device or "",
                "resolved_device": "",
                "device_resolution": resolution,
                "adb_backend": backend.name,
            }

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(SCREENSHOT_DIR, f"panel_{ts}.png")

        try:
            proc = await asyncio.create_subprocess_exec(
                *adb_cmd(["exec-out", "screencap", "-p"], device=resolution["resolved_device"], backend=backend),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                return {"success": False, "message": f"Screenshot failed: {err}"}

            with open(filepath, "wb") as f:
                f.write(stdout)

            size_kb = len(stdout) / 1024
            return {
                "success": True,
                "path": filepath,
                "size_kb": round(size_kb, 1),
                "url": f"/api/image_get?path={filepath}",
                "adb_backend": backend.name,
                "requested_device": device or "",
                "resolved_device": resolution["resolved_device"],
            }
        except asyncio.TimeoutError:
            return {"success": False, "message": "Screenshot timed out"}
        except Exception as e:
            return {"success": False, "message": str(e)}
