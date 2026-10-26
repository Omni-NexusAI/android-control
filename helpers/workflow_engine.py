"""Record and replay Android Control workflows without an LLM.

Recording
---------
While a recording is active for a device, every input that reaches that
device through Android Control is captured: agent tool calls (``adb_tools``),
``droidclaw_run`` steps, panel quick actions and taps/swipes on the live
preview. Taps are stored with the UI element under the finger (resource id,
text, description, class, relative position) so replay can find the element
again even if it moved, the list scrolled a little, or the phone has a
different screen size.

Playback
--------
For each step the player waits for the recorded element to appear (polling
the UI dump), taps its *current* center, and falls back to scaled coordinates
only for steps that never had an identifiable element. Playback stops at the
first failed step unless ``continue_on_error`` is set, and returns a per-step
report. It is fully async and never blocks the Agent Zero event loop.

Storage: ``data/workflows/<slug>.json`` inside the plugin (git-ignored).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from usr.plugins.droidclaw.helpers.adb_backend import run_adb_async, run_adb_result
from usr.plugins.droidclaw.helpers.ui_parser import dump_ui_xml, parse_ui_xml

logger = logging.getLogger("droidclaw")

FORMAT_VERSION = 2
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = _PLUGIN_DIR / "data" / "workflows"

STEP_ACTIONS = {"tap", "longpress", "swipe", "type", "key", "launch", "wait", "shell"}
MIN_DELAY_MS = 300
MAX_DELAY_MS = 5000
DEFAULT_TARGET_TIMEOUT = 8.0


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug[:60] or "workflow"


def _path(name: str) -> Path:
    return WORKFLOWS_DIR / f"{slugify(name)}.json"


def save_workflow(workflow: dict) -> Path:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    workflow = dict(workflow)
    workflow["version"] = FORMAT_VERSION
    workflow["slug"] = slugify(workflow.get("name", ""))
    workflow["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path = _path(workflow["name"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_workflow(name: str) -> dict | None:
    path = _path(name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_workflows() -> list[dict]:
    if not WORKFLOWS_DIR.is_dir():
        return []
    items = []
    for path in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append(
            {
                "name": data.get("name", path.stem),
                "slug": path.stem,
                "description": data.get("description", ""),
                "steps": len(data.get("steps", [])),
                "created": data.get("created", ""),
                "device_model": (data.get("device") or {}).get("model", ""),
            }
        )
    return items


def delete_workflow(name: str) -> bool:
    path = _path(name)
    if path.is_file():
        path.unlink()
        return True
    return False


def describe_step(step: dict) -> str:
    action = step.get("action")
    params = step.get("params", {})
    target = step.get("target") or {}
    label = target.get("text") or target.get("content_desc") or (target.get("resource_id") or "").split("/")[-1]
    if action in ("tap", "longpress"):
        return f"{action} " + (f'"{label}"' if label else f"({params.get('x')},{params.get('y')})")
    if action == "swipe":
        return f"swipe ({params.get('x1')},{params.get('y1')})->({params.get('x2')},{params.get('y2')})"
    if action == "type":
        return f'type "{params.get("text", "")[:40]}"'
    if action == "key":
        return f"key {params.get('key')}"
    if action == "launch":
        return f"launch {params.get('package')}"
    if action == "wait":
        return f"wait {params.get('seconds')}s"
    if action == "shell":
        return f"shell {params.get('command', '')[:40]}"
    return str(action)


# ---------------------------------------------------------------------------
# Element targeting
# ---------------------------------------------------------------------------

def _rect(elem: dict) -> list[int]:
    return list(elem.get("rect") or [0, 0, 0, 0])


def _screen(elements: list[dict]) -> tuple[int, int]:
    width = max((_rect(e)[2] for e in elements), default=0)
    height = max((_rect(e)[3] for e in elements), default=0)
    return width, height


def element_at(elements: list[dict], x: int, y: int) -> dict | None:
    """Smallest identifiable element containing (x, y)."""
    best = None
    best_area = None
    for elem in elements:
        x1, y1, x2, y2 = _rect(elem)
        if not (x1 <= x < x2 and y1 <= y < y2):
            continue
        identifiable = elem.get("resource_id") or elem.get("text") or elem.get("content_desc")
        if not identifiable:
            continue
        area = (x2 - x1) * (y2 - y1)
        # Prefer clickable elements of similar size: a label inside a button
        # and the button itself are both fine, a full-screen container is not.
        if best is None or area < best_area or (area == best_area and elem.get("clickable")):
            best, best_area = elem, area
    return best


def target_for(elements: list[dict], x: int, y: int) -> dict | None:
    elem = element_at(elements, x, y)
    width, height = _screen(elements)
    if elem is None:
        return None
    x1, y1, x2, y2 = _rect(elem)
    return {
        "resource_id": elem.get("resource_id", ""),
        "text": elem.get("text", ""),
        "content_desc": elem.get("content_desc", ""),
        "class": elem.get("class", ""),
        "package": elem.get("package", ""),
        # Where inside the element the tap landed, and where the element was.
        "offset": [round((x - x1) / max(1, x2 - x1), 3), round((y - y1) / max(1, y2 - y1), 3)],
        "rel_center": [round((x1 + x2) / 2 / max(1, width), 4), round((y1 + y2) / 2 / max(1, height), 4)],
    }


def find_target(elements: list[dict], target: dict) -> dict | None:
    """Best current element for a recorded target, or None."""
    width, height = _screen(elements)
    best = None
    best_score = 0.0
    for elem in elements:
        score = 0.0
        if target.get("resource_id"):
            if elem.get("resource_id") == target["resource_id"]:
                score += 5
            elif elem.get("resource_id"):
                continue  # a different id is a different element
        if target.get("text"):
            if elem.get("text") == target["text"]:
                score += 4
            elif target["text"].lower() in (elem.get("text") or "").lower():
                score += 1.5
        if target.get("content_desc"):
            if elem.get("content_desc") == target["content_desc"]:
                score += 4
            elif target["content_desc"].lower() in (elem.get("content_desc") or "").lower():
                score += 1.5
        if score == 0:
            continue
        if target.get("class") and elem.get("class") == target["class"]:
            score += 0.5
        # Several matches (e.g. same id in a list): prefer the one closest to
        # where it was recorded.
        rel = target.get("rel_center")
        if rel and width and height:
            x1, y1, x2, y2 = _rect(elem)
            dx = (x1 + x2) / 2 / width - rel[0]
            dy = (y1 + y2) / 2 / height - rel[1]
            score -= min(1.0, (dx * dx + dy * dy) ** 0.5 * 2)
        if score > best_score:
            best, best_score = elem, score
    # Text-only partial matches are too weak to act on.
    return best if best_score >= 2.5 else None


def point_in(elem: dict, offset=None) -> tuple[int, int]:
    x1, y1, x2, y2 = _rect(elem)
    ox, oy = offset or (0.5, 0.5)
    # Stay inside the element even if the recorded offset was on its edge.
    ox, oy = min(max(ox, 0.1), 0.9), min(max(oy, 0.1), 0.9)
    return int(x1 + (x2 - x1) * ox), int(y1 + (y2 - y1) * oy)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

@dataclass
class Recording:
    name: str
    device: str
    description: str = ""
    steps: list = field(default_factory=list)
    started: float = field(default_factory=time.time)
    last_step: float = field(default_factory=time.time)
    screen: list = field(default_factory=list)
    model: str = ""
    source: str = ""


_rec_lock = threading.Lock()
_recordings: dict[str, Recording] = {}


def _device_info(device: str) -> tuple[list, str]:
    size = [0, 0]
    result = run_adb_result(["shell", "wm", "size"], device=device, timeout=6)
    for line in (result.get("stdout") or "").splitlines():
        if "size" in line:
            try:
                w, h = line.split(":")[-1].strip().lower().split("x")
                size = [int(w), int(h)]
            except ValueError:
                continue
    model = run_adb_result(["shell", "getprop", "ro.product.model"], device=device, timeout=6)
    return size, (model.get("stdout") or "").strip()


def start_recording(name: str, device: str, description: str = "", source: str = "") -> dict:
    if not name:
        return {"success": False, "message": "name is required"}
    if not device:
        return {"success": False, "message": "device is required"}
    with _rec_lock:
        if device in _recordings:
            current = _recordings[device]
            return {"success": False, "message": f"Already recording '{current.name}' on {device}"}
        _recordings[device] = Recording(name=name, device=device, description=description, source=source)
    try:
        size, model = _device_info(device)
    except Exception:
        size, model = [0, 0], ""
    with _rec_lock:
        rec = _recordings.get(device)
        if rec:
            rec.screen, rec.model = size, model
    exists = load_workflow(name) is not None
    return {
        "success": True,
        "message": f"Recording '{name}' on {device}." + (" Saving will replace the existing workflow." if exists else ""),
        "device": device,
    }


def recording_for(device: str) -> Recording | None:
    with _rec_lock:
        return _recordings.get(device)


def recording_status() -> list[dict]:
    with _rec_lock:
        return [
            {"name": r.name, "device": r.device, "steps": len(r.steps), "elapsed": round(time.time() - r.started, 1)}
            for r in _recordings.values()
        ]


def stop_recording(device: str, save: bool = True) -> dict:
    with _rec_lock:
        rec = _recordings.pop(device, None)
    if rec is None:
        return {"success": False, "message": f"No recording in progress on {device}"}
    if not save:
        return {"success": True, "message": f"Discarded recording '{rec.name}'", "steps": len(rec.steps)}
    if not rec.steps:
        return {"success": False, "message": f"Recording '{rec.name}' captured no steps; nothing saved."}
    workflow = {
        "name": rec.name,
        "description": rec.description,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(rec.started)),
        "device": {"serial": rec.device, "model": rec.model, "size": rec.screen},
        "steps": rec.steps,
    }
    path = save_workflow(workflow)
    return {"success": True, "message": f"Saved '{rec.name}' with {len(rec.steps)} steps", "path": str(path), "steps": len(rec.steps)}


def _append(device: str, step: dict) -> bool:
    with _rec_lock:
        rec = _recordings.get(device)
        if rec is None:
            return False
        now = time.time()
        if rec.steps:
            gap = int((now - rec.last_step) * 1000)
            rec.steps[-1]["delay_ms"] = max(MIN_DELAY_MS, min(MAX_DELAY_MS, gap))
        step.setdefault("delay_ms", 800)
        rec.steps.append(step)
        rec.last_step = now
    logger.info("Workflow recorder (%s): %s", device, describe_step(step))
    return True


def drop_last_steps(device: str, count: int = 1) -> int:
    """Remove the most recent steps (e.g. an action that changed nothing)."""
    with _rec_lock:
        rec = _recordings.get(device)
        if rec is None:
            return 0
        dropped = 0
        while rec.steps and dropped < count:
            rec.steps.pop()
            dropped += 1
        return dropped


def is_recording(device: str) -> bool:
    with _rec_lock:
        return device in _recordings


async def _elements_now(device: str) -> list[dict]:
    xml = await asyncio.to_thread(dump_ui_xml, device)
    return parse_ui_xml(xml) if xml else []


async def prepare_step(device: str, action: str, params: dict, elements: list[dict] | None = None) -> dict | None:
    """Build a step for ``device`` if it is being recorded (else None).

    Call *before* executing the input so taps capture the element under the
    finger from the pre-tap screen; pass the result to ``commit_step`` once the
    input succeeded.
    """
    if not device or not is_recording(device) or action not in STEP_ACTIONS:
        return None
    step = {"action": action, "params": dict(params)}
    if action in ("tap", "longpress"):
        if elements is None:
            elements = await _elements_now(device)
        target = target_for(elements, int(params["x"]), int(params["y"]))
        if target:
            step["target"] = target
    return step


def commit_step(device: str, step: dict | None) -> bool:
    if not step:
        return False
    return _append(device, step)


async def record_input(device: str, action: str, params: dict, elements: list[dict] | None = None) -> bool:
    """prepare_step + commit_step for inputs that already succeeded."""
    return commit_step(device, await prepare_step(device, action, params, elements))


def record_canonical(device: str, action: dict, raw_elements: list[dict] | None = None, executed: dict | None = None) -> int:
    """Record a canonical execution-loop action; returns the steps added."""
    if not device or not is_recording(device):
        return 0
    executed = executed or {}
    name = action.get("action")
    steps = []
    if name in ("tap", "longpress") and executed.get("center"):
        x, y = executed["center"]
        step = {"action": name, "params": {"x": int(x), "y": int(y)}}
        target = target_for(raw_elements or [], int(x), int(y))
        if target:
            step["target"] = target
        steps.append(step)
    elif name == "type":
        if executed.get("focus"):
            x, y = executed["focus"]
            focus = {"action": "tap", "params": {"x": int(x), "y": int(y)}}
            target = target_for(raw_elements or [], int(x), int(y))
            if target:
                focus["target"] = target
            steps.append(focus)
        steps.append({"action": "type", "params": {"text": action.get("text", ""), "submit": bool(action.get("submit"))}})
    elif name == "press":
        steps.append({"action": "key", "params": {"key": action.get("key", "")}})
    elif name == "launch":
        steps.append({"action": "launch", "params": {"package": action.get("package", "")}})
    elif name == "wait":
        steps.append({"action": "wait", "params": {"seconds": float(action.get("seconds", 1))}})
    elif name in ("swipe", "scroll") and executed.get("swipe"):
        x1, y1, x2, y2 = executed["swipe"]
        steps.append({"action": "swipe", "params": {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": 350}})
    return sum(1 for step in steps if _append(device, step))


_KEY_ALIASES = {
    "home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "enter": "KEYCODE_ENTER", "recent": "KEYCODE_APP_SWITCH",
    "recents": "KEYCODE_APP_SWITCH", "wakeup": "KEYCODE_WAKEUP", "delete": "KEYCODE_DEL", "menu": "KEYCODE_MENU",
}


async def prepare_adb_args(device: str, args: list[str]) -> dict | None:
    """prepare_step for a raw ``adb shell input ...`` / ``monkey`` command."""
    if not device or not is_recording(device):
        return None
    words = []
    for arg in args:
        words.extend(str(arg).split())
    if words[:1] != ["shell"]:
        return None
    words = words[1:]
    try:
        if words[:2] == ["input", "tap"]:
            return await prepare_step(device, "tap", {"x": int(float(words[2])), "y": int(float(words[3]))})
        if words[:2] == ["input", "swipe"]:
            x1, y1, x2, y2 = (int(float(v)) for v in words[2:6])
            duration = int(words[6]) if len(words) > 6 else 300
            if abs(x1 - x2) < 10 and abs(y1 - y2) < 10 and duration >= 500:
                return await prepare_step(device, "longpress", {"x": x1, "y": y1})
            return await prepare_step(device, "swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration})
        if words[:2] == ["input", "keyevent"]:
            return await prepare_step(device, "key", {"key": words[-1]})
        if words[:2] == ["input", "text"]:
            text = " ".join(words[2:]).replace("%s", " ")
            text = re.sub(r"\\(.)", r"\1", text)
            return await prepare_step(device, "type", {"text": text})
        if words[:1] == ["monkey"] and "-p" in words:
            return await prepare_step(device, "launch", {"package": words[words.index("-p") + 1]})
    except (IndexError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

async def _adb(device: str, args: list[str], timeout: int = 15) -> dict:
    return await run_adb_async(args, device=device, timeout=timeout)


async def _device_size(device: str) -> list[int]:
    result = await _adb(device, ["shell", "wm", "size"], timeout=6)
    size = [0, 0]
    for line in (result.get("stdout") or "").splitlines():
        if "size" in line:
            try:
                w, h = line.split(":")[-1].strip().lower().split("x")
                size = [int(w), int(h)]
            except ValueError:
                continue
    return size


def _scale(value: int, src: int, dst: int) -> int:
    if not src or not dst:
        return int(value)
    return int(round(value * dst / src))


@dataclass
class PlaybackRun:
    run_id: str
    name: str
    device: str
    total: int
    status: str = "running"
    current: int = 0
    results: list = field(default_factory=list)
    message: str = ""
    started: float = field(default_factory=time.time)
    cancel: bool = False

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "device": self.device,
            "status": self.status,
            "current": self.current,
            "total": self.total,
            "results": self.results,
            "message": self.message,
            "elapsed": round(time.time() - self.started, 1),
        }


_runs: dict[str, PlaybackRun] = {}


def get_run(run_id: str) -> dict | None:
    run = _runs.get(run_id)
    return run.to_dict() if run else None


def cancel_run(run_id: str) -> bool:
    run = _runs.get(run_id)
    if run and run.status == "running":
        run.cancel = True
        return True
    return False


async def play_workflow(
    workflow: dict,
    device: str,
    continue_on_error: bool = False,
    speed: float = 1.0,
    target_timeout: float = DEFAULT_TARGET_TIMEOUT,
    run: PlaybackRun | None = None,
) -> dict:
    """Replay ``workflow`` on ``device``. Returns the run report dict."""
    steps = workflow.get("steps", [])
    run = run or PlaybackRun(run_id=uuid.uuid4().hex[:10], name=workflow.get("name", ""), device=device, total=len(steps))
    _runs[run.run_id] = run
    speed = max(0.25, min(float(speed or 1.0), 4.0))
    src_w, src_h = ((workflow.get("device") or {}).get("size") or [0, 0])[:2]

    try:
        await _adb(device, ["shell", "input", "keyevent", "KEYCODE_WAKEUP"], timeout=6)
        dst_w, dst_h = await _device_size(device)
        for index, step in enumerate(steps, start=1):
            if run.cancel:
                run.status = "cancelled"
                run.message = f"Cancelled before step {index}"
                break
            run.current = index
            if index > 1:
                delay = steps[index - 2].get("delay_ms", 800) / 1000.0 / speed
                await asyncio.sleep(max(0.15, delay))
            started = time.time()
            ok, detail = await _play_step(step, device, (src_w, src_h), (dst_w, dst_h), target_timeout)
            run.results.append(
                {
                    "step": index,
                    "action": describe_step(step),
                    "ok": ok,
                    "detail": detail,
                    "elapsed": round(time.time() - started, 2),
                }
            )
            if not ok and not continue_on_error:
                run.status = "failed"
                run.message = f"Step {index} failed: {detail}"
                break
        else:
            failed = [r for r in run.results if not r["ok"]]
            run.status = "completed_with_errors" if failed else "completed"
            run.message = (
                f"Completed {len(steps)} steps" + (f", {len(failed)} failed" if failed else "")
            )
    except Exception as exc:  # pragma: no cover - defensive
        run.status = "failed"
        run.message = f"Playback error: {exc}"
        logger.exception("Workflow playback failed")
    return run.to_dict()


def start_playback(workflow: dict, device: str, **kwargs) -> str:
    """Start playback in the background; poll with get_run(run_id)."""
    run = PlaybackRun(run_id=uuid.uuid4().hex[:10], name=workflow.get("name", ""), device=device, total=len(workflow.get("steps", [])))
    _runs[run.run_id] = run
    asyncio.get_running_loop().create_task(play_workflow(workflow, device, run=run, **kwargs))
    return run.run_id


async def _wait_for_target(device: str, target: dict, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while True:
        elements = await _elements_now(device)
        found = find_target(elements, target)
        if found or time.monotonic() >= deadline:
            return found
        await asyncio.sleep(0.5)


def _check(result: dict) -> tuple[bool, str]:
    if result.get("returncode") == 0:
        return True, ""
    return False, (result.get("stderr") or result.get("output") or "adb error")[:200]


async def _play_step(step: dict, device: str, src: tuple, dst: tuple, target_timeout: float) -> tuple[bool, str]:
    from usr.plugins.droidclaw.helpers.vision import escape_input_text

    action = step.get("action")
    params = step.get("params") or {}
    target = step.get("target")

    if action in ("tap", "longpress"):
        if target:
            elem = await _wait_for_target(device, target, target_timeout)
            if elem is None:
                label = target.get("text") or target.get("content_desc") or target.get("resource_id")
                return False, f'element "{label}" did not appear within {target_timeout:g}s'
            x, y = point_in(elem, target.get("offset"))
            how = "matched element"
        else:
            x = _scale(int(params.get("x", 0)), src[0], dst[0])
            y = _scale(int(params.get("y", 0)), src[1], dst[1])
            how = "scaled coordinates"
        if action == "tap":
            ok, err = _check(await _adb(device, ["shell", "input", "tap", str(x), str(y)]))
        else:
            ok, err = _check(await _adb(device, ["shell", "input", "swipe", str(x), str(y), str(x), str(y), "800"]))
        return ok, (f"{how} at ({x},{y})" if ok else err)

    if action == "swipe":
        coords = [
            _scale(int(params.get("x1", 0)), src[0], dst[0]),
            _scale(int(params.get("y1", 0)), src[1], dst[1]),
            _scale(int(params.get("x2", 0)), src[0], dst[0]),
            _scale(int(params.get("y2", 0)), src[1], dst[1]),
        ]
        ok, err = _check(await _adb(device, ["shell", "input", "swipe", *map(str, coords), str(int(params.get("duration", 300)))]))
        return ok, ("swiped" if ok else err)

    if action == "type":
        text = str(params.get("text", ""))
        if not text.isascii():
            return False, "adb text input only supports ASCII characters"
        ok, err = _check(await _adb(device, ["shell", "input", "text", escape_input_text(text)], timeout=20))
        if ok and params.get("submit"):
            ok, err = _check(await _adb(device, ["shell", "input", "keyevent", "KEYCODE_ENTER"]))
        return ok, ("typed" if ok else err)

    if action == "key":
        key = str(params.get("key", ""))
        keycode = _KEY_ALIASES.get(key.lower(), key if key.upper().startswith("KEYCODE_") or key.isdigit() else f"KEYCODE_{key.upper()}")
        ok, err = _check(await _adb(device, ["shell", "input", "keyevent", keycode]))
        return ok, (keycode if ok else err)

    if action == "launch":
        package = str(params.get("package", ""))
        result = await _adb(device, ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])
        ok, err = _check(result)
        if ok and "No activities found" in (result.get("output") or ""):
            ok, err = False, f"{package} is not installed or has no launcher activity"
        if ok:
            await asyncio.sleep(1.0)
        return ok, (f"launched {package}" if ok else err)

    if action == "wait":
        seconds = min(float(params.get("seconds", 1)), 60.0)
        await asyncio.sleep(seconds)
        return True, f"waited {seconds:g}s"

    if action == "shell":
        command = str(params.get("command", ""))
        ok, err = _check(await _adb(device, ["shell", command], timeout=30))
        return ok, ("ran" if ok else err)

    return False, f"unknown step action '{action}'"
