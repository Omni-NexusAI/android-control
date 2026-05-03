import subprocess
import asyncio
from typing import Optional

from helpers.tool import Tool, Response
from usr.plugins.droidclaw.helpers.adb_backend import canonical_devices, resolve_device, run_adb_async


async def _run_adb(cmd: list, device: Optional[str] = None, timeout: int = 15) -> str:
    try:
        result = await run_adb_async(cmd, device=device, timeout=timeout)
    except asyncio.TimeoutError:
        return f"Error: command timed out after {timeout}s"
    if result["returncode"] != 0:
        return f"Error: {result['stderr'] or result['stdout'] or result['output']}"
    return result["stdout"] or result["stderr"] or "OK"


async def _auto_detect_device() -> Optional[str]:
    resolution = resolve_device("")
    if resolution.get("resolved_device"):
        return resolution["resolved_device"]
    raise RuntimeError("No ADB device connected. Run 'adb devices' to check.")


class AdbDevice(Tool):
    async def execute(self, **kwargs) -> Response:
        action = self.args.get("action", "")
        if not action:
            return Response(message="Error: action is required", break_loop=False)

        ip_port = self.args.get("ip_port", "")
        code = self.args.get("code", "")
        device = self.args.get("device", None)

        if action == "status":
            result = await self._device_status()
        elif action == "connect":
            result = await self._device_connect(ip_port)
        elif action == "pair":
            result = await self._device_pair(ip_port, code)
        elif action == "disconnect":
            result = await self._device_disconnect(ip_port)
        elif action == "foreground_app":
            result = await self._foreground_app(device)
        elif action == "wake":
            result = await _run_adb(
                ["shell", "input", "keyevent", "KEYCODE_WAKEUP"], device
            )
        elif action == "sleep":
            result = await _run_adb(
                ["shell", "input", "keyevent", "KEYCODE_SLEEP"], device
            )
        else:
            result = f"Error: unknown action '{action}'. Valid actions: status, connect, pair, disconnect, foreground_app, wake, sleep"

        return Response(message=result, break_loop=False)

    async def _device_status(self) -> str:
        devices = []
        for dev in canonical_devices():
            details = f"model:{dev.model}" if dev.model else ""
            devices.append(f"  - {dev.serial} [connected] {details}")
        if not devices:
            return "No devices connected"
        return f"Connected devices ({len(devices)}):\n" + "\n".join(devices)

    async def _device_connect(self, ip_port: str) -> str:
        if not ip_port:
            return "Error: ip_port is required (format: IP:PORT)"
        result = await _run_adb(["connect", ip_port])
        if "connected" in result.lower():
            return f"Connected to {ip_port}. {result}"
        elif "already connected" in result.lower():
            return f"Already connected to {ip_port}."
        return f"Connection failed: {result}"

    async def _device_pair(self, ip_port: str, code: str) -> str:
        if not ip_port:
            return "Error: ip_port is required for pairing (format: IP:PORT)"
        if not code:
            return "Error: code is required for pairing"
        result = await run_adb_async(["pair", ip_port, code], timeout=15, resolve=False)
        combined = result["output"]
        if "successfully" in combined.lower() or result["returncode"] == 0:
            return f"Paired successfully with {ip_port}."
        return f"Pairing failed: {combined}"

    async def _device_disconnect(self, ip_port: str) -> str:
        if ip_port:
            result = await run_adb_async(["disconnect", ip_port], timeout=15, resolve=False)
            text = result["output"] or "OK"
            return (
                f"Disconnected {ip_port}. {text}"
                if text != "OK"
                else f"Disconnected {ip_port}."
            )
        result = await run_adb_async(["disconnect"], timeout=15, resolve=False)
        text = result["output"] or "OK"
        return (
            f"Disconnected all devices. {text}"
            if text != "OK"
            else "Disconnected all devices."
        )

    async def _foreground_app(self, device: Optional[str]) -> str:
        try:
            device = await _auto_detect_device() if not device else resolve_device(device).get("resolved_device")
            if not device:
                return "Error: ADB device is not connected"
        except RuntimeError as e:
            return f"Error: {e}"
        result = await _run_adb(
            ["shell", "dumpsys", "activity", "activities"], device, timeout=10
        )
        for line in result.split("\n"):
            if "mResumedActivity" in line:
                parts = line.strip().split()
                for part in parts:
                    if "/" in part:
                        return f"Foreground app: {part}"
                return f"Foreground: {line.strip()}"
        return "Could not determine foreground app"
