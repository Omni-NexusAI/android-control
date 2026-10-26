import asyncio
import os
import xml.etree.ElementTree as ET

from helpers.tool import Tool, Response
from usr.plugins.droidclaw.helpers.adb_backend import run_adb_async
from usr.plugins.droidclaw.helpers.device_context import DeviceSelectionError, claim, screenshot_path
from usr.plugins.droidclaw.helpers.ui_parser import dump_ui_xml


async def _dump_ui(device: str, filter_text: str, max_elements: int) -> str:
    xml_content = await asyncio.to_thread(dump_ui_xml, device)
    if not xml_content:
        return "Error dumping UI: uiautomator returned no hierarchy (screen may be animating or secure)"
    return _parse_ui_xml(xml_content, filter_text, max_elements)


def _parse_ui_xml(xml_content: str, filter_text: str, max_elements: int) -> str:
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        return f"Error parsing XML: {e}"

    elements = []
    filter_lower = filter_text.lower() if filter_text else ""

    for node in root.iter():
        text = node.get("text", "") or ""
        desc = node.get("content-desc", "") or ""
        bounds = node.get("bounds", "") or ""
        clickable = node.get("clickable", "false") or "false"
        res_id = node.get("resource-id", "") or ""
        checkable = node.get("checkable", "false") or "false"
        checked = node.get("checked", "false") or "false"
        focusable = node.get("focusable", "false") or "false"
        focused = node.get("focused", "false") or "false"
        scrollable = node.get("scrollable", "false") or "false"
        enabled = node.get("enabled", "true") or "true"
        package = node.get("package", "") or ""
        klass = node.get("class", "") or ""

        if filter_lower:
            match = (
                filter_lower in text.lower()
                or filter_lower in desc.lower()
                or filter_lower in res_id.lower()
            )
            if not match:
                continue

        if not filter_lower and not text and not desc and clickable != "true":
            continue

        elements.append(
            {
                "text": text,
                "desc": desc,
                "bounds": bounds,
                "clickable": clickable,
                "res_id": res_id,
                "checkable": checkable,
                "checked": checked,
                "focusable": focusable,
                "focused": focused,
                "scrollable": scrollable,
                "enabled": enabled,
                "package": package,
                "class": klass,
            }
        )

        if len(elements) >= max_elements:
            break

    if not elements:
        if filter_text:
            return f'No elements matching filter "{filter_text}" found.'
        return "No UI elements found."

    lines = [f"UI Elements ({len(elements)} found):"]
    lines.append("=" * 60)
    for i, elem in enumerate(elements):
        lines.append(f"\n[{i}] {elem['text'] or '(no text)'}")
        if elem["desc"]:
            lines.append(f"    desc: {elem['desc']}")
        if elem["res_id"]:
            lines.append(f"    id: {elem['res_id']}")
        lines.append(f"    bounds: {elem['bounds']}")
        flags = []
        if elem["clickable"] == "true":
            flags.append("clickable")
        if elem["scrollable"] == "true":
            flags.append("scrollable")
        if elem["checkable"] == "true":
            flags.append("checkable")
        if elem["focusable"] == "true":
            flags.append("focusable")
        if elem["checked"] == "true":
            flags.append("checked")
        if elem["focused"] == "true":
            flags.append("focused")
        if flags:
            lines.append(f"    flags: {', '.join(flags)}")

    return "\n".join(lines)


async def _take_screenshot(device: str) -> str:
    filepath = screenshot_path(device)
    try:
        result = await run_adb_async(["exec-out", "screencap", "-p"], device=device, timeout=15, binary=True)
        if result["returncode"] != 0:
            return f"Error capturing screenshot: {result['stderr'] or result['output']}"
        stdout = result["stdout"]
        with open(filepath, "wb") as f:
            f.write(stdout)
        size_kb = os.path.getsize(filepath) / 1024
        return f"Screenshot saved: {filepath} ({size_kb:.1f} KB)"
    except asyncio.TimeoutError:
        return "Error: Screenshot capture timed out"
    except Exception as e:
        return f"Error capturing screenshot: {e}"


class AdbScreen(Tool):
    async def execute(self, **kwargs) -> Response:
        mode = self.args.get("mode", "xml")
        device = self.args.get("device", "")
        filter_text = self.args.get("filter", "")
        max_elements = int(self.args.get("max_elements", 40))

        try:
            device = await claim(self.agent, device)
        except DeviceSelectionError as e:
            return Response(message=f"Error: {e}", break_loop=False)

        results = []
        if mode in ("xml", "both"):
            results.append(await _dump_ui(device, filter_text, max_elements))
        if mode in ("screenshot", "both"):
            results.append(await _take_screenshot(device))

        return Response(message="\n\n".join(results), break_loop=False)
