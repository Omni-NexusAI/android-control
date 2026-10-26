"""Android Control Execution Loop - Autonomous phone control via LLM-guided actions.

Core loop: observe (UI dump -> numbered elements, optional annotated
screenshot) -> ask LLM for one action -> execute via ADB -> check whether the
screen changed -> repeat.

Device I/O is injected via the constructor so the loop can be exercised
without a phone.
"""

import asyncio
import base64
import json
import logging
import re
import time

from usr.plugins.droidclaw.helpers import vision
from usr.plugins.droidclaw.helpers.action_validator import validate_action as _default_validate

logger = logging.getLogger("droidclaw")

SYSTEM_PROMPT = """You are an Android phone automation agent. You operate a real phone one action at a time to reach the user's goal.

Every turn you receive:
- the goal and the actions you already took, each marked with whether the screen changed afterwards;
- the current screen as a numbered list of UI elements: [number] kind "label" id=... at (x,y) flags;
- sometimes a screenshot of the same screen. When present, every listed element is outlined on it with a coloured box labelled with the SAME number as in the list.

How to use them:
- Act on elements by number: {"action": "tap", "element": 7}. This is always more precise than coordinates.
- Use the screenshot to understand what the list cannot say: unlabelled icons, which item is highlighted, toggle states, images, layout, web or game content. Then act on the matching number.
- Only when the thing you need has no number (e.g. inside a web page or image) use device pixel coordinates {"action": "tap", "x": 540, "y": 1200}. The screen size is given in pixels; the screenshot may be shown smaller, so convert positions to the stated screen size.
- If you need to see the screen and no screenshot was attached, respond {"action": "look"}.

Actions (one per reply):
- tap {"element": N} | {"x": X, "y": Y}
- longpress {"element": N} | {"x": X, "y": Y}
- type {"text": "...", "element": N (optional: field to focus first), "clear": true (optional), "submit": true (optional: press enter)}
- scroll {"direction": "down"} reveals content further down (also up/left/right); add "element": N to scroll inside a specific list
- swipe {"direction": "up"} raw finger swipe (e.g. up from the home screen opens the app drawer)
- press {"key": "back" | "home" | "enter" | "recent" | "notifications" | "delete" | "wakeup"}
- launch {"package": "com.android.settings"}
- wait {"seconds": 2}
- look {}
- done {"summary": "what was achieved"}
- fail {"reason": "why the goal is impossible"}

Rules:
- Check the action history. If an action did not change the screen, do not repeat it; try something else.
- Prefer launch with a package name to open apps when you know it.
- Use done only when the goal is visibly achieved on the current screen.
- Add a short "reason" to every action.

Reply with ONLY one JSON object, for example:
{"action": "tap", "element": 4, "reason": "Open Wi-Fi settings"}"""

# Keycode mapping for press action
_KEYCODES = {
    "home": "KEYCODE_HOME",
    "back": "KEYCODE_BACK",
    "enter": "KEYCODE_ENTER",
    "wakeup": "KEYCODE_WAKEUP",
    "menu": "KEYCODE_MENU",
    "recent": "KEYCODE_APP_SWITCH",
    "recents": "KEYCODE_APP_SWITCH",
    "app_switch": "KEYCODE_APP_SWITCH",
    "delete": "KEYCODE_DEL",
    "backspace": "KEYCODE_DEL",
    "tab": "KEYCODE_TAB",
    "escape": "KEYCODE_ESCAPE",
    "power": "KEYCODE_POWER",
    "search": "KEYCODE_SEARCH",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
}

HISTORY_LIMIT = 8

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _json_objects(text: str):
    """Yield every top-level {...} substring, respecting JSON strings."""
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start:i + 1]
                start = -1


def parse_llm_response(response: str) -> dict:
    """Extract a JSON action from the LLM response. Never raises.

    Handles pure JSON, fenced blocks, prose before/after the JSON and
    <think>...</think> reasoning. When several action objects appear, the last
    one wins (models usually deliberate before the final answer).
    """
    if not response:
        return {"action": "invalid", "error": "empty response"}

    text = _THINK_RE.sub("", str(response))
    if "</think>" in text.lower():  # unopened think block: keep the answer part
        text = text[text.lower().rfind("</think>") + len("</think>"):]
    text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    found = None
    for candidate in _json_objects(text):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and ("action" in parsed or found is None):
            found = parsed
    if found is not None:
        return found

    logger.warning("Failed to parse LLM response as action: %s", text[:200])
    return {"action": "invalid", "error": "no JSON object found in the reply"}


def format_result_summary(result: dict, goal: str) -> str:
    """Format the execution result into a human-readable summary."""
    lines = [
        "Android Control Execution Summary",
        "=" * 50,
        f"Goal: {goal}",
        f"Status: {result['status']}",
        f"Steps: {result['steps']}",
        f"Elapsed: {result['elapsed']}s",
    ]
    if result.get("vision_steps") is not None:
        lines.append(f"Screenshots used: {result['vision_steps']}")
    if result.get("notes"):
        lines.append("Notes: " + "; ".join(result["notes"]))
    lines += [
        "",
        f"Summary: {result['summary']}",
        "",
        "Action Log:",
        "-" * 40,
    ]

    for entry in result.get("actions", []):
        step = entry.get("step", "?")
        action = entry.get("action", "?")
        desc = entry.get("description", "")
        reason = entry.get("reason", "")
        elapsed = entry.get("elapsed", 0)
        outcome = entry.get("outcome", "")
        tail = f" -> {outcome}" if outcome else ""
        lines.append(f"  Step {step}: {action} - {desc or reason}{tail} ({elapsed}s)")

    lines.append("")
    lines.append("=" * 50)
    return "\n".join(lines)


class Observation:
    def __init__(self, raw: list, selected: list, size: tuple):
        self.raw = raw
        self.selected = selected
        self.size = size
        self.signature = vision.screen_signature(selected) if selected else ("empty", len(raw))


class ExecutionLoop:
    """Autonomous execution loop for Android phone control.

    Args:
        device: ADB device serial string.
        llm_client: LLMClient instance (chat / chat_with_vision).
        max_steps: Maximum number of action steps.
        step_delay: Seconds to let the UI settle after each action.
        vision_mode: off | fallback | always.
        max_elements: Maximum UI elements to list (and mark on screenshots).
        stuck_threshold: Consecutive no-change actions before recovery.
        auto_intervene: Perform recovery actions when stuck (else only warn).
        image_max_edge: Long edge (px) of screenshots sent to the model.
        fn_run_adb: run_adb(args, device, timeout) -> str (raises RuntimeError).
        fn_dump_ui: dump_ui(device) -> list[dict].
        fn_wake_device: wake_device(device) -> bool.
        fn_screenshot: screenshot(device) -> PNG bytes.
        fn_validate_action: validate_action(dict) -> dict (canonical action).
    """

    def __init__(
        self,
        device: str,
        llm_client,
        max_steps: int = 30,
        step_delay: float = 2.0,
        vision_mode: str = "off",
        max_elements: int = 40,
        stuck_threshold: int = 3,
        auto_intervene: bool = True,
        image_max_edge: int = 1280,
        fn_run_adb=None,
        fn_dump_ui=None,
        fn_wake_device=None,
        fn_screenshot=None,
        fn_validate_action=None,
        **_legacy,
    ):
        self.device = device
        self.llm_client = llm_client
        self.max_steps = max_steps
        self.step_delay = step_delay
        self.vision_mode = (vision_mode or "off").lower()
        self.max_elements = max_elements
        self.stuck_threshold = max(1, int(stuck_threshold or 3))
        self.auto_intervene = auto_intervene
        self.image_max_edge = image_max_edge

        self._run_adb = fn_run_adb
        self._dump_ui = fn_dump_ui
        self._wake_device = fn_wake_device
        self._screenshot = fn_screenshot
        self._validate_action = fn_validate_action or _default_validate

        self.step_log = []
        self.history = []
        self.no_change_count = 0
        self.recovery_level = 0
        self.vision_steps = 0
        self.vision_disabled_reason = ""
        self.force_look = False
        self.notes = []

    async def _adb(self, args: list, timeout: int = 10) -> str:
        """Run an ADB command asynchronously."""
        return await asyncio.to_thread(self._run_adb, args, self.device, timeout)

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    async def _observe(self) -> Observation:
        try:
            raw = await asyncio.to_thread(self._dump_ui, self.device)
        except Exception as exc:
            logger.error("UI dump failed: %s", exc)
            raw = []
        if not raw and self._wake_device:
            # Screen off / lock screen: wake (no-op if already awake) and retry.
            try:
                await asyncio.to_thread(self._wake_device, self.device)
            except Exception:
                pass
            await asyncio.sleep(1.0)
            try:
                raw = await asyncio.to_thread(self._dump_ui, self.device)
            except Exception:
                raw = []
        size = self._screen_size(raw)
        selected = vision.select_elements(raw, self.max_elements, size)
        return Observation(raw, selected, size)

    def _screen_size(self, raw: list) -> tuple:
        width = height = 0
        for elem in raw or []:
            rect = elem.get("rect") or [0, 0, 0, 0]
            width, height = max(width, rect[2]), max(height, rect[3])
        if width and height:
            self._last_size = (width, height)
            return (width, height)
        return getattr(self, "_last_size", (1080, 2400))

    def _vision_reason(self, obs: Observation) -> str:
        if self.vision_mode == "off" or self.vision_disabled_reason or not self._screenshot:
            return ""
        if self.vision_mode == "always":
            return "always"
        if self.force_look:
            return "requested"
        if self.no_change_count > 0:
            return "last action changed nothing"
        return vision.needs_vision(obs.selected, len(obs.raw))

    async def _image_for(self, obs: Observation):
        try:
            png = await asyncio.to_thread(self._screenshot, self.device)
            jpeg, device_size = await asyncio.to_thread(
                vision.annotate, png, obs.selected, self.image_max_edge
            )
            if not obs.raw:
                obs.size = device_size
            return base64.b64encode(jpeg).decode("ascii")
        except Exception as exc:
            logger.warning("Screenshot for vision failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------

    def _history_text(self) -> str:
        if not self.history:
            return "(none yet)"
        return "\n".join(self.history[-HISTORY_LIMIT:])

    def _user_message(self, goal: str, step: int, obs: Observation, image_reason: str, warning: str) -> str:
        width, height = obs.size
        parts = [
            f"Goal: {goal}",
            f"Step {step} of {self.max_steps}.",
            "",
            "Actions so far:",
            self._history_text(),
        ]
        if warning:
            parts += ["", f"WARNING: {warning}"]
        parts += ["", f"Screen size: {width}x{height} pixels."]
        if obs.selected:
            parts += [f"Elements on screen ({len(obs.selected)}):", vision.format_elements(obs.selected)]
        else:
            parts.append("No UI element list is available for this screen (secure, animating or canvas content).")
        if image_reason:
            parts += [
                "",
                "A screenshot of this screen is attached"
                + (" with each listed element boxed and labelled by its number." if obs.selected else ".")
                + f" (attached because: {image_reason})",
            ]
        parts += ["", "Reply with one JSON action."]
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, goal: str) -> dict:
        start_time = time.time()
        prev_obs = None
        warning = ""

        for step in range(1, self.max_steps + 1):
            step_start = time.time()
            logger.info("=== Step %d/%d ===", step, self.max_steps)

            obs = await self._observe()

            # Did the previous action do anything?
            if prev_obs is not None and self.history:
                changed = obs.signature != prev_obs.signature
                self._mark_last_outcome("screen changed" if changed else "no visible change")
                if changed:
                    self.no_change_count = 0
                    self.recovery_level = 0
                else:
                    self.no_change_count += 1
                    if self.no_change_count >= self.stuck_threshold:
                        warning = await self._handle_stuck(step)
                        obs = await self._observe()

            image_reason = self._vision_reason(obs)
            image_b64 = await self._image_for(obs) if image_reason else None
            if image_reason and not image_b64:
                image_reason = ""
            self.force_look = False

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._user_message(goal, step, obs, image_reason, warning)},
            ]
            warning = ""

            llm_response = await self._ask(messages, image_b64)
            if image_b64:
                self.vision_steps += 1

            action = self._validate_action(parse_llm_response(llm_response))
            action_name = action.get("action", "invalid")
            reason = action.get("reason", "")
            logger.info("Step %d action: %s | reason: %s", step, action_name, reason)

            if action_name == "done":
                summary = action.get("summary") or reason or goal
                self.step_log.append({"step": step, "action": "done", "reason": summary, "elapsed": round(time.time() - step_start, 1)})
                return self._result("completed", step, start_time, summary)

            if action_name == "fail":
                summary = action.get("reason") or "Model reported the goal cannot be completed"
                self.step_log.append({"step": step, "action": "fail", "reason": summary, "elapsed": round(time.time() - step_start, 1)})
                return self._result("failed", step, start_time, summary)

            if action_name == "invalid":
                error = action.get("error", "invalid action")
                self.history.append(f"{len(self.history) + 1}. (invalid reply: {error})")
                self.step_log.append({"step": step, "action": "invalid", "description": error, "elapsed": round(time.time() - step_start, 1)})
                prev_obs = None  # nothing executed, nothing to compare
                continue

            if action_name == "look":
                self.force_look = True
                self.history.append(f"{len(self.history) + 1}. look (requested a screenshot)")
                self.step_log.append({"step": step, "action": "look", "reason": reason, "elapsed": round(time.time() - step_start, 1)})
                prev_obs = None
                continue

            description = await self._execute_action(action, obs)
            self.history.append(f"{len(self.history) + 1}. {description}" + (f" - {reason}" if reason else ""))
            self.step_log.append({
                "step": step,
                "action": action_name,
                "description": description,
                "reason": reason,
                "elements": len(obs.selected),
                "vision": bool(image_b64),
                "elapsed": round(time.time() - step_start, 1),
            })
            prev_obs = obs

            if description.startswith("error("):
                prev_obs = None
                self._mark_last_outcome("failed")
            if action_name != "wait" and step < self.max_steps:
                await asyncio.sleep(self.step_delay)

        return self._result(
            "max_steps_reached", self.max_steps, start_time,
            f"Reached max steps ({self.max_steps}) without completing goal.",
        )

    def _result(self, status: str, steps: int, start_time: float, summary: str) -> dict:
        notes = list(self.notes)
        if self.vision_disabled_reason:
            notes.append(f"vision turned off mid-run: {self.vision_disabled_reason}")
        return {
            "status": status,
            "steps": steps,
            "elapsed": round(time.time() - start_time, 1),
            "actions": self.step_log,
            "summary": summary,
            "vision_steps": self.vision_steps,
            "notes": notes,
        }

    def _mark_last_outcome(self, outcome: str) -> None:
        if self.history and "->" not in self.history[-1]:
            self.history[-1] += f" -> {outcome}"
        for entry in reversed(self.step_log):
            if entry.get("action") not in ("look", "invalid", "stuck_recovery"):
                entry.setdefault("outcome", outcome)
                break

    async def _ask(self, messages: list, image_b64) -> str:
        try:
            if image_b64:
                return await self.llm_client.chat_with_vision(messages, image_b64, mime="image/jpeg")
            return await self.llm_client.chat(messages)
        except RuntimeError as exc:
            if image_b64:
                # Model/endpoint can't take this image (no vision, context too
                # small, payload limit): continue text-only rather than stall.
                logger.warning("Vision request failed, retrying without image: %s", exc)
                self.vision_disabled_reason = str(exc)[:160]
                try:
                    return await self.llm_client.chat(messages)
                except RuntimeError as exc2:
                    exc = exc2
            logger.error("LLM call failed: %s", exc)
            return json.dumps({"action": "wait", "seconds": 2, "reason": f"LLM error: {str(exc)[:80]}"})

    # ------------------------------------------------------------------
    # Stuck handling
    # ------------------------------------------------------------------

    async def _handle_stuck(self, step: int) -> str:
        count = self.no_change_count
        if not self.auto_intervene:
            self.force_look = True
            return f"your last {count} actions did not change the screen. Choose a different approach."
        recoveries = [
            ({"action": "press", "key": "back"}, "pressed back"),
            ({"action": "scroll", "direction": "down"}, "scrolled down"),
            ({"action": "press", "key": "home"}, "went to the home screen"),
        ]
        recovery, label = recoveries[min(self.recovery_level, len(recoveries) - 1)]
        self.recovery_level += 1
        self.no_change_count = 0
        self.force_look = True
        description = await self._execute_action(recovery, None)
        self.step_log.append({"step": step, "action": "stuck_recovery", "description": description, "reason": label, "elapsed": 0})
        self.history.append(f"{len(self.history) + 1}. (automatic recovery: {label})")
        await asyncio.sleep(self.step_delay)
        return (
            f"your last {count} actions did not change the screen, so the system {label}. "
            "Re-read the screen and try a different approach."
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _target(self, action: dict, obs) -> tuple:
        """Resolve element number or x/y to device coordinates."""
        if "element" in action:
            el = vision.element_by_number(obs.selected if obs else [], action["element"])
            if el is None:
                raise ValueError(f"element {action['element']} is not on the current screen")
            return el.center, el
        x, y = int(action.get("x", -1)), int(action.get("y", -1))
        width, height = obs.size if obs else getattr(self, "_last_size", (0, 0))
        if x < 0 or y < 0 or (width and x >= width) or (height and y >= height):
            raise ValueError(f"({x},{y}) is outside the {width}x{height} screen")
        return (x, y), None

    async def _execute_action(self, action: dict, obs) -> str:
        """Execute a canonical action via ADB; returns a short description."""
        name = action.get("action", "")
        size = obs.size if obs else getattr(self, "_last_size", (1080, 2400))
        try:
            if name in ("tap", "longpress"):
                (x, y), el = self._target(action, obs)
                if name == "tap":
                    await self._adb(["shell", "input", "tap", str(x), str(y)])
                else:
                    await self._adb(["shell", "input", "swipe", str(x), str(y), str(x), str(y), "800"])
                what = f"[{el.number}] {vision._clip(el.label, 30)!r}" if el else f"({x},{y})"
                return f"{name} {what}"

            if name in ("swipe", "scroll"):
                direction = action.get("direction", "up")
                rect = None
                if "element" in action and obs:
                    el = vision.element_by_number(obs.selected, action["element"])
                    rect = el.rect if el else None
                finger = direction
                if name == "scroll":
                    finger = {"down": "up", "up": "down", "left": "right", "right": "left"}[direction]
                coords = vision.swipe_vector(finger, size, rect)
                await self._adb(["shell", "input", "swipe", *[str(c) for c in coords], "350"])
                return f"{name} {direction}" + (f" in [{action['element']}]" if rect else "")

            if name == "type":
                text = action.get("text", "")
                el = None
                if "element" in action:
                    (x, y), el = self._target(action, obs)
                    await self._adb(["shell", "input", "tap", str(x), str(y)])
                    await asyncio.sleep(0.4)
                if action.get("clear"):
                    existing = len(el.label) if el and el.label else 30
                    await self._adb(["shell", "input", "keyevent", "KEYCODE_MOVE_END"])
                    await self._adb(["shell", "input", "keyevent", *(["KEYCODE_DEL"] * min(existing + 5, 120))], timeout=20)
                if not text.isascii():
                    raise ValueError("adb text input only supports ASCII characters")
                await self._adb(["shell", "input", "text", vision.escape_input_text(text)], timeout=20)
                if action.get("submit"):
                    await self._adb(["shell", "input", "keyevent", "KEYCODE_ENTER"])
                return f'type "{text[:30]}"' + (" + enter" if action.get("submit") else "")

            if name == "press":
                key = str(action.get("key", "enter")).strip().lower()
                if key == "wakeup" and self._wake_device:
                    await asyncio.to_thread(self._wake_device, self.device)
                elif key == "notifications":
                    await self._adb(["shell", "cmd", "statusbar", "expand-notifications"])
                elif key == "quick_settings":
                    await self._adb(["shell", "cmd", "statusbar", "expand-settings"])
                else:
                    keycode = _KEYCODES.get(key, key.upper() if key.upper().startswith("KEYCODE_") else f"KEYCODE_{key.upper()}")
                    await self._adb(["shell", "input", "keyevent", keycode])
                return f"press {key}"

            if name == "launch":
                package = action.get("package", "")
                output = await self._adb(
                    ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"], timeout=15
                )
                if "No activities found" in output or "Error" in output:
                    raise RuntimeError(f"{package} has no launchable activity (is it installed?)")
                return f"launch {package}"

            if name == "wait":
                seconds = float(action.get("seconds", 1))
                await asyncio.sleep(seconds)
                return f"wait {seconds:g}s"

            return f"unknown({name})"

        except (RuntimeError, ValueError) as exc:
            logger.warning("Action %s failed: %s", name, exc)
            return f"error({name}: {str(exc)[:120]})"
