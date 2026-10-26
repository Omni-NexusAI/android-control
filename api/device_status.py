"""Return current ADB device status with model info."""

import asyncio

from helpers.api import ApiHandler, Request, Response
from helpers import plugins
from usr.plugins.droidclaw.helpers.adb_backend import (
    canonical_devices,
    diagnostics as adb_diagnostics,
    list_mdns_services,
    resolve_device,
    select_backend,
)
from usr.plugins.droidclaw.helpers.adb_runtime import bridge_capabilities, read_adb_health

PLUGIN_NAME = "droidclaw"


class DeviceStatus(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        # Device discovery and diagnostics spawn adb subprocesses; keep them
        # off the event loop so the A0 UI stays responsive.
        return await asyncio.to_thread(_status, input)


def _status(input: dict) -> dict:
    cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
    requested_device = input.get("device", None)
    configured_device = cfg.get("device", "")
    target_device = configured_device if requested_device is None else (requested_device or "")
    backend = select_backend()
    # light=True is the panel's periodic poll: cached device list only, no
    # diagnostics or bridge probes (those spawn many adb processes).
    light = bool(input.get("light"))

    devices = [device.to_dict() for device in canonical_devices(backend, wait_tailscale=not light)]
    mdns_services = [] if light else [service.to_dict() for service in list_mdns_services(backend)]
    resolution = resolve_device(target_device, backend)

    active_device = resolution.get("device")
    configured_available = True
    focused_available = bool(resolution.get("resolved")) if target_device else True
    if target_device and not resolution.get("resolved"):
        configured_available = False
        if requested_device is None:
            auto_resolution = resolve_device("", backend)
            active_device = auto_resolution.get("device")
        else:
            active_device = None

    if light:
        return {
            "devices": devices,
            "active_device": active_device,
            "focused_device": active_device,
            "focused_available": focused_available,
            "configured_device": configured_device,
            "configured_available": configured_available,
            "requested_device": target_device,
            "resolved_device": active_device.get("serial") if active_device else "",
            "device_resolution": resolution,
            "adb_backend": backend.name,
            "adb_backend_message": backend.message,
            "connected": active_device is not None,
            "light": True,
        }

    capabilities = bridge_capabilities(force=False)

    return {
        "devices": devices,
        "active_device": active_device,
        "focused_device": active_device,
        "focused_available": focused_available,
        "configured_device": configured_device,
        "configured_available": configured_available,
        "requested_device": target_device,
        "resolved_device": active_device.get("serial") if active_device else "",
        "device_resolution": resolution,
        "mdns_services": mdns_services,
        "adb_backend": backend.name,
        "adb_backend_message": backend.message,
        "adb_diagnostics": adb_diagnostics(),
        "adb_health": read_adb_health(),
        "capabilities": capabilities,
        "usb_visibility_message": "" if devices else "No ADB devices are visible inside the A0 container. For wired USB, Docker must expose the device to the container.",
        "connected": active_device is not None,
    }
