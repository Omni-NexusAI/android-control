"""Android Control Execution Loop - Autonomous phone control via LLM-guided actions.

Core loop: dump UI -> format elements -> send to LLM -> parse action ->
execute via ADB -> detect stuck -> repeat.

This module is self-contained and does not use relative imports.
It receives helper functions via constructor injection to avoid
namespace conflicts with A0's helpers package.
"""

import asyncio
import json
import logging
import re
import time

logger = logging.getLogger("droidclaw")

SYSTEM_PROMPT = """You are an Android phone automation agent. You control a phone via ADB commands.

You receive the current screen state as a numbered list of UI elements.
Each element has: index, text, description, bounds, clickable, resource_id, and center coordinates (cx, cy).

Your goal is provided by the user. You must navigate the phone to achieve that goal.

Available actions:
- tap: Tap at coordinates. Args: x, y
- longpress: Long press at coordinates. Args: x, y
- swipe: Swipe in a direction. Args: direction (up/down/left/right)
- type: Type text into the focused field. Args: text
- press: Press a hardware key. Args: keycode (home, back, enter, wakeup)
- launch: Launch an app by package name. Args: package
- wait: Wait before next step. Args: seconds
- done: Task is complete. Args: summary

Respond with ONLY a JSON object on a single line:
{"action": "tap", "x": 540, "y": 1200, "reason": "Tapping the Settings icon"}

Navigation strategy:
1. Look at the elements and find the one that matches your target.
2. Use the center coordinates (cx, cy) from that element for taps.
3. If the target is not visible, scroll (swipe) to find it.
4. If the screen is blank or locked, press wakeup first.
5. Be precise - use exact coordinates from the element list.
6. If an app needs to be opened, use launch with the package name.
7. When the goal is achieved, respond with done and a summary.

IMPORTANT: Return ONLY valid JSON. No markdown, no code blocks, no extra text."""

# Keycode mapping for press action
_KEYCODES = {
    "home": "KEYCODE_HOME",
    "back": "KEYCODE_BACK",
    "enter": "KEYCODE_ENTER",
    "wakeup": "KEYCODE_WAKEUP",
    "menu": "KEYCODE_MENU",
    "recent": "KEYCODE_APP_SWITCH",
    "delete": "KEYCODE_DEL",
    "tab": "KEYCODE_TAB",
    "escape": "KEYCODE_ESCAPE",
    "power": "KEYCODE_POWER",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
}


def _filter_elements(elements: list, max_elements: int) -> list:
    """Filter elements to the most relevant ones for the LLM.

    Prioritizes clickable elements and those with text/description.
    """
    scored = []
    for elem in elements:
        score = 0
        if elem.get("clickable"):
            score += 2
        if elem.get("text", "").strip():
            score += 3
        if elem.get("content_desc", "").strip():
            score += 2
        if elem.get("resource_id", "").strip():
            score += 1
        scored.append((score, elem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [elem for _, elem in scored[:max_elements]]


def format_elements_for_llm(elements: list, max_elements: int = 40) -> str:
    """Format UI elements as a numbered list for the LLM."""
    filtered = _filter_elements(elements, max_elements)
    lines = []
    for i, elem in enumerate(filtered):
        text = elem.get("text", "")
        desc = elem.get("content_desc", "")
        bounds = elem.get("bounds", "")
        clickable = elem.get("clickable", False)
        res_id = elem.get("resource_id", "")
        center = elem.get("center", [0, 0])
        cx, cy = center[0], center[1]

        parts = [
            f"[{i}]",
            f'text="{text}"' if text else 'text=""',
            f'desc="{desc}"' if desc else "",
            f"bounds={bounds}",
            f"click={clickable}",
        ]
        if res_id:
            parts.append(f"id={res_id}")
        parts.append(f"cx={cx},cy={cy}")

        line = " ".join(p for p in parts if p)
        lines.append(line)

    return "\n".join(lines)


def parse_llm_response(response: str) -> dict:
    """Extract a JSON action from the LLM response.

    Handles: pure JSON, markdown code blocks, JSON embedded in text.
    """
    if not response:
        return {"action": "wait", "reason": "Empty LLM response", "seconds": 2}

    text = response.strip()

    # Try direct JSON parse first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "action" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    patterns = [
        r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```",
        r"(```json\s*\n)?(\{[^`]*?\})(\s*```)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            json_str = match.group(1)
            if json_str and json_str.startswith("```"):
                json_str = match.group(2)
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, dict) and "action" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass

    # Try finding any JSON object in the text
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        json_candidate = text[brace_start:brace_end + 1]
        try:
            parsed = json.loads(json_candidate)
            if isinstance(parsed, dict) and "action" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse LLM response as action: %s", text[:200])
    return {"action": "wait", "reason": "Could not parse LLM response", "seconds": 2}


def format_result_summary(result: dict, goal: str) -> str:
    """Format the execution result into a human-readable summary."""
    lines = [
        "Android Control Execution Summary (Python Native)",
        "=" * 50,
        f"Goal: {goal}",
        f"Status: {result['status']}",
        f"Steps: {result['steps']}",
        f"Elapsed: {result['elapsed']}s",
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
        lines.append(f"  Step {step}: {action} - {desc or reason} ({elapsed}s)")

    lines.append("")
    lines.append("=" * 50)
    return "\n".join(lines)


class ExecutionLoop:
    """Autonomous execution loop for Android phone control.

    Receives helper functions via constructor injection to avoid
    namespace conflicts. All ADB, UI parsing, and validation
    functions are injected by the caller.

    Args:
        device: ADB device serial string.
        llm_client: LLMClient instance for making LLM calls.
        max_steps: Maximum number of action steps.
        step_delay: Seconds to wait between steps.
        vision_mode: Vision mode (off, fallback, always).
        max_elements: Maximum UI elements to send to LLM.
        stuck_threshold: Consecutive identical screens before stuck recovery.
        fn_run_adb: Callable for run_adb(args, device, timeout) -> str.
        fn_dump_ui: Callable for dump_ui(device) -> list[dict].
        fn_wake_device: Callable for wake_device(device) -> bool.
        fn_capture_screenshot: Callable for capture_screenshot(device) -> str.
        fn_validate_action: Callable for validate_action(action) -> dict.
        fn_compare_dumps: Callable for compare_dumps(prev, curr) -> dict.
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
        fn_run_adb=None,
        fn_dump_ui=None,
        fn_wake_device=None,
        fn_capture_screenshot=None,
        fn_validate_action=None,
        fn_compare_dumps=None,
    ):
        self.device = device
        self.llm_client = llm_client
        self.max_steps = max_steps
        self.step_delay = step_delay
        self.vision_mode = vision_mode
        self.max_elements = max_elements
        self.stuck_threshold = stuck_threshold

        # Injected helper functions
        self._run_adb = fn_run_adb
        self._dump_ui = fn_dump_ui
        self._wake_device = fn_wake_device
        self._capture_screenshot = fn_capture_screenshot
        self._validate_action = fn_validate_action
        self._compare_dumps = fn_compare_dumps

        # State tracking
        self.conversation_history = []
        self.stuck_count = 0
        self.previous_elements = []
        self.step_log = []

    async def _adb(self, args: list, timeout: int = 10) -> str:
        """Run an ADB command asynchronously."""
        return await asyncio.to_thread(self._run_adb, args, self.device, timeout)

    async def run(self, goal: str) -> dict:
        """Execute the main autonomous loop.

        Args:
            goal: Natural language goal to achieve on the phone.

        Returns:
            Result dict with keys: status, steps, elapsed, actions, summary.
        """
        start_time = time.time()

        self.conversation_history = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Your goal: {goal}\n\n"
                    "Analyze the current screen and take the first action. "
                    "Respond with ONLY a JSON action."
                ),
            },
        ]

        for step in range(1, self.max_steps + 1):
            step_start = time.time()
            logger.info("=== Step %d/%d ===", step, self.max_steps)

            # 1. Get screen state
            try:
                elements = await asyncio.to_thread(self._dump_ui, self.device)
            except Exception as exc:
                logger.error("UI dump failed: %s", exc)
                elements = []

            if not elements:
                logger.warning("No UI elements - trying to wake device")
                try:
                    await asyncio.to_thread(self._wake_device, self.device)
                except Exception:
                    pass
                await asyncio.sleep(2)
                try:
                    elements = await asyncio.to_thread(self._dump_ui, self.device)
                except Exception:
                    elements = []

            # 2. Format elements for LLM
            elements_text = format_elements_for_llm(elements, self.max_elements)
            element_count = len(elements) if elements else 0
            filtered_count = elements_text.count("\n") + 1 if elements_text else 0

            # 3. Build message for LLM
            user_msg = (
                f"Step {step}/{self.max_steps}. "
                f"Screen has {element_count} elements ({filtered_count} shown).\n\n"
                f"Elements:\n{elements_text}"
            )

            # Add vision if enabled
            screenshot_b64 = None
            if self.vision_mode in ("always", "fallback") and (
                self.vision_mode == "always" or element_count < 5
            ):
                try:
                    screenshot_path = await asyncio.to_thread(
                        self._capture_screenshot, self.device
                    )
                    import base64
                    with open(screenshot_path, "rb") as f:
                        screenshot_b64 = base64.b64encode(f.read()).decode("ascii")
                except Exception as exc:
                    logger.warning("Screenshot failed: %s", exc)

            # Update conversation: keep system prompt + initial goal, replace last user msg
            if len(self.conversation_history) > 2:
                self.conversation_history = self.conversation_history[:2]
            self.conversation_history.append({"role": "user", "content": user_msg})

            # 4. Call LLM
            try:
                if screenshot_b64:
                    llm_response = await self.llm_client.chat_with_vision(
                        self.conversation_history, screenshot_b64
                    )
                else:
                    llm_response = await self.llm_client.chat(self.conversation_history)
            except RuntimeError as exc:
                logger.error("LLM call failed: %s", exc)
                llm_response = '{"action": "wait", "reason": "LLM error", "seconds": 3}'

            self.conversation_history.append(
                {"role": "assistant", "content": llm_response}
            )

            # 5. Parse action
            raw_action = parse_llm_response(llm_response)
            action = self._validate_action(raw_action)
            action_name = action.get("action", "unknown")
            reason = action.get("reason", "")

            logger.info("Step %d action: %s | reason: %s", step, action_name, reason)

            # 6. Check for done
            if action_name == "done":
                elapsed = time.time() - start_time
                summary = action.get("summary", action.get("reason", goal))
                self.step_log.append({
                    "step": step,
                    "action": "done",
                    "reason": summary,
                    "elapsed": round(time.time() - step_start, 1),
                })
                return {
                    "status": "completed",
                    "steps": step,
                    "elapsed": round(elapsed, 1),
                    "actions": self.step_log,
                    "summary": summary,
                }

            # 7. Execute action
            action_desc = await self._execute_action(action)
            self.step_log.append({
                "step": step,
                "action": action_name,
                "description": action_desc,
                "reason": reason,
                "elements": element_count,
                "elapsed": round(time.time() - step_start, 1),
            })

            # 8. Check for stuck state
            if elements:
                is_same = await self._check_stuck(elements)
                if is_same:
                    self.stuck_count += 1
                    logger.warning(
                        "Stuck detected (%d/%d)",
                        self.stuck_count, self.stuck_threshold,
                    )
                    if self.stuck_count >= self.stuck_threshold:
                        await self._recover_from_stuck(step)
                        self.stuck_count = 0
                else:
                    self.stuck_count = 0

            self.previous_elements = elements

            # 9. Delay between steps
            if step < self.max_steps:
                await asyncio.sleep(self.step_delay)

        # Max steps reached
        elapsed = time.time() - start_time
        return {
            "status": "max_steps_reached",
            "steps": self.max_steps,
            "elapsed": round(elapsed, 1),
            "actions": self.step_log,
            "summary": f"Reached max steps ({self.max_steps}) without completing goal.",
        }

    async def _execute_action(self, action: dict) -> str:
        """Execute a validated action via ADB."""
        action_name = action.get("action", "")

        try:
            if action_name == "tap":
                x = int(action.get("x", 0))
                y = int(action.get("y", 0))
                await self._adb(["shell", "input", "tap", str(x), str(y)])
                return f"tap({x},{y})"

            elif action_name == "longpress":
                x = int(action.get("x", 0))
                y = int(action.get("y", 0))
                await self._adb(
                    ["shell", "input", "swipe", str(x), str(y), str(x), str(y), "1000"]
                )
                return f"longpress({x},{y})"

            elif action_name == "swipe":
                direction = action.get("direction", "up")
                swipe_map = {
                    "up": ["540", "1800", "540", "600", "300"],
                    "down": ["540", "600", "540", "1800", "300"],
                    "left": ["900", "1200", "180", "1200", "300"],
                    "right": ["180", "1200", "900", "1200", "300"],
                }
                coords = swipe_map.get(direction, swipe_map["up"])
                await self._adb(["shell", "input", "swipe"] + coords)
                return f"swipe({direction})"

            elif action_name == "type":
                text_to_type = action.get("text", "")
                if text_to_type:
                    safe_text = text_to_type.replace(" ", "%s")
                    safe_text = safe_text.replace("&", "\\&")
                    safe_text = safe_text.replace("<", "\\<")
                    safe_text = safe_text.replace(">", "\\>")
                    await self._adb(["shell", "input", "text", safe_text], timeout=15)
                return f'type("{text_to_type[:30]}")'

            elif action_name == "press":
                keycode_raw = action.get("keycode", "enter").lower()
                if keycode_raw == "wakeup":
                    await asyncio.to_thread(self._wake_device, self.device)
                else:
                    keycode = _KEYCODES.get(keycode_raw, f"KEYCODE_{keycode_raw.upper()}")
                    await self._adb(["shell", "input", "keyevent", keycode])
                return f"press({keycode_raw})"

            elif action_name == "launch":
                package = action.get("package", "")
                if package:
                    try:
                        await self._adb(
                            ["shell", "am", "start", "-n", f"{package}/.MainActivity"]
                        )
                    except RuntimeError:
                        pass
                    try:
                        await self._adb(
                            ["shell", "monkey", "-p", package, "-c",
                             "android.intent.category.LAUNCHER", "1"]
                        )
                    except RuntimeError:
                        pass
                return f"launch({package})"

            elif action_name == "wait":
                seconds = int(action.get("seconds", action.get("duration", 2)))
                await asyncio.sleep(seconds)
                return f"wait({seconds}s)"

            else:
                logger.warning("Unknown action: %s", action_name)
                await asyncio.sleep(1)
                return f"unknown({action_name})"

        except RuntimeError as exc:
            logger.error("ADB action failed: %s", exc)
            return f"error({action_name}: {str(exc)[:100]})"

    async def _check_stuck(self, current_elements: list) -> bool:
        """Check if the screen state has not changed."""
        if not self.previous_elements:
            return False
        comparison = self._compare_dumps(self.previous_elements, current_elements)
        return not comparison["changed"]

    async def _recover_from_stuck(self, step: int) -> None:
        """Attempt to recover from a stuck state."""
        logger.info("Attempting stuck recovery at step %d", step)

        recovery_actions = [
            {"action": "press", "keycode": "back", "reason": "Stuck recovery: press back"},
            {"action": "swipe", "direction": "up", "reason": "Stuck recovery: scroll up"},
            {"action": "press", "keycode": "home", "reason": "Stuck recovery: go home"},
        ]

        idx = min(self.stuck_count - self.stuck_threshold, len(recovery_actions) - 1)
        recovery = recovery_actions[max(0, idx)]

        action_desc = await self._execute_action(recovery)
        self.step_log.append({
            "step": step,
            "action": "stuck_recovery",
            "description": action_desc,
            "reason": recovery["reason"],
            "elapsed": 0,
        })

        await asyncio.sleep(2)
