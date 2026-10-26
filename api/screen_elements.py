"""Numbered list of meaningful on-screen elements (the panel's Dump button)."""

import asyncio

from helpers.api import ApiHandler, Request, Response
from usr.plugins.droidclaw.helpers import vision
from usr.plugins.droidclaw.helpers.adb_backend import resolve_device
from usr.plugins.droidclaw.helpers.ui_parser import dump_ui_xml, parse_ui_xml


class ScreenElements(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        resolution = await asyncio.to_thread(resolve_device, input.get("device") or "")
        serial = resolution.get("resolved_device") or ""
        if not serial:
            return {"success": False, "message": "No connected device"}
        xml = await asyncio.to_thread(dump_ui_xml, serial)
        if not xml:
            return {"success": False, "message": "uiautomator returned no hierarchy (screen may be animating or secure)"}
        raw = parse_ui_xml(xml)
        width = max((e["rect"][2] for e in raw), default=0)
        height = max((e["rect"][3] for e in raw), default=0)
        selected = vision.select_elements(raw, int(input.get("max_elements") or 60), (width, height))
        return {
            "success": True,
            "device": serial,
            "width": width,
            "height": height,
            "count": len(selected),
            "text": vision.format_elements(selected),
        }
