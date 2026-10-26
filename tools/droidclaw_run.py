"""Android Control Run Tool - Autonomous Android control via Python-native execution loop.

Tier 1 (Local Model): Python execution loop that dumps phone UI, sends the
numbered element list (and, when useful, an annotated screenshot) to an LLM,
executes the chosen action via ADB, and repeats.

No external TypeScript/bun dependency required.
"""

import asyncio
import logging
import time

from helpers.tool import Tool, Response
from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import canonical_devices, run_adb_result
from usr.plugins.droidclaw.helpers.device_context import (
    LEASE_TTL,
    DeviceSelectionError,
    acquire_device,
    claim,
    release_device,
)

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"


# ---------------------------------------------------------------------------
# Device I/O injected into the execution loop (run in worker threads)
# ---------------------------------------------------------------------------

def _run_adb(args: list, device: str = None, timeout: int = 10) -> str:
    """Execute an adb command and return stdout."""
    result = run_adb_result(args, device=device if device is not None else "", timeout=timeout)
    if result["returncode"] != 0:
        raise RuntimeError(f"adb failed: {result['stderr'] or result['stdout'] or result['output']}")
    return result["stdout"].strip()


def _wake_device(device: str = None) -> bool:
    """Wake the screen; swipe the keyguard away only if the screen was off.

    The old unconditional swipe scrolled lists / opened the app drawer on an
    already-awake screen whenever a UI dump came back empty.
    """
    try:
        power = run_adb_result(["shell", "dumpsys", "power"], device=device or "", timeout=8)
        asleep = "mWakefulness=Awake" not in (power.get("stdout") or "")
        _run_adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], device=device, timeout=5)
        if asleep:
            time.sleep(0.4)
            size = _screen_size(device)
            x, y1, y2 = size[0] // 2, int(size[1] * 0.85), int(size[1] * 0.35)
            _run_adb(["shell", "input", "swipe", str(x), str(y1), str(x), str(y2), "300"], device=device, timeout=5)
        return True
    except RuntimeError:
        return False


def _screen_size(device: str = None) -> tuple:
    try:
        output = _run_adb(["shell", "wm", "size"], device=device, timeout=5)
    except RuntimeError:
        return (1080, 2400)
    sizes = [line.split(":")[-1].strip() for line in output.splitlines() if "size" in line]
    for value in reversed(sizes):  # "Override size" wins over "Physical size"
        try:
            w, h = value.lower().split("x")
            return (int(w), int(h))
        except ValueError:
            continue
    return (1080, 2400)


def _screenshot_png(device: str = None) -> bytes:
    """Capture a PNG screenshot straight from the device."""
    result = run_adb_result(["exec-out", "screencap", "-p"], device=device or "", timeout=15, binary=True)
    if result["returncode"] != 0 or not result["stdout"]:
        raise RuntimeError(f"screenshot failed: {result['stderr'] or result['output']}")
    return result["stdout"]


def _get_connected_devices() -> list:
    """List connected ADB devices."""
    try:
        return [device.to_dict() for device in canonical_devices()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_plugin_config(agent=None) -> dict:
    try:
        return plugins.get_plugin_config(PLUGIN_NAME, agent=agent) or {}
    except TypeError:
        return plugins.get_plugin_config(PLUGIN_NAME) or {}


_DEFAULTS = {
    "provider": "ollama",
    "vision_mode": "fallback",
    "model_supports_vision": True,
    "max_steps": 30,
    "stuck_threshold": 3,
    "step_delay": 2,
    "max_elements": 40,
    "auto_intervene": True,
    "vision_image_max_edge": 1280,
}

_KEYS = [
    "provider", "default_model", "vision_mode", "max_steps",
    "device", "tier", "stuck_threshold", "step_delay",
    "max_elements", "auto_intervene", "api_base", "api_key",
    "model_supports_vision", "vision_image_max_edge",
]


def _bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _merge_config(cfg: dict, kwargs: dict) -> dict:
    """Tool args > plugin config > defaults (missing keys keep their default)."""
    merged = {}
    for k in _KEYS:
        if k in kwargs and kwargs[k] not in (None, ""):
            merged[k] = kwargs[k]
        elif cfg.get(k) not in (None, ""):
            merged[k] = cfg[k]
        else:
            merged[k] = _DEFAULTS.get(k)
    # Legacy arg name from the tool prompt.
    if "intervene" in kwargs and kwargs["intervene"] not in (None, ""):
        merged["auto_intervene"] = kwargs["intervene"]
    return merged


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------

class DroidClawRun(Tool):
    """Autonomous Android phone control via Python-native execution loop."""

    async def execute(self, **kwargs) -> Response:
        goal = self.args.get("goal", "")
        if not goal:
            return Response(message="Error: goal is required", break_loop=False)

        cfg = _load_plugin_config(self.agent)
        tool_kwargs = dict(self.args)
        tool_kwargs.update(kwargs)
        m = _merge_config(cfg, tool_kwargs)

        # Resolve device: explicit arg > agent binding > config > only device.
        explicit = tool_kwargs.get("device") or ("" if self.agent is not None else m.get("device") or "")
        try:
            device = await claim(self.agent, explicit)
        except DeviceSelectionError as exc:
            return Response(message=f"Error: {exc}", break_loop=False)

        try:
            return await self._run_on_device(goal, device, m)
        finally:
            release_device(self.agent, device)

    async def _run_on_device(self, goal: str, device: str, m: dict) -> Response:
        # Verify device is responsive
        try:
            output = await asyncio.to_thread(_run_adb, ["shell", "echo", "ok"], device, 5)
            if "ok" not in output:
                return Response(
                    message=f"Error: ADB device {device} is not responding.",
                    break_loop=False,
                )
        except Exception as exc:
            return Response(
                message=f"Error: Cannot reach ADB device {device}: {exc}",
                break_loop=False,
            )

        # Resolve LLM configuration
        provider = (m.get("provider") or "ollama").lower()
        model = m.get("default_model") or ""
        api_base = m.get("api_base") or ""
        api_key = m.get("api_key") or ""

        if not model:
            return Response(
                message="Error: No model configured. Set default_model in plugin config.",
                break_loop=False,
            )

        from usr.plugins.droidclaw.helpers import action_validator, execution_loop, ui_parser
        from usr.plugins.droidclaw.helpers.llm_client import LLMClient

        llm_client = LLMClient(
            provider=provider,
            model=model,
            api_base=api_base,
            api_key=api_key,
        )

        supports_vision = _bool(m.get("model_supports_vision"), True)
        vision_mode = str(m.get("vision_mode") or "fallback").lower() if supports_vision else "off"

        loop = execution_loop.ExecutionLoop(
            device=device,
            llm_client=llm_client,
            max_steps=int(m.get("max_steps") or 30),
            step_delay=float(m.get("step_delay") or 2),
            vision_mode=vision_mode,
            max_elements=int(m.get("max_elements") or 40),
            stuck_threshold=int(m.get("stuck_threshold") or 3),
            auto_intervene=_bool(m.get("auto_intervene"), True),
            image_max_edge=int(m.get("vision_image_max_edge") or 1280),
            fn_run_adb=_run_adb,
            fn_dump_ui=ui_parser.dump_ui,
            fn_wake_device=_wake_device,
            fn_screenshot=_screenshot_png,
            fn_validate_action=action_validator.validate_action,
        )

        logger.info(
            "Starting Android Control loop: device=%s, provider=%s, model=%s, vision=%s, goal=%s",
            device, provider, model, vision_mode, goal[:100],
        )

        renew = asyncio.create_task(self._hold_device(device))
        try:
            result = await loop.run(goal)
        except Exception as exc:
            logger.error("Execution loop failed: %s", exc, exc_info=True)
            return Response(
                message=f"Android Control execution failed: {exc}",
                break_loop=False,
            )
        finally:
            renew.cancel()

        summary = execution_loop.format_result_summary(result, goal)
        return Response(message=f"Device: {device}\n{summary}", break_loop=False)

    async def _hold_device(self, device: str) -> None:
        """Keep this agent's claim on the device alive for the whole run."""
        while True:
            await asyncio.sleep(LEASE_TTL / 3)
            try:
                await acquire_device(self.agent, device, wait=0)
            except DeviceSelectionError:
                return
