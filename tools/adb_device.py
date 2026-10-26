import asyncio
from typing import Optional

from helpers.tool import Tool, Response
from usr.plugins.droidclaw.helpers.adb_backend import canonical_devices, resolve_device, run_adb_async
from usr.plugins.droidclaw.helpers.device_context import (
    DeviceSelectionError,
    acquire_device,
    bind_device,
    bound_device,
    claim,
    describe_devices,
    holder_id,
    lease_table,
    release_device,
)

ACTIONS = "status, list, select, release, connect, pair, disconnect, foreground_app, wake, sleep"


async def _run_adb(cmd: list, device: Optional[str] = None, timeout: int = 15) -> str:
    try:
        result = await run_adb_async(cmd, device=device, timeout=timeout)
    except asyncio.TimeoutError:
        return f"Error: command timed out after {timeout}s"
    if result["returncode"] != 0:
        return f"Error: {result['stderr'] or result['stdout'] or result['output']}"
    return result["stdout"] or result["stderr"] or "OK"


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class AdbDevice(Tool):
    async def execute(self, **kwargs) -> Response:
        action = self.args.get("action", "")
        if not action:
            return Response(message="Error: action is required", break_loop=False)

        ip_port = self.args.get("ip_port", "")
        code = self.args.get("code", "")
        device = self.args.get("device", None)

        try:
            if action in ("status", "list"):
                result = await self._device_status()
            elif action == "select":
                result = await self._select(device or ip_port)
            elif action == "release":
                released = release_device(self.agent)
                bind_device(self.agent, "")
                result = f"Released {', '.join(released)}." if released else "This agent held no devices."
            elif action == "connect":
                result = await self._device_connect(ip_port)
            elif action == "pair":
                result = await self._device_pair(ip_port, code)
            elif action == "disconnect":
                result = await self._device_disconnect(ip_port or device or "", _truthy(self.args.get("all", False)))
            elif action == "foreground_app":
                result = await self._foreground_app(await claim(self.agent, device))
            elif action == "wake":
                result = await _run_adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], await claim(self.agent, device))
            elif action == "sleep":
                result = await _run_adb(["shell", "input", "keyevent", "KEYCODE_SLEEP"], await claim(self.agent, device))
            else:
                result = f"Error: unknown action '{action}'. Valid actions: {ACTIONS}"
        except DeviceSelectionError as e:
            result = f"Error: {e}"

        return Response(message=result, break_loop=False)

    async def _device_status(self) -> str:
        devices = await asyncio.to_thread(canonical_devices)
        if not devices:
            return "No devices connected"
        leases = lease_table()
        bound = bound_device(self.agent)
        lines = [f"Connected devices ({len(devices)}):"]
        for dev in devices:
            notes = []
            if dev.model:
                notes.append(f"model:{dev.model}")
            if bound and (bound == dev.serial or bound in (dev.aliases or [])):
                notes.append("bound to this agent")
            lease = leases.get(dev.serial)
            if lease:
                notes.append(f"in use by {lease['holder']}")
            lines.append(f"  - {dev.serial} [connected] {' '.join(notes)}".rstrip())
        if len(devices) > 1 and not bound:
            lines.append('Multiple devices: pass "device" on each call, or bind this agent with action "select".')
        return "\n".join(lines)

    async def _select(self, requested: str) -> str:
        if not requested:
            devices = await asyncio.to_thread(canonical_devices)
            return 'Error: "device" is required for select. Connected devices:\n' + (describe_devices(devices) or "(none)")
        resolution = await asyncio.to_thread(resolve_device, requested)
        serial = resolution.get("resolved_device")
        if not serial:
            devices = await asyncio.to_thread(canonical_devices)
            return f"Error: device '{requested}' is not connected. Connected devices:\n" + (describe_devices(devices) or "(none)")
        previous = bound_device(self.agent)
        if previous and previous != serial:
            release_device(self.agent, previous)
        await acquire_device(self.agent, serial)
        bind_device(self.agent, serial)
        scope = "this agent" if holder_id(self.agent) else "this call"
        return f"Selected {serial} for {scope}. Android Control tools will use it when \"device\" is omitted."

    async def _device_connect(self, ip_port: str) -> str:
        if not ip_port:
            return "Error: ip_port is required (format: IP:PORT)"
        result = await _run_adb(["connect", ip_port])
        if "already connected" in result.lower():
            return f"Already connected to {ip_port}."
        if "connected" in result.lower() and "cannot" not in result.lower() and "failed" not in result.lower():
            return f"Connected to {ip_port}. {result}"
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

    async def _device_disconnect(self, ip_port: str, disconnect_all: bool) -> str:
        if ip_port:
            result = await run_adb_async(["disconnect", ip_port], timeout=15, resolve=False)
            release_device(self.agent, ip_port)
            text = result["output"] or "OK"
            return (
                f"Disconnected {ip_port}. {text}"
                if text != "OK"
                else f"Disconnected {ip_port}."
            )
        if not disconnect_all:
            return (
                "Error: disconnect needs ip_port (or device). Disconnecting every wireless device would "
                "interrupt other agents; pass \"all\": true if that is really intended."
            )
        result = await run_adb_async(["disconnect"], timeout=15, resolve=False)
        text = result["output"] or "OK"
        return (
            f"Disconnected all devices. {text}"
            if text != "OK"
            else "Disconnected all devices."
        )

    async def _foreground_app(self, device: str) -> str:
        result = await _run_adb(
            ["shell", "dumpsys", "activity", "activities"], device, timeout=10
        )
        for line in result.split("\n"):
            if "mResumedActivity" in line or "topResumedActivity" in line:
                parts = line.strip().split()
                for part in parts:
                    if "/" in part:
                        return f"Foreground app on {device}: {part.rstrip('}')}"
                return f"Foreground on {device}: {line.strip()}"
        return "Could not determine foreground app"
