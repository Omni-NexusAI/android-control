import asyncio

from helpers.tool import Tool, Response
from usr.plugins.droidclaw.helpers import vision
from usr.plugins.droidclaw.helpers.adb_backend import run_adb_async
from usr.plugins.droidclaw.helpers.device_context import DeviceSelectionError, claim, screenshot_path
from usr.plugins.droidclaw.helpers.ui_parser import dump_ui_xml, parse_ui_xml


def _screen_size(raw: list) -> tuple:
    width = max((e.get("rect", [0, 0, 0, 0])[2] for e in raw), default=0)
    height = max((e.get("rect", [0, 0, 0, 0])[3] for e in raw), default=0)
    return (width, height)


async def _elements(device: str, filter_text: str, max_elements: int):
    xml_content = await asyncio.to_thread(dump_ui_xml, device)
    if not xml_content:
        return None, (0, 0)
    raw = parse_ui_xml(xml_content)
    size = _screen_size(raw)
    if filter_text:
        needle = filter_text.lower()
        raw = [
            e for e in raw
            if needle in (e.get("text") or "").lower()
            or needle in (e.get("content_desc") or "").lower()
            or needle in (e.get("resource_id") or "").lower()
        ]
    return vision.select_elements(raw, max_elements, size), size


async def _screenshot_png(device: str) -> bytes:
    result = await run_adb_async(["exec-out", "screencap", "-p"], device=device, timeout=15, binary=True)
    if result["returncode"] != 0 or not result["stdout"]:
        raise RuntimeError(result["stderr"] or result["output"] or "empty screenshot")
    return result["stdout"]


class AdbScreen(Tool):
    async def execute(self, **kwargs) -> Response:
        mode = str(self.args.get("mode", "xml") or "xml").lower()
        device = self.args.get("device", "")
        filter_text = self.args.get("filter", "")
        try:
            max_elements = int(self.args.get("max_elements", 40) or 40)
        except (TypeError, ValueError):
            max_elements = 40

        try:
            device = await claim(self.agent, device)
        except DeviceSelectionError as e:
            return Response(message=f"Error: {e}", break_loop=False)

        if mode == "both":
            mode = "annotated"

        parts = []
        selected, size = (None, (0, 0))
        if mode in ("xml", "annotated"):
            selected, size = await _elements(device, filter_text, max_elements)
            if selected is None:
                parts.append("UI dump unavailable (secure, animating or canvas screen).")
            elif not selected:
                parts.append(
                    f'No elements matching filter "{filter_text}".' if filter_text else "No UI elements found."
                )
            else:
                vision.remember_elements(self.agent, device, selected)
                parts.append(
                    f"Screen {size[0]}x{size[1]} on {device}. Elements ({len(selected)}), "
                    "act on them by number (adb_tools tap with \"element\": N):\n"
                    + vision.format_elements(selected)
                )

        if mode in ("screenshot", "annotated"):
            try:
                png = await _screenshot_png(device)
            except RuntimeError as e:
                parts.append(f"Error capturing screenshot: {e}")
            else:
                if mode == "annotated":
                    jpeg, _ = await asyncio.to_thread(vision.annotate, png, selected or [])
                    path = screenshot_path(device, prefix="marked", ext="jpg")
                    with open(path, "wb") as f:
                        f.write(jpeg)
                    parts.append(
                        f"Annotated screenshot: {path}\n"
                        "Every listed element is boxed on it and labelled with its number. "
                        f'To look at it, call vision_load with paths ["{path}"] and ask what you need '
                        "(for example which numbered element is the search icon); then act on that number."
                    )
                else:
                    path = screenshot_path(device)
                    with open(path, "wb") as f:
                        f.write(png)
                    parts.append(f"Screenshot saved: {path} ({len(png) / 1024:.1f} KB)")

        if not parts:
            parts.append(f"Error: unknown mode '{mode}'. Use xml, annotated or screenshot.")
        return Response(message="\n\n".join(parts), break_loop=False)
