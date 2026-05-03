import asyncio
import os
import time
import xml.etree.ElementTree as ET

from helpers.tool import Tool, Response
from usr.plugins.droidclaw.helpers.adb_backend import adb_cmd, resolve_device, run_adb_async, select_backend


async def _run_adb(cmd: list, device: str, timeout: int = 30) -> tuple:
    try:
        result = await run_adb_async(cmd, device=device, timeout=timeout)
        return (
            result["returncode"],
            result["stdout"],
            result["stderr"],
        )
    except asyncio.TimeoutError:
        return (-1, "", "ADB command timed out")
    except FileNotFoundError:
        return (-1, "", "adb not found in PATH")
    except Exception as e:
        return (-1, "", str(e))


async def _get_device(device: str) -> str:
    resolution = resolve_device(device if device is not None else "")
    return resolution.get("resolved_device") or ""


async def _dump_ui(device: str, filter_text: str, max_elements: int) -> str:
    rc, out, err = await _run_adb(
        ["shell", "uiautomator", "dump", "/sdcard/ui.xml"], device, timeout=15
    )
    if rc != 0:
        return f"Error dumping UI: {err}"
    local_path = "/tmp/droidclaw_ui.xml"
    rc, out, err = await _run_adb(
        ["pull", "/sdcard/ui.xml", local_path], device, timeout=10
    )
    if rc != 0:
        return f"Error pulling UI dump: {err}"
    await _run_adb(["shell", "rm", "/sdcard/ui.xml"], device, timeout=5)
    if not os.path.exists(local_path):
        return "Error: UI dump file not found after pull"
    result = _parse_ui_xml(local_path, filter_text, max_elements)
    try:
        os.remove(local_path)
    except OSError:
        pass
    return result


def _parse_ui_xml(xml_path: str, filter_text: str, max_elements: int) -> str:
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        return f"Error parsing XML: {e}"
    except Exception as e:
        return f"Error reading XML file: {e}"

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
    screenshot_dir = "/a0/tmp/droidclaw_screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"screen_{timestamp}.png"
    filepath = os.path.join(screenshot_dir, filename)
    try:
        backend = select_backend()
        resolution = resolve_device(device, backend)
        if not resolution.get("resolved_device"):
            return "Error: No ADB device found. Connect a device first."
        proc = await asyncio.create_subprocess_exec(
            *adb_cmd(["exec-out", "screencap", "-p"], device=resolution["resolved_device"], backend=backend),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return f"Error capturing screenshot: {stderr.decode('utf-8', errors='replace')}"
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

        device = await _get_device(device)
        if not device:
            return Response(
                message="Error: No ADB device found. Connect a device first.",
                break_loop=False,
            )

        results = []
        if mode in ("xml", "both"):
            results.append(await _dump_ui(device, filter_text, max_elements))
        if mode in ("screenshot", "both"):
            results.append(await _take_screenshot(device))

        return Response(message="\n\n".join(results), break_loop=False)
