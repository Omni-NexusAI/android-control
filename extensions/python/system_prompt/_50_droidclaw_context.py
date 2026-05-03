import subprocess
import os
import logging

from helpers.extension import Extension
from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import adb_cmd

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"

_cached_guide = None
_config_cache = None
_config_key = ""


def _get_plugin_root():
    ext_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(ext_dir)))


def _load_config() -> dict:
    global _config_cache, _config_key
    try:
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
        new_key = str(sorted(cfg.items()))
        if _config_cache is not None and new_key == _config_key:
            return _config_cache
        _config_cache = cfg
        _config_key = new_key
        return _config_cache
    except Exception as e:
        logger.debug(f"Failed to load config: {e}")
        return {}


def _get_connected_devices():
    try:
        result = subprocess.run(
            adb_cmd(["devices"]),
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.strip().split("\n")
        devices = []
        for line in lines[1:]:
            parts = line.strip().split("\t")
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices
    except Exception:
        return []


def _get_device_property(serial, prop):
    try:
        result = subprocess.run(
            adb_cmd(["shell", "getprop", prop], device=serial),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _get_foreground_app(serial):
    try:
        result = subprocess.run(
            adb_cmd(["shell", "dumpsys activity activities"], device=serial),
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.split("\n"):
            if "mResumedActivity" in line:
                parts = line.strip().split()
                for part in parts:
                    if "/" in part and not part.startswith("Activity"):
                        return part
                for part in reversed(parts):
                    if "." in part and "Activity" not in part:
                        return part.strip("}")
        return "unknown"
    except Exception:
        return "unknown"


def _load_navigation_guide():
    global _cached_guide
    if _cached_guide is not None:
        return _cached_guide
    try:
        plugin_root = _get_plugin_root()
        guide_path = os.path.join(plugin_root, "prompts", "droidclaw_system.md")
        if os.path.exists(guide_path):
            with open(guide_path, "r", encoding="utf-8") as f:
                _cached_guide = f.read()
        else:
            _cached_guide = ""
    except Exception:
        _cached_guide = ""
    return _cached_guide


def _build_config_block(cfg):
    provider = cfg.get("provider", "ollama")
    model = cfg.get("default_model", "") or cfg.get("model", "")
    vision = cfg.get("vision_mode", "fallback")
    model_supports_vision = bool(cfg.get("model_supports_vision", True))
    tier = cfg.get("tier", "auto")
    max_steps = cfg.get("max_steps", 30)
    device = cfg.get("device", "")
    stuck_threshold = cfg.get("stuck_threshold", 3)
    auto_intervene = cfg.get("auto_intervene", True)
    api_base = cfg.get("api_base", "")

    tier_labels = {
        "auto": "Auto (agent decides)",
        "tier1": "Tier 1 (Local Model)",
        "tier2": "Tier 2 (A0 Direct)",
        "tier3": "Tier 3 (Workflow Only)",
    }
    tier_str = tier_labels.get(tier, tier)

    lines = [
        "ANDROID CONTROL CONFIG (user overrides):",
        f"- Provider: {provider}",
        f"- Model: {model}",
        f"- Model Supports Vision: {'YES' if model_supports_vision else 'NO'}",
        f"- Vision Mode: {vision if model_supports_vision else 'off (model vision disabled)'}",
        f"- Tier: {tier_str}",
        f"- Max Steps: {max_steps}",
        f"- ADB Device: {device or '(auto-detect)'}",
        f"- Stuck Threshold: {stuck_threshold}",
        f"- Auto-Intervene: {'ON' if auto_intervene else 'OFF'}",
        f"- API Base: {api_base or '(provider default)'}",
    ]
    return "\n".join(lines)


class DroidClawContext(Extension):
    def execute(self, **kwargs):
        system_prompt = kwargs.get("system_prompt")
        if system_prompt is None:
            return

        try:
            cfg = _load_config()
            config_block = _build_config_block(cfg)
            devices = _get_connected_devices()

            if not devices:
                injection = (
                    "\n\n"
                    "---\n"
                    "## Android Control\n"
                    f"{config_block}\n"
                    "\n"
                    "*No ADB device currently connected.*\n"
                )
                system_prompt.append(injection)
                return

            serial = devices[0]
            configured_device = cfg.get("device", "")
            if configured_device:
                for d in devices:
                    if configured_device in d:
                        serial = d
                        break

            model = _get_device_property(serial, "ro.product.model")
            android_ver = _get_device_property(serial, "ro.build.version.release")
            sdk_ver = _get_device_property(serial, "ro.build.version.sdk")
            foreground = _get_foreground_app(serial)
            guide = _load_navigation_guide()

            device_info = (
                "\n\n"
                "---\n"
                "## Android Control Device Context\n"
                f"**Device Connected**: {serial}\n"
                f"**Model**: {model or 'unknown'}\n"
                f"**Android**: {android_ver or 'unknown'} (SDK {sdk_ver or '?'})\n"
                f"**Foreground App**: {foreground}\n"
                "**ADB Tools Available**: tap, swipe, type, keyevent, screencap, uiautomator dump\n"
                "\n"
                f"### {config_block}\n"
                "\n"
            )

            if guide:
                device_info += guide
            else:
                device_info += (
                    "*Navigation guide not loaded. Use ADB tools directly.*\n"
                )

            system_prompt.append(device_info)

        except Exception as e:
            logger.error(f"Android Control context injection failed: {e}")
