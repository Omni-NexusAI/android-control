"""Capture screenshot from device and return image path."""

import asyncio
import base64
import io
import logging
import os
import re
import time

from helpers.api import ApiHandler, Request, Response
from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import run_adb_async

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"
SCREENSHOT_DIR = "/a0/tmp/droidclaw_screenshots"
PANEL_FRAME_MAX_AGE = 120


def _prune_panel_frames(directory: str, keep: str) -> None:
    """Panel previews are transient; drop old frames so the dir can't grow unbounded."""
    cutoff = time.time() - PANEL_FRAME_MAX_AGE
    try:
        for name in os.listdir(directory):
            if not name.startswith("panel_"):
                continue
            path = os.path.join(directory, name)
            if path != keep and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


def _jpeg_data_url(png: bytes, max_edge: int, quality: int) -> tuple[str, int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        width, height = img.size
        frame = img.convert("RGB")
    scale = min(1.0, max(64, max_edge) / max(width, height))
    if scale < 1.0:
        frame = frame.resize((int(width * scale), int(height * scale)), Image.BILINEAR)
    out = io.BytesIO()
    frame.save(out, format="JPEG", quality=max(30, min(quality, 95)))
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii"), width, height


class Screenshot(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        device = input.get("device", None)
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}

        if not device:
            device = cfg.get("device", "")

        try:
            result = await run_adb_async(
                ["exec-out", "screencap", "-p"], device=device or "", timeout=15, binary=True
            )
        except Exception as e:
            return {"success": False, "message": str(e)}

        if result["returncode"] != 0 or not result["stdout"]:
            not_connected = not result.get("resolved_device")
            return {
                "success": False,
                "message": (
                    ("No device connected" if not device else f"ADB device is not connected: {device}")
                    if not_connected
                    else f"Screenshot failed: {result['stderr'] or result['output']}"
                ),
                "requested_device": device or "",
                "resolved_device": result.get("resolved_device") or "",
                "device_resolution": result.get("device_resolution"),
                "adb_backend": result.get("backend"),
            }

        if input.get("inline"):
            # One round-trip for live previews: a downscaled JPEG data URL,
            # nothing written to disk.
            try:
                data_url, width, height = await asyncio.to_thread(
                    _jpeg_data_url, result["stdout"], int(input.get("max_edge") or 1024), int(input.get("quality") or 70)
                )
            except Exception as e:
                return {"success": False, "message": f"Could not encode preview: {e}"}
            return {
                "success": True,
                "data_url": data_url,
                "width": width,
                "height": height,
                "resolved_device": result["resolved_device"],
                "recovered": result.get("recovered", False),
            }

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        safe_serial = re.sub(r"[^A-Za-z0-9_.-]", "_", result["resolved_device"])[:64]
        filepath = os.path.join(SCREENSHOT_DIR, f"panel_{safe_serial}_{int(time.time() * 1000)}.png")
        with open(filepath, "wb") as f:
            f.write(result["stdout"])
        _prune_panel_frames(SCREENSHOT_DIR, filepath)

        return {
            "success": True,
            "path": filepath,
            "size_kb": round(len(result["stdout"]) / 1024, 1),
            "url": f"/api/image_get?path={filepath}",
            "adb_backend": result["backend"],
            "requested_device": device or "",
            "resolved_device": result["resolved_device"],
            "recovered": result.get("recovered", False),
        }
