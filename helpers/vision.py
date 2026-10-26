"""Screen understanding helpers shared by the task loop and the agent tools.

Why vision used to *hurt*: the model got a full-resolution, unannotated PNG
next to a text list it had no way to relate to the picture, was told to use
coordinates from the list, and providers silently downscale images, so any
position read off the picture was in the wrong coordinate space.

The approach here is "set-of-marks": the exact elements listed in the text
prompt are drawn on the screenshot as numbered boxes. The model picks an
element *number*; the executor taps that element's real center in device
pixels. Image scaling therefore can never produce a wrong tap, and the image
adds what the XML lacks (icons without labels, visual state, layout, canvas
and web content) instead of competing with it.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

# Distinct, high-contrast box colours (cycled per element number).
_PALETTE = [
    (230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180),
    (70, 240, 240), (240, 50, 230), (210, 245, 60), (0, 128, 128), (170, 110, 40),
    (128, 0, 0), (0, 0, 128), (128, 128, 0), (255, 225, 25),
]

_EDITABLE_HINTS = ("EditText", "AutoCompleteTextView", "SearchView")


@dataclass
class ScreenElement:
    number: int
    rect: list[int]
    center: tuple[int, int]
    label: str
    kind: str
    resource_id: str = ""
    flags: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def describe(self) -> str:
        parts = [f"[{self.number}]", self.kind]
        if self.label:
            parts.append(f'"{_clip(self.label, 60)}"')
        if self.resource_id:
            parts.append(f"id={self.resource_id.split('/')[-1]}")
        parts.append(f"at ({self.center[0]},{self.center[1]})")
        if self.flags:
            parts.append(" ".join(self.flags))
        return " ".join(parts)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rect(elem: dict) -> list[int]:
    rect = elem.get("rect")
    if rect and len(rect) == 4:
        return list(rect)
    try:
        from usr.plugins.droidclaw.helpers.ui_parser import parse_rect
    except Exception:  # pragma: no cover
        return [0, 0, 0, 0]
    return parse_rect(elem.get("bounds", ""))


def _kind(elem: dict) -> str:
    cls = (elem.get("class") or "").split(".")[-1]
    if any(h in cls for h in _EDITABLE_HINTS):
        return "input"
    if elem.get("checkable"):
        return "toggle"
    if "Button" in cls:
        return "button"
    if "Image" in cls:
        return "image"
    if elem.get("scrollable"):
        return "list"
    if elem.get("clickable"):
        return "tappable"
    return "text"


def _flags(elem: dict) -> list[str]:
    flags = []
    if elem.get("checkable"):
        flags.append("checked" if elem.get("checked") else "unchecked")
    if elem.get("focused"):
        flags.append("focused")
    if elem.get("selected"):
        flags.append("selected")
    if elem.get("scrollable"):
        flags.append("scrollable")
    if elem.get("enabled") is False:
        flags.append("disabled")
    if elem.get("password"):
        flags.append("password")
    return flags


def _label(elem: dict) -> str:
    return (elem.get("text") or elem.get("content_desc") or elem.get("hint") or "").strip()


def _interesting(elem: dict) -> bool:
    return bool(
        _label(elem)
        or elem.get("clickable")
        or elem.get("long_clickable")
        or elem.get("scrollable")
        or elem.get("checkable")
        or any(h in (elem.get("class") or "") for h in _EDITABLE_HINTS)
    )


def select_elements(elements: list[dict], max_elements: int = 40, screen: tuple[int, int] | None = None) -> list[ScreenElement]:
    """Pick the elements worth showing, numbered 1..N in reading order."""
    width, height = screen or (0, 0)
    candidates = []
    seen = set()
    for elem in elements or []:
        if not _interesting(elem):
            continue
        rect = _rect(elem)
        x1, y1, x2, y2 = rect
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        if width and height and (x2 <= 0 or y2 <= 0 or x1 >= width or y1 >= height):
            continue
        label = _label(elem)
        key = (tuple(rect), label)
        if key in seen:
            continue
        seen.add(key)
        score = 0
        if label:
            score += 3
        if elem.get("clickable") or elem.get("long_clickable"):
            score += 2
        if elem.get("checkable") or _kind(elem) == "input":
            score += 2
        if elem.get("resource_id"):
            score += 1
        if elem.get("scrollable"):
            score += 1
        candidates.append((score, rect, elem))

    candidates.sort(key=lambda item: item[0], reverse=True)
    chosen = candidates[: max(1, max_elements)]
    # Reading order: top-to-bottom in coarse rows, then left-to-right.
    chosen.sort(key=lambda item: ((item[1][1] + item[1][3]) // 2 // 40, item[1][0]))

    result = []
    for number, (_, rect, elem) in enumerate(chosen, start=1):
        center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        result.append(
            ScreenElement(
                number=number,
                rect=rect,
                center=center,
                label=_label(elem),
                kind=_kind(elem),
                resource_id=elem.get("resource_id", ""),
                flags=_flags(elem),
                raw=elem,
            )
        )
    return result


def format_elements(selected: list[ScreenElement]) -> str:
    return "\n".join(el.describe() for el in selected)


def screen_signature(selected: list[ScreenElement]) -> tuple:
    """Cheap identity of what is on screen, for "did my action change anything"."""
    return tuple((el.label, el.kind, tuple(el.rect), tuple(el.flags)) for el in selected)


def needs_vision(selected: list[ScreenElement], raw_count: int) -> str:
    """Return a reason to attach the screenshot in ``fallback`` mode, or ''."""
    if raw_count == 0:
        return "no UI hierarchy available"
    if len(selected) < 3:
        return "very few labelled elements"
    unlabeled = [el for el in selected if not el.label and el.kind in ("tappable", "image", "button")]
    if len(unlabeled) >= max(3, len(selected) // 2):
        return "many unlabelled icons"
    if any(cls in (el.raw.get("class") or "") for el in selected for cls in ("WebView", "SurfaceView", "TextureView")):
        return "web/canvas content"
    return ""


def image_size(image_bytes: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        return img.size


def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def annotate(
    image_bytes: bytes,
    selected: list[ScreenElement],
    max_edge: int = 1280,
    quality: int = 75,
) -> tuple[bytes, tuple[int, int]]:
    """Draw numbered boxes for ``selected`` and return (jpeg_bytes, device_size).

    Boxes are drawn at device resolution, then the image is downscaled so the
    long edge is at most ``max_edge`` pixels. Returns the original (device)
    size so callers can state the coordinate space.
    """
    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(image_bytes)) as src:
        img = src.convert("RGB")
    size = img.size
    scale = min(1.0, max_edge / max(size)) if max_edge else 1.0
    if selected:
        draw = ImageDraw.Draw(img, "RGBA")
        # Label size is chosen so it stays legible after downscaling.
        label_px = max(14, int(22 / max(scale, 0.01)))
        font = _font(label_px)
        line = max(2, int(3 / max(scale, 0.01)))
        for el in selected:
            color = _PALETTE[(el.number - 1) % len(_PALETTE)]
            x1, y1, x2, y2 = el.rect
            draw.rectangle([x1, y1, x2, y2], outline=color + (255,), width=line)
            text = str(el.number)
            tx1, ty1, tx2, ty2 = draw.textbbox((0, 0), text, font=font)
            tw, th = tx2 - tx1, ty2 - ty1
            pad = max(2, label_px // 6)
            bx = min(max(0, x1), size[0] - tw - 2 * pad)
            by = y1 - th - 2 * pad if y1 - th - 2 * pad >= 0 else y1
            draw.rectangle([bx, by, bx + tw + 2 * pad, by + th + 2 * pad], fill=color + (230,))
            draw.text((bx + pad - tx1, by + pad - ty1), text, fill=(255, 255, 255, 255), font=font)
    if scale < 1.0:
        img = img.resize((max(1, int(size[0] * scale)), max(1, int(size[1] * scale))), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue(), size


def element_by_number(selected: list[ScreenElement], number) -> ScreenElement | None:
    try:
        n = int(str(number).strip().lstrip("#[").rstrip("]"))
    except (TypeError, ValueError):
        return None
    for el in selected:
        if el.number == n:
            return el
    return None


def swipe_vector(direction: str, size: tuple[int, int], rect: list[int] | None = None) -> list[int]:
    """Swipe start/end points inside ``rect`` (or the screen) for a direction.

    ``direction`` is the finger direction. To scroll content down (see what is
    below) the finger moves *up*.
    """
    width, height = size
    x1, y1, x2, y2 = rect if rect else (0, 0, width, height)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    dx, dy = int((x2 - x1) * 0.35), int((y2 - y1) * 0.3)
    vectors = {
        "up": [cx, cy + dy, cx, cy - dy],
        "down": [cx, cy - dy, cx, cy + dy],
        "left": [cx + dx, cy, cx - dx, cy],
        "right": [cx - dx, cy, cx + dx, cy],
    }
    return vectors.get(direction, vectors["up"])


_ADB_TEXT_ESCAPES = {
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
    "`": "\\`",
    "$": "\\$",
    "*": "\\*",
    "~": "\\~",
    "\\": "\\\\",
    "#": "\\#",
    "?": "\\?",
    "!": "\\!",
}


def escape_input_text(text: str) -> str:
    """Escape text for ``adb shell input text`` (device shell parses it)."""
    return "".join(_ADB_TEXT_ESCAPES.get(ch, ch) for ch in text)


# ---------------------------------------------------------------------------
# Per-agent element cache (so tools can act on "element N" from the last look)
# ---------------------------------------------------------------------------

_SCREEN_KEY = "droidclaw_screen_elements"
_anon_cache: dict[str, list[dict]] = {}


def remember_elements(agent, serial: str, selected: list[ScreenElement]) -> None:
    rows = [{"number": e.number, "center": list(e.center), "rect": e.rect, "label": e.label} for e in selected]
    if agent is None:
        _anon_cache[serial] = rows
        return
    store = dict(agent.get_data(_SCREEN_KEY) or {}) if hasattr(agent, "get_data") else {}
    store[serial] = rows
    agent.set_data(_SCREEN_KEY, store)


def recall_element(agent, serial: str, number) -> dict | None:
    if agent is None:
        rows = _anon_cache.get(serial) or []
    else:
        rows = (agent.get_data(_SCREEN_KEY) or {}).get(serial) or []
    try:
        n = int(str(number).strip().lstrip("#[").rstrip("]"))
    except (TypeError, ValueError):
        return None
    return next((r for r in rows if r["number"] == n), None)
