import subprocess
import asyncio
import os
import time
import xml.etree.ElementTree as ET
from typing import Optional

from helpers.tool import Tool, Response
from usr.plugins.droidclaw.helpers.adb_backend import adb_cmd, parse_adb_command, resolve_device, run_adb_async, select_backend


async def _run_adb(cmd: list, device: Optional[str] = None, timeout: int = 30) -> str:
    try:
        result = await run_adb_async(cmd, device=device if device is not None else "", timeout=timeout)
    except asyncio.TimeoutError:
        return f"Error: command timed out after {timeout}s"
    if result["returncode"] != 0:
        detail = result["stderr"] or result["stdout"] or result["output"] or "unknown error"
        return f"Error: {detail}"
    return result["stdout"] or result["stderr"] or "OK"


async def _get_device(device: Optional[str] = None) -> Optional[str]:
    resolution = resolve_device(device if device is not None else "")
    if resolution.get("resolved_device"):
        return resolution["resolved_device"]
    if device:
        raise RuntimeError(f"ADB device is not connected: {device}")
    raise RuntimeError("No ADB device connected. Run 'adb devices' to check.")


def _escape_for_adb(text: str) -> str:
    replacements = {
        " ": "%s",
        "&": "\\&",
        ";": "\\;",
        "|": "\\|",
        "<": "\\<",
        ">": "\\>",
        "(": "\\(",
        ")": "\\)",
        '"': '\\"',
        "'": "\\'",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


async def _parse_coordinates(coordinates: str) -> tuple:
    if not coordinates:
        raise ValueError("coordinates required (format: x,y)")
    parts = coordinates.replace(" ", "").split(",")
    if len(parts) != 2:
        raise ValueError(f"Invalid coordinates '{coordinates}'. Use format: x,y")
    return int(parts[0]), int(parts[1])


async def _handle_tap(coordinates: str, device: Optional[str]) -> str:
    try:
        x, y = await _parse_coordinates(coordinates)
    except ValueError as e:
        return f"Error: {e}"
    result = await _run_adb(["shell", "input", "tap", str(x), str(y)], device)
    return f"Tapped ({x}, {y}). {result}" if result != "OK" else f"Tapped ({x}, {y})."


async def _handle_longpress(coordinates: str, device: Optional[str]) -> str:
    try:
        x, y = await _parse_coordinates(coordinates)
    except ValueError as e:
        return f"Error: {e}"
    result = await _run_adb(
        ["shell", "input", "swipe", str(x), str(y), str(x), str(y), "1000"], device
    )
    return (
        f"Long-pressed ({x}, {y}). {result}"
        if result != "OK"
        else f"Long-pressed ({x}, {y})."
    )


async def _handle_swipe(direction: str, coordinates: str, device: Optional[str]) -> str:
    swipe_map = {
        "up": ("540", "1600", "540", "800"),
        "down": ("540", "800", "540", "1600"),
        "left": ("800", "1200", "200", "1200"),
        "right": ("200", "1200", "800", "1200"),
    }
    if direction.lower() in swipe_map:
        x1, y1, x2, y2 = swipe_map[direction.lower()]
    elif coordinates:
        try:
            parts = coordinates.split(",")
            if len(parts) != 4:
                return "Error: swipe coordinates format: x1,y1,x2,y2"
            x1, y1, x2, y2 = parts[0], parts[1], parts[2], parts[3]
        except Exception:
            return "Error: swipe coordinates format: x1,y1,x2,y2"
    else:
        return (
            "Error: provide direction (up/down/left/right) or coordinates (x1,y1,x2,y2)"
        )
    result = await _run_adb(["shell", "input", "swipe", x1, y1, x2, y2, "300"], device)
    dir_label = direction if direction else f"({x1},{y1})->({x2},{y2})"
    return f"Swiped {dir_label}. {result}" if result != "OK" else f"Swiped {dir_label}."


async def _handle_type(text: str, device: Optional[str]) -> str:
    if not text:
        return "Error: text is required for type action"
    escaped = _escape_for_adb(text)
    result = await _run_adb(["shell", "input", "text", escaped], device)
    return f"Typed: '{text}'. {result}" if result != "OK" else f"Typed: '{text}'."


async def _handle_press(keycode: str, device: Optional[str]) -> str:
    if not keycode:
        return "Error: keycode is required for press action"
    result = await _run_adb(["shell", "input", "keyevent", keycode], device)
    return (
        f"Pressed keycode {keycode}. {result}"
        if result != "OK"
        else f"Pressed keycode {keycode}."
    )


async def _handle_launch(package: str, device: Optional[str]) -> str:
    if not package:
        return "Error: package is required for launch action"
    result = await _run_adb(
        ["shell", "am", "start", "-n", f"{package}/.MainActivity"], device
    )
    fallback = False
    if "Error" in result or "does not exist" in result:
        result = await _run_adb(
            [
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            device,
        )
        fallback = True
    method = "monkey fallback" if fallback else "am start"
    return f"Launched {package} ({method}). {result}"


async def _handle_shell(command: str, device: Optional[str]) -> str:
    if not command:
        return "Error: command is required for shell action"
    return await _run_adb(parse_adb_command("shell " + command), device)


async def _handle_screenshot(device: Optional[str]) -> str:
    screenshot_dir = "/a0/tmp/droidclaw_screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(screenshot_dir, f"screen_{ts}.png")
    backend = select_backend()
    resolution = resolve_device(device if device is not None else "", backend)
    if not resolution.get("resolved_device"):
        return f"Error: ADB device is not connected: {device or 'auto'}"
    proc = await asyncio.create_subprocess_exec(
        *adb_cmd(["exec-out", "screencap", "-p"], device=resolution["resolved_device"], backend=backend),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        return f"Error: screenshot failed - {err}"
    with open(filepath, "wb") as f:
        f.write(stdout)
    size_kb = len(stdout) / 1024
    return f"Screenshot saved: {filepath} ({size_kb:.1f} KB)"


async def _handle_screen_dump(device: Optional[str]) -> str:
    dump_result = await _run_adb(
        ["shell", "uiautomator", "dump", "/sdcard/ui.xml"], device, timeout=15
    )
    if "Error" in dump_result:
        return f"Error dumping UI: {dump_result}"
    tmp_path = "/tmp/droidclaw_ui.xml"
    pull_result = await _run_adb(["pull", "/sdcard/ui.xml", tmp_path], device)
    if "Error" in pull_result:
        return f"Error pulling UI dump: {pull_result}"
    try:
        tree = ET.parse(tmp_path)
        root = tree.getroot()
    except ET.ParseError as e:
        return f"Error parsing UI XML: {e}"
    elements = []
    idx = 0
    for node in root.iter():
        bounds = node.attrib.get("bounds", "")
        text_val = node.attrib.get("text", "")
        desc = node.attrib.get("content-desc", "")
        clickable = node.attrib.get("clickable", "false")
        res_id = node.attrib.get("resource-id", "")
        class_name = node.attrib.get("class", "")
        label = text_val or desc or ""
        if not label and not clickable == "true":
            continue
        center_x, center_y = "", ""
        if bounds:
            try:
                parts = bounds.replace("][", ",").strip("[]").split(",")
                x1, y1, x2, y2 = (
                    int(parts[0]),
                    int(parts[1]),
                    int(parts[2]),
                    int(parts[3]),
                )
                center_x = str((x1 + x2) // 2)
                center_y = str((y1 + y2) // 2)
            except (ValueError, IndexError):
                pass
        click_tag = " [clickable]" if clickable == "true" else ""
        id_tag = f" id={res_id}" if res_id else ""
        coord_tag = f" ({center_x},{center_y})" if center_x else ""
        elements.append(f"[{idx}] {label}{coord_tag}{click_tag}{id_tag}")
        idx += 1
    if not elements:
        return "UI dump empty - no interactive elements found"
    return "\n".join(elements)


class AdbTools(Tool):
    async def execute(self, **kwargs) -> Response:
        action = self.args.get("action", "")
        if not action:
            return Response(message="Error: action is required", break_loop=False)

        coordinates = self.args.get("coordinates", "")
        direction = self.args.get("direction", "")
        text = self.args.get("text", "")
        keycode = self.args.get("keycode", "")
        package = self.args.get("package", "")
        command = self.args.get("command", "")
        device = self.args.get("device", None)

        try:
            dev = await _get_device(device)
        except RuntimeError as e:
            return Response(message=f"Error: {e}", break_loop=False)

        handlers = {
            "tap": lambda: _handle_tap(coordinates, dev),
            "longpress": lambda: _handle_longpress(coordinates, dev),
            "swipe": lambda: _handle_swipe(direction, coordinates, dev),
            "type": lambda: _handle_type(text, dev),
            "press": lambda: _handle_press(keycode, dev),
            "home": lambda: _run_adb(
                ["shell", "input", "keyevent", "KEYCODE_HOME"], dev
            ),
            "back": lambda: _run_adb(
                ["shell", "input", "keyevent", "KEYCODE_BACK"], dev
            ),
            "enter": lambda: _run_adb(
                ["shell", "input", "keyevent", "KEYCODE_ENTER"], dev
            ),
            "launch": lambda: _handle_launch(package, dev),
            "shell": lambda: _handle_shell(command, dev),
            "screenshot": lambda: _handle_screenshot(dev),
            "screen_dump": lambda: _handle_screen_dump(dev),
        }

        if action in handlers:
            result = await handlers[action]()
        else:
            result = f"Error: unknown action '{action}'. Valid actions: tap, longpress, swipe, type, press, launch, home, back, enter, shell, screenshot, screen_dump"

        return Response(message=result, break_loop=False)
