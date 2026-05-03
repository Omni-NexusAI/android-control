"""Android Control Run Tool - Autonomous Android control via Python-native execution loop.

Tier 1 (Local Model): Python execution loop that dumps phone UI, sends elements
to an LLM, parses the action response, executes via ADB, and repeats.

No external TypeScript/bun dependency required.

Uses importlib to load plugin-internal helpers (ui_parser, action_validator,
screen_comparator, llm_client, execution_loop) to avoid namespace collision
with A0's own helpers package. ADB functions are inlined here to bypass
the adb_client.py module which has conflicting imports.
"""

import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
import tempfile
import time
import types

from helpers.tool import Tool, Response
from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import canonical_devices, resolve_device, run_adb_result

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_HELPERS_DIR = os.path.join(_PLUGIN_DIR, "helpers")


# ---------------------------------------------------------------------------
# Inline ADB functions (avoids loading adb_client.py which conflicts)
# ---------------------------------------------------------------------------

def _run_adb(args: list, device: str = None, timeout: int = 10) -> str:
    """Execute an adb command and return stdout."""
    try:
        result = run_adb_result(args, device=device if device is not None else "", timeout=timeout)
        if result["returncode"] != 0:
            raise RuntimeError(f"adb failed: {result['stderr'] or result['stdout'] or result['output']}")
        return result["stdout"].strip()
    except FileNotFoundError:
        raise RuntimeError("adb not found. Install Android SDK platform-tools.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb command timed out after {timeout}s")


def _wake_device(device: str = None) -> bool:
    """Wake the phone screen if it is asleep."""
    try:
        _run_adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], device=device, timeout=5)
        time.sleep(0.3)
        _run_adb(
            ["shell", "input", "swipe", "540", "1800", "540", "800", "300"],
            device=device, timeout=5,
        )
        return True
    except RuntimeError:
        return False


_SCREENSHOT_DIR = "/a0/tmp/droidclaw_screenshots"


def _capture_screenshot(device: str = None, local_path: str = None) -> str:
    """Capture a screenshot from the device and save it locally."""
    os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
    if local_path is None:
        timestamp = int(time.time() * 1000)
        local_path = os.path.join(_SCREENSHOT_DIR, f"screenshot_{timestamp}.png")
    remote_path = "/sdcard/droidclaw_screenshot.png"
    _run_adb(["shell", "screencap", "-p", remote_path], device=device, timeout=10)
    _run_adb(["pull", remote_path, local_path], device=device, timeout=15)
    try:
        _run_adb(["shell", "rm", remote_path], device=device, timeout=5)
    except RuntimeError:
        pass
    return local_path


def _get_connected_devices() -> list:
    """List connected ADB devices."""
    try:
        return [device.to_dict() for device in canonical_devices()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Plugin module loader (importlib-based)
# ---------------------------------------------------------------------------

_loaded_modules = {}


def _load_plugin_module(module_name: str):
    """Load a module from the plugin's helpers/ directory via importlib."""
    prefixed_name = f"droidclaw_helpers.{module_name}"
    if prefixed_name in _loaded_modules:
        return _loaded_modules[prefixed_name]

    if "droidclaw_helpers" not in sys.modules:
        pkg = types.ModuleType("droidclaw_helpers")
        pkg.__path__ = [_PLUGIN_HELPERS_DIR]
        pkg.__package__ = "droidclaw_helpers"
        sys.modules["droidclaw_helpers"] = pkg

    file_path = os.path.join(_PLUGIN_HELPERS_DIR, f"{module_name}.py")
    if not os.path.isfile(file_path):
        raise ImportError(f"Plugin helper not found: {file_path}")

    spec = importlib.util.spec_from_file_location(prefixed_name, file_path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "droidclaw_helpers"
    sys.modules[prefixed_name] = module
    spec.loader.exec_module(module)

    _loaded_modules[prefixed_name] = module
    return module


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_plugin_config() -> dict:
    return plugins.get_plugin_config(PLUGIN_NAME) or {}


def _merge_config(cfg: dict, kwargs: dict) -> dict:
    keys = [
        "provider", "default_model", "vision_mode", "max_steps",
        "device", "tier", "stuck_threshold", "step_delay",
        "max_elements", "auto_intervene", "api_base", "api_key",
        "model_supports_vision",
    ]
    merged = {}
    for k in keys:
        if k in kwargs and kwargs[k] not in (None, ""):
            merged[k] = kwargs[k]
        elif k in cfg:
            merged[k] = cfg[k]
        else:
            merged[k] = None
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

        cfg = _load_plugin_config()
        tool_kwargs = dict(self.args)
        tool_kwargs.update(kwargs)
        m = _merge_config(cfg, tool_kwargs)

        # Resolve device
        requested_device = m.get("device") or ""
        resolution = resolve_device(requested_device)
        device = resolution.get("resolved_device") or ""
        if not device:
            return Response(
                message="Error: No ADB device connected. Connect a phone first.",
                break_loop=False,
            )

        # Verify device is responsive
        try:
            output = _run_adb(["shell", "echo", "ok"], device=device, timeout=5)
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

        # Load plugin helper modules (only the ones without import conflicts)
        try:
            ui_parser = _load_plugin_module("ui_parser")
            action_validator = _load_plugin_module("action_validator")
            screen_comparator = _load_plugin_module("screen_comparator")
            llm_client_mod = _load_plugin_module("llm_client")
            execution_loop_mod = _load_plugin_module("execution_loop")
        except ImportError as exc:
            return Response(
                message=f"Error: Failed to load plugin modules: {exc}",
                break_loop=False,
            )

        # Create LLM client
        llm_client = llm_client_mod.LLMClient(
            provider=provider,
            model=model,
            api_base=api_base,
            api_key=api_key,
        )

        # Create execution loop with injected functions
        loop = execution_loop_mod.ExecutionLoop(
            device=device,
            llm_client=llm_client,
            max_steps=int(m.get("max_steps") or 30),
            step_delay=float(m.get("step_delay") or 2),
            vision_mode=(m.get("vision_mode") or "off") if bool(m.get("model_supports_vision", True)) else "off",
            max_elements=int(m.get("max_elements") or 40),
            stuck_threshold=int(m.get("stuck_threshold") or 3),
            fn_run_adb=_run_adb,
            fn_dump_ui=ui_parser.dump_ui,
            fn_wake_device=_wake_device,
            fn_capture_screenshot=_capture_screenshot,
            fn_validate_action=action_validator.validate_action,
            fn_compare_dumps=screen_comparator.compare_dumps,
        )

        logger.info(
            "Starting Android Control Python loop: device=%s, provider=%s, model=%s, goal=%s",
            device, provider, model, goal[:100],
        )

        try:
            result = await loop.run(goal)
        except Exception as exc:
            logger.error("Execution loop failed: %s", exc, exc_info=True)
            return Response(
                message=f"Android Control execution failed: {exc}",
                break_loop=False,
            )

        summary = execution_loop_mod.format_result_summary(result, goal)
        return Response(message=summary, break_loop=False)
