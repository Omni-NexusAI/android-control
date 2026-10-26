import asyncio
from typing import Optional

from helpers.tool import Tool, Response
from usr.plugins.droidclaw.helpers.adb_backend import parse_adb_command, run_adb_async
from usr.plugins.droidclaw.helpers.device_context import DeviceSelectionError, claim, screenshot_path
from usr.plugins.droidclaw.helpers import vision
from usr.plugins.droidclaw.helpers.ui_parser import dump_ui_xml, parse_ui_xml


async def _run_adb(cmd: list, device: Optional[str] = None, timeout: int = 30) -> str:
    try:
        result = await run_adb_async(cmd, device=device if device is not None else "", timeout=timeout)
    except asyncio.TimeoutError:
        return f"Error: command timed out after {timeout}s"
    if result["returncode"] != 0:
        detail = result["stderr"] or result["stdout"] or result["output"] or "unknown error"
        return f"Error: {detail}"
    return result["stdout"] or result["stderr"] or "OK"


def _escape_for_adb(text: str) -> str:
    return vision.escape_input_text(text)


_SIZES: dict[str, tuple] = {}


async def _screen_size(device: str) -> tuple:
    if device in _SIZES:
        return _SIZES[device]
    output = await _run_adb(["shell", "wm", "size"], device)
    size = (1080, 2400)
    for line in output.splitlines():
        if "size" in line:
            try:
                w, h = line.split(":")[-1].strip().lower().split("x")
                size = (int(w), int(h))
            except ValueError:
                continue
    _SIZES[device] = size
    return size


async def _parse_coordinates(coordinates: str) -> tuple:
    if not coordinates:
        raise ValueError("coordinates required (format: x,y)")
    parts = coordinates.replace(" ", "").split(",")
    if len(parts) != 2:
        raise ValueError(f"Invalid coordinates '{coordinates}'. Use format: x,y")
    return int(parts[0]), int(parts[1])


def _element_point(agent, device: str, element) -> tuple:
    row = vision.recall_element(agent, device, element)
    if row is None:
        raise ValueError(
            f"element {element} is unknown for {device}; read the screen first with adb_screen "
            "(mode xml or annotated) and use a number from that list"
        )
    return int(row["center"][0]), int(row["center"][1])


async def _resolve_point(agent, coordinates: str, element, device: str) -> tuple:
    if element not in (None, ""):
        return _element_point(agent, device, element)
    return await _parse_coordinates(coordinates)


async def _handle_tap(coordinates: str, device: Optional[str], agent=None, element=None) -> str:
    try:
        x, y = await _resolve_point(agent, coordinates, element, device)
    except ValueError as e:
        return f"Error: {e}"
    result = await _run_adb(["shell", "input", "tap", str(x), str(y)], device)
    return f"Tapped ({x}, {y}). {result}" if result != "OK" else f"Tapped ({x}, {y})."


async def _handle_longpress(coordinates: str, device: Optional[str], agent=None, element=None) -> str:
    try:
        x, y = await _resolve_point(agent, coordinates, element, device)
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
    if direction and direction.lower() in ("up", "down", "left", "right"):
        size = await _screen_size(device)
        x1, y1, x2, y2 = (str(v) for v in vision.swipe_vector(direction.lower(), size))
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
        ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
        device,
    )
    if "No activities found" in result or result.startswith("Error"):
        return f"Error: could not launch {package} (is it installed?). {result}"
    return f"Launched {package}."


async def _handle_shell(command: str, device: Optional[str]) -> str:
    if not command:
        return "Error: command is required for shell action"
    return await _run_adb(parse_adb_command("shell " + command), device)


async def _handle_screenshot(device: Optional[str]) -> str:
    filepath = screenshot_path(device or "auto")
    result = await run_adb_async(["exec-out", "screencap", "-p"], device=device or "", timeout=15, binary=True)
    if result["returncode"] != 0:
        return f"Error: screenshot failed - {result['stderr'] or result['output']}"
    stdout = result["stdout"]
    with open(filepath, "wb") as f:
        f.write(stdout)
    size_kb = len(stdout) / 1024
    return f"Screenshot saved: {filepath} ({size_kb:.1f} KB)"


async def _handle_screen_dump(device: Optional[str], agent=None) -> str:
    xml_content = await asyncio.to_thread(dump_ui_xml, device)
    if not xml_content:
        return "Error dumping UI: uiautomator returned no hierarchy (screen may be animating or secure)"
    raw = parse_ui_xml(xml_content)
    size = (
        max((e["rect"][2] for e in raw), default=0),
        max((e["rect"][3] for e in raw), default=0),
    )
    selected = vision.select_elements(raw, 60, size)
    if not selected:
        return "UI dump empty - no interactive elements found"
    vision.remember_elements(agent, device, selected)
    return f"Screen {size[0]}x{size[1]}. Elements (tap with \"element\": N):\n" + vision.format_elements(selected)


class AdbTools(Tool):
    async def execute(self, **kwargs) -> Response:
        action = self.args.get("action", "")
        if not action:
            return Response(message="Error: action is required", break_loop=False)

        coordinates = self.args.get("coordinates", "")
        element = self.args.get("element", None)
        direction = self.args.get("direction", "")
        text = self.args.get("text", "")
        keycode = self.args.get("keycode", "")
        package = self.args.get("package", "")
        command = self.args.get("command", "")
        device = self.args.get("device", None)

        try:
            dev = await claim(self.agent, device)
        except DeviceSelectionError as e:
            return Response(message=f"Error: {e}", break_loop=False)

        handlers = {
            "tap": lambda: _handle_tap(coordinates, dev, self.agent, element),
            "longpress": lambda: _handle_longpress(coordinates, dev, self.agent, element),
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
            "screen_dump": lambda: _handle_screen_dump(dev, self.agent),
        }

        if action in handlers:
            result = await handlers[action]()
        else:
            result = f"Error: unknown action '{action}'. Valid actions: tap, longpress, swipe, type, press, launch, home, back, enter, shell, screenshot, screen_dump"

        return Response(message=result, break_loop=False)
