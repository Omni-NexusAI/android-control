"""Interactive input from the live preview (tap / swipe / keys / text).

Coordinates arrive normalized (0..1) relative to the frame the browser shows,
with that frame's width/height so portrait/landscape maps correctly. Taps and
swipes go through the scrcpy control socket when a stream is live (lowest
latency) and through `adb shell input` otherwise. Inputs are captured by an
active workflow recording on the device.
"""

import asyncio

from helpers.api import ApiHandler, Request, Response
from usr.plugins.droidclaw.helpers import scrcpy_stream, workflow_engine
from usr.plugins.droidclaw.helpers.adb_backend import resolve_device, run_adb_async
from usr.plugins.droidclaw.helpers.vision import escape_input_text

KEYS = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "power": "KEYCODE_POWER",
    "wakeup": "KEYCODE_WAKEUP",
    "enter": "KEYCODE_ENTER",
    "delete": "KEYCODE_DEL",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
}

_sizes: dict[str, tuple[int, int]] = {}


async def _device_size(serial: str) -> tuple[int, int]:
    if serial not in _sizes:
        result = await run_adb_async(["shell", "wm", "size"], device=serial, timeout=6)
        size = (0, 0)
        for line in (result.get("stdout") or "").splitlines():
            if "size" in line:
                try:
                    w, h = line.split(":")[-1].strip().lower().split("x")
                    size = (int(w), int(h))
                except ValueError:
                    continue
        if not size[0]:
            return size
        _sizes[serial] = size
    return _sizes[serial]


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


class Control(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        action = str(input.get("action") or "").lower()
        resolution = await asyncio.to_thread(resolve_device, input.get("device") or "")
        serial = resolution.get("resolved_device") or ""
        if not serial:
            return {"success": False, "message": "No connected device"}

        if action in KEYS or action == "key":
            keycode = KEYS.get(action) or str(input.get("key") or "")
            step = await workflow_engine.prepare_step(serial, "key", {"key": keycode})
            result = await run_adb_async(["shell", "input", "keyevent", keycode], device=serial, timeout=8)
            return self._done(result, serial, step, keycode)

        if action == "text":
            text = str(input.get("text") or "")
            if not text or not text.isascii():
                return {"success": False, "message": "Only non-empty ASCII text can be typed over adb"}
            step = await workflow_engine.prepare_step(serial, "type", {"text": text})
            result = await run_adb_async(["shell", "input", "text", escape_input_text(text)], device=serial, timeout=15)
            return self._done(result, serial, step, "typed")

        if action not in ("tap", "longpress", "swipe"):
            return {"success": False, "message": f"Unknown action '{action}'"}

        nx, ny = _clamp(_num(input.get("x"))), _clamp(_num(input.get("y")))
        nx2, ny2 = _clamp(_num(input.get("x2"), nx)), _clamp(_num(input.get("y2"), ny))
        frame_w, frame_h = _num(input.get("frame_w")), _num(input.get("frame_h"))
        dev_w, dev_h = await _device_size(serial)
        if frame_w and frame_h and dev_w and (frame_w > frame_h) != (dev_w > dev_h):
            dev_w, dev_h = dev_h, dev_w  # rotated
        x, y = int(nx * max(dev_w - 1, 0)), int(ny * max(dev_h - 1, 0))
        x2, y2 = int(nx2 * max(dev_w - 1, 0)), int(ny2 * max(dev_h - 1, 0))
        duration = max(0.05, min(_num(input.get("duration"), 0.3), 3.0))

        if action == "swipe":
            step = await workflow_engine.prepare_step(
                serial, "swipe", {"x1": x, "y1": y, "x2": x2, "y2": y2, "duration": int(duration * 1000)}
            )
        else:
            step = await workflow_engine.prepare_step(serial, action, {"x": x, "y": y})

        session = await asyncio.to_thread(scrcpy_stream.running_session, serial)
        if session is not None:
            if action == "tap":
                ok = await asyncio.to_thread(session.tap, nx, ny)
            elif action == "longpress":
                ok = await asyncio.to_thread(session.tap, nx, ny, 0.8)
            else:
                ok = await asyncio.to_thread(session.swipe, nx, ny, nx2, ny2, duration)
            if ok:
                workflow_engine.commit_step(serial, step)
                return {"success": True, "via": "stream", "x": x, "y": y}

        if action == "tap":
            args = ["shell", "input", "tap", str(x), str(y)]
        elif action == "longpress":
            args = ["shell", "input", "swipe", str(x), str(y), str(x), str(y), "800"]
        else:
            args = ["shell", "input", "swipe", str(x), str(y), str(x2), str(y2), str(int(duration * 1000))]
        result = await run_adb_async(args, device=serial, timeout=10)
        response = self._done(result, serial, step, action)
        response.update({"via": "adb", "x": x, "y": y})
        return response

    @staticmethod
    def _done(result: dict, serial: str, step, label: str) -> dict:
        if result.get("returncode") == 0:
            workflow_engine.commit_step(serial, step)
            return {"success": True, "message": label}
        return {"success": False, "message": result.get("stderr") or result.get("output") or "adb error"}
