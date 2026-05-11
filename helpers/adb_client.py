"""
Android Control ADB Client - Core ADB command execution via subprocess.

Provides functions for device management, UI interaction,
screen capture, and app control through Android Debug Bridge.
"""

import logging
import os
import re
import subprocess
import time

from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import adb_cmd, canonical_devices, run_adb_result

logger = logging.getLogger("droidclaw")

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCREENSHOT_DIR = "/a0/tmp/droidclaw_screenshots"


def _load_config() -> dict:
    try:
        return plugins.get_plugin_config("droidclaw") or {}
    except Exception:
        return {}


def run_adb(args: list, device: str = None, timeout: int = 10) -> str:
    """
    Execute an adb command and return stdout.

    Args:
        args: List of adb command arguments (e.g. ["shell", "dumpsys", "activity"]).
        device: Target device serial/IP. None resolves to default device.
        timeout: Maximum seconds to wait for command completion.

    Returns:
        Command stdout as stripped string.

    Raises:
        RuntimeError: If the adb command fails or times out.
    """
    if device is None:
        device = get_default_device()
    try:
        result = run_adb_result(args, device=device if device is not None else "", timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(
            "ADB client is unavailable. Android Control should install plugin-owned "
            "platform-tools automatically; check Android Control dependency diagnostics."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb command timed out after {timeout}s")

    logger.debug("Running adb command: %s", " ".join(result.get("cmd") or []))

    if result["returncode"] != 0:
        stderr = result["stderr"]
        logger.error("adb command failed (rc=%d): %s", result["returncode"], stderr)
        raise RuntimeError(f"adb command failed: {stderr}")

    return result["stdout"].strip()


def get_connected_devices() -> list[dict]:
    """
    List connected ADB devices with model, IP, and state.

    Returns:
        List of dicts with keys: serial, model, ip, state.
    """
    devices = []
    for device in canonical_devices():
        devices.append(
            {
                "serial": device.serial,
                "model": device.model or "unknown",
                "ip": device.ip,
                "state": device.state,
                "aliases": device.aliases or [],
            }
        )

    return devices


def connect_device(ip_port: str) -> bool:
    """
    Connect to a wireless ADB device.

    Args:
        ip_port: Device address in IP:PORT format (e.g. "192.168.1.10:5555").

    Returns:
        True if connection succeeded, False otherwise.
    """
    try:
        output = subprocess.run(
            adb_cmd(["connect", ip_port]), capture_output=True, text=True, timeout=15
        ).stdout.strip()
        logger.info("adb connect %s: %s", ip_port, output)
        return "connected" in output.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.error("Failed to connect to %s: %s", ip_port, exc)
        return False


def pair_device(ip_port: str, code: str) -> bool:
    """
    Pair with a device using a pairing code (ADB over WiFi).

    Args:
        ip_port: Device address in IP:PORT format.
        code: Pairing code displayed on the device.

    Returns:
        True if pairing succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            adb_cmd(["pair", ip_port, code]), capture_output=True, text=True, timeout=15
        )
        success = result.returncode == 0
        if success:
            logger.info("Successfully paired with %s", ip_port)
        else:
            logger.error("Pairing failed for %s: %s", ip_port, result.stderr.strip())
        return success
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.error("Failed to pair with %s: %s", ip_port, exc)
        return False


def get_default_device() -> str:
    """
    Get the default device serial from config, or auto-detect.

    Returns:
        Device serial string, or empty string if none available.
    """
    config = _load_config()
    configured = config.get("device", "")

    if configured:
        return configured

    devices = get_connected_devices()
    if devices:
        return devices[0]["serial"]

    logger.warning("No ADB device configured and none connected")
    return ""


def get_foreground_app(device: str = None) -> str:
    """
    Get the current foreground app package name.

    Args:
        device: Target device serial. None resolves to default device.

    Returns:
        Package name of the foreground app, or empty string if detection fails.
    """
    try:
        output = run_adb(
            ["shell", "dumpsys", "activity", "activities"],
            device=device,
            timeout=10,
        )
    except RuntimeError as exc:
        logger.error("Failed to get foreground app: %s", exc)
        return ""

    for line in output.splitlines():
        if "mResumedActivity" in line or "topResumedActivity" in line:
            match = re.search(r"([a-zA-Z][a-zA-Z0-9_.]+)/[a-zA-Z][a-zA-Z0-9_.]+", line)
            if match:
                return match.group(1)
            parts = line.split()
            for part in parts:
                if "/" in part and not part.startswith("TaskRecord"):
                    pkg = part.split("/")[0]
                    if re.match(r"^[a-zA-Z][a-zA-Z0-9_.]+$", pkg):
                        return pkg

    logger.warning("Could not determine foreground app")
    return ""


def wake_device(device: str = None) -> bool:
    """
    Wake the phone screen if it is asleep.

    Args:
        device: Target device serial. None resolves to default device.

    Returns:
        True if wake command was sent successfully, False otherwise.
    """
    try:
        run_adb(
            ["shell", "input", "keyevent", "KEYCODE_WAKEUP"], device=device, timeout=5
        )
        time.sleep(0.3)
        run_adb(
            ["shell", "input", "swipe", "540", "1800", "540", "800", "300"],
            device=device,
            timeout=5,
        )
        return True
    except RuntimeError as exc:
        logger.error("Failed to wake device: %s", exc)
        return False


def capture_screenshot(device: str = None, local_path: str = None) -> str:
    """
    Capture a screenshot from the device and save it locally.

    Args:
        device: Target device serial. None resolves to default device.
        local_path: Local file path to save screenshot. Auto-generated if None.

    Returns:
        Absolute path to the saved screenshot file.

    Raises:
        RuntimeError: If screenshot capture or pull fails.
    """
    os.makedirs(_SCREENSHOT_DIR, exist_ok=True)

    if local_path is None:
        timestamp = int(time.time() * 1000)
        local_path = os.path.join(_SCREENSHOT_DIR, f"screenshot_{timestamp}.png")

    remote_path = "/sdcard/droidclaw_screenshot.png"

    run_adb(["shell", "screencap", "-p", remote_path], device=device, timeout=10)
    run_adb(["pull", remote_path, local_path], device=device, timeout=15)

    try:
        run_adb(["shell", "rm", remote_path], device=device, timeout=5)
    except RuntimeError:
        pass

    logger.info("Screenshot saved to %s", local_path)
    return local_path
