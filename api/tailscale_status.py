"""Tailscale status and management endpoint for Android Control."""

import asyncio
import logging

from helpers.api import ApiHandler, Request, Response
from helpers import plugins
from usr.plugins.droidclaw.helpers.tailscale_discovery import (
    discover_adb_port_candidates,
    discover_adb_peers,
    close_adb_forward,
    ensure_adb_forward,
    forwarded_adb_serial,
    generate_auth_url,
    get_tailscale_status,
    get_tailscale_status_json,
    probe_adb_port,
    resolve_tailscale_ip,
    tailscale_diagnostics,
    ensure_daemon_running,
    invalidate_cache,
)
from usr.plugins.droidclaw.helpers.adb_backend import run_adb_async, select_backend

logger = logging.getLogger("droidclaw")

PLUGIN_NAME = "droidclaw"


def _configured_adb_port(cfg: dict, override=None) -> int:
    defaults = cfg.get("defaults")
    value = override
    if value in (None, ""):
        value = cfg.get("tailscale_adb_port", 5555)
    if isinstance(defaults, dict) and value in (None, "", 5555, "5555"):
        value = defaults.get("tailscale_adb_port", value)
    try:
        port = int(str(value).strip())
    except Exception:
        port = 5555
    if port < 1 or port > 65535:
        port = 5555
    return port


def _peer_target(peer: dict) -> str:
    return (peer.get("dns_name") or peer.get("hostname") or peer.get("ip") or "").rstrip(".")


def _match_key(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


async def _connected_device_identity(serial: str) -> list[str]:
    if not serial:
        return []
    probes = [
        ["shell", "settings", "get", "global", "device_name"],
        ["shell", "getprop", "ro.product.model"],
        ["shell", "getprop", "ro.product.vendor.model"],
        ["shell", "getprop", "ro.product.device"],
        ["shell", "getprop", "ro.product.manufacturer"],
    ]
    identities = [serial]
    for prefer in ("host", "container"):
        backend = select_backend(prefer)
        for args in probes:
            result = await run_adb_async(
                args,
                device=serial,
                backend=backend,
                resolve=False,
                timeout=6,
            )
            value = (result.get("output") or "").strip()
            if result.get("returncode") == 0 and value and value.lower() != "null":
                identities.append(value)
        if len(identities) > 1:
            break
    deduped = []
    for value in identities:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _select_peer(peers: list[dict], hostname: str = "", identities: list[str] | None = None) -> dict | None:
    requested = (hostname or "").lower().rstrip(".")
    if requested:
        for peer in peers:
            values = {
                str(peer.get("hostname") or "").lower().rstrip("."),
                str(peer.get("dns_name") or "").lower().rstrip("."),
                str(peer.get("ip") or "").lower(),
                str(peer.get("node_id") or "").lower(),
            }
            if requested in values:
                return peer
    identity_keys = [_match_key(value) for value in (identities or []) if _match_key(value)]
    if identity_keys:
        for peer in peers:
            peer_values = [
                str(peer.get("hostname") or ""),
                str(peer.get("dns_name") or ""),
            ]
            peer_keys = [_match_key(value) for value in peer_values if _match_key(value)]
            for identity in identity_keys:
                for peer_key in peer_keys:
                    if identity and peer_key and (identity in peer_key or peer_key in identity):
                        return peer
    reachable = [peer for peer in peers if peer.get("online") and peer.get("adb_reachable")]
    if reachable:
        return reachable[0]
    online = [peer for peer in peers if peer.get("online")]
    if len(online) == 1:
        return online[0]
    return None


async def _connect_tailscale_adb(hostname: str, port: int) -> dict:
    ip = resolve_tailscale_ip(hostname)
    if not ip:
        return {
            "success": False,
            "message": f"Peer not found: {hostname}",
        }

    ip_port = f"{ip}:{port}"
    reachable = await probe_adb_port(ip, port, timeout=3.0)
    if not reachable:
        return {
            "success": False,
            "message": (
                f"Tailscale sees {hostname}, but ADB is not reachable at {ip_port}. "
                "Enable Allow incoming connections in the phone's Tailscale settings. Then use the "
                "port shown by Android Wireless debugging, or run Connect with Tailscale on a "
                "wired/LAN-visible device to request classic ADB TCP/IP on the selected port."
            ),
            "ip": ip,
            "port": port,
            "port_reachable": False,
        }

    forward = ensure_adb_forward(ip, port)
    if not forward.get("success"):
        return {
            "success": False,
            "message": forward.get("message") or f"Could not create a Tailscale ADB forward for {ip_port}.",
            "ip": ip,
            "port": port,
            "port_reachable": True,
        }
    local_endpoint = forward["local_endpoint"]
    result = await run_adb_async(
        ["connect", local_endpoint],
        timeout=15,
        backend=select_backend("container"),
        resolve=False,
    )
    combined = result["output"]

    if (
        "connected" in combined.lower()
        or "already connected" in combined.lower()
    ):
        return {
            "success": True,
            "message": f"Connected to {ip_port}",
            "serial": ip_port,
            "adb_backend": result["backend"],
            "ip": ip,
            "port": port,
            "forwarded_endpoint": local_endpoint,
            "port_reachable": True,
        }

    close_adb_forward(ip, port)
    if "authenticate" in combined.lower() or "unauthorized" in combined.lower():
        return {
            "success": False,
            "message": (
                f"Reached ADB on {ip_port} through Tailscale, but the phone rejected this computer's ADB key. "
                "Accept the Allow USB debugging prompt on the phone, then connect again."
            ),
            "adb_backend": result["backend"],
            "ip": ip,
            "port": port,
            "port_reachable": True,
            "authentication_required": True,
        }
    return {
        "success": False,
        "message": f"ADB connect failed for {ip_port}: {combined}",
        "adb_backend": result["backend"],
        "ip": ip,
        "port": port,
        "port_reachable": True,
    }


class TailscaleStatus(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
        action = input.get("action", "status")

        if action == "status":
            ensure_daemon_running(timeout=8)
            invalidate_cache()
            ts_status = get_tailscale_status()
            auth_url = ""
            if ts_status.running and not ts_status.logged_in:
                status_json = get_tailscale_status_json(use_cache=False) or {}
                auth_url = status_json.get("AuthURL") or ""
            peers = []
            if ts_status.running and ts_status.logged_in:
                adb_port = _configured_adb_port(cfg, input.get("port"))
                peers = [
                    p.to_dict()
                    for p in await discover_adb_peers(
                        config={
                            "tailscale_os_filter": ["android"],
                            "tailscale_online_only": False,
                            "tailscale_adb_port": adb_port,
                            "manual_adb_port": input.get("port"),
                        },
                        probe_ports=True,
                    )
                ]
            return {
                "success": True,
                "tailscale": ts_status.to_dict(),
                "peers": peers,
                "auth_url": auth_url,
                "adb_port_candidates": discover_adb_port_candidates({"tailscale_adb_port": _configured_adb_port(cfg)})[0],
                "diagnostics": tailscale_diagnostics(),
            }

        elif action == "connect":
            hostname = input.get("hostname", "")
            if not hostname:
                return {
                    "success": False,
                    "message": "hostname is required",
                }
            try:
                port = _configured_adb_port(cfg, input.get("port"))
                return await _connect_tailscale_adb(hostname, port)
            except Exception as e:
                logger.error(f"Tailscale ADB connect failed: {e}")
                return {"success": False, "message": str(e)}

        elif action == "refresh":
            ensure_daemon_running(timeout=8)
            invalidate_cache()
            adb_port = _configured_adb_port(cfg, input.get("port"))
            peers = await discover_adb_peers(
                config={
                    "tailscale_os_filter": ["android"],
                    "tailscale_online_only": False,
                    "tailscale_adb_port": adb_port,
                    "manual_adb_port": input.get("port"),
                },
                probe_ports=True,
            )
            return {
                "success": True,
                "peers": [p.to_dict() for p in peers],
                "adb_port_candidates": discover_adb_port_candidates({
                    "tailscale_adb_port": adb_port,
                    "manual_adb_port": input.get("port"),
                })[0],
            }

        elif action == "start_daemon":
            ok, msg = ensure_daemon_running()
            return {
                "success": ok,
                "message": msg,
            }

        elif action == "disconnect":
            hostname = input.get("hostname", "")
            if not hostname:
                return {
                    "success": False,
                    "message": "hostname is required",
                }
            port = _configured_adb_port(cfg, input.get("port"))
            ip = resolve_tailscale_ip(hostname)
            if not ip:
                return {
                    "success": False,
                    "message": f"Peer not found: {hostname}",
                }
            ip_port = f"{ip}:{port}"
            try:
                local_endpoint = forwarded_adb_serial(ip_port)
                disconnect_target = local_endpoint or ip_port
                result = await run_adb_async(
                    ["disconnect", disconnect_target],
                    timeout=10,
                    backend=select_backend("container"),
                    resolve=False,
                )
                close_adb_forward(ip, port)
                combined = result["output"]

                if "disconnected" in combined.lower() or "not connected" in combined.lower():
                    return {
                        "success": True,
                        "message": f"Disconnected from {ip_port}",
                        "adb_backend": result["backend"],
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Disconnect failed: {combined}",
                        "adb_backend": result["backend"],
                    }
            except Exception as e:
                logger.error(f"Tailscale ADB disconnect failed: {e}")
                return {"success": False, "message": str(e)}

        elif action == "bootstrap":
            serial = input.get("serial", "")
            if not serial:
                return {"success": False, "message": "serial is required"}
            try:
                from usr.plugins.droidclaw.helpers.tailscale_discovery import bootstrap_tailscale
                result = bootstrap_tailscale(serial, auth_url=input.get("auth_url", ""))
                return result
            except Exception as e:
                logger.error(f"Tailscale bootstrap failed: {e}")
                return {"success": False, "message": str(e)}

        elif action == "setup_connect":
            serial = (input.get("serial") or "").strip()
            hostname = (input.get("hostname") or "").strip()
            port = _configured_adb_port(cfg, input.get("port"))
            steps = []
            identities = []

            ok, daemon_msg = ensure_daemon_running(timeout=12)
            if not ok:
                return {
                    "success": False,
                    "stage": "a0_tailscale_daemon",
                    "message": f"Could not start Android Control Tailscale daemon: {daemon_msg}",
                    "steps": steps,
                }

            invalidate_cache()
            ts_status = get_tailscale_status()
            if not ts_status.logged_in:
                auth_result = generate_auth_url()
                return {
                    "success": False,
                    "auth_required": True,
                    "stage": "a0_tailscale_auth",
                    "auth_url": auth_result.get("auth_url", ""),
                    "message": "Authorize the Android Control A0/container node on Tailscale, then run Connect with Tailscale again.",
                    "steps": steps,
                }
            steps.append("Android Control A0/container node is on Tailscale.")

            if serial:
                identities = await _connected_device_identity(serial)
                if identities:
                    display_identity = next((value for value in identities if value != serial), identities[0])
                    steps.append(f"Matched connected device identity: {display_identity}.")
                try:
                    launch = await run_adb_async(
                        ["shell", "monkey", "-p", "com.tailscale.ipn", "-c", "android.intent.category.LAUNCHER", "1"],
                        device=serial,
                        timeout=10,
                    )
                    if launch["returncode"] == 0:
                        steps.append("Opened Tailscale on the selected Android device.")
                    else:
                        steps.append("Could not open Tailscale automatically; confirm the phone is signed in manually.")
                except Exception:
                    steps.append("Could not open Tailscale automatically; confirm the phone is signed in manually.")

                tcp_result = await run_adb_async(["tcpip", str(port)], device=serial, timeout=15)
                if tcp_result["returncode"] != 0:
                    return {
                        "success": False,
                        "stage": "adb_tcpip",
                        "message": tcp_result["output"] or f"Could not request ADB TCP/IP on port {port}.",
                        "adb_backend": tcp_result["backend"],
                        "steps": steps,
                    }
                steps.append(f"Requested classic ADB TCP/IP on port {port}.")
                await asyncio.sleep(1.5)

            peers = [
                p.to_dict()
                for p in await discover_adb_peers(
                    config={
                        "tailscale_os_filter": ["android"],
                        "tailscale_online_only": False,
                        "tailscale_adb_port": port,
                        "manual_adb_port": port,
                    },
                    probe_ports=True,
                )
            ]
            if not peers:
                return {
                    "success": False,
                    "stage": "phone_tailnet",
                    "message": "No Android Tailscale peers were found. Sign the target phone into the same tailnet, then refresh.",
                    "peers": peers,
                    "steps": steps,
                }

            target = _select_peer(peers, hostname, identities)
            if not target:
                return {
                    "success": False,
                    "stage": "select_peer",
                    "message": "Multiple Android Tailscale peers are online. Choose the target peer from Tailscale Remote Devices.",
                    "peers": peers,
                    "steps": steps,
                }

            target_host = _peer_target(target)
            target_port = int(target.get("adb_port") or port)
            if not target.get("online"):
                return {
                    "success": False,
                    "stage": "phone_tailnet",
                    "message": f"{target_host} is known to the tailnet but is currently offline. Open Tailscale on that phone, then try again.",
                    "peers": peers,
                    "steps": steps,
                }
            if not target.get("adb_reachable"):
                return {
                    "success": False,
                    "stage": "adb_port",
                    "message": (
                        f"{target_host} is online in Tailscale, but ADB is not reachable on "
                        f"{target.get('ip')}:{target_port}. Enable Allow incoming connections in "
                        "the phone's Tailscale settings, then check the Wireless debugging connect "
                        "port or try a manual port override."
                    ),
                    "peers": peers,
                    "steps": steps,
                }

            result = await _connect_tailscale_adb(target_host, target_port)
            result["peers"] = peers
            result["steps"] = steps
            result["stage"] = "adb_connect"
            return result

        elif action == "open_phone_tailscale":
            serial = input.get("serial", "")
            if not serial:
                return {"success": False, "message": "serial is required"}
            try:
                launch = await run_adb_async(
                    ["shell", "monkey", "-p", "com.tailscale.ipn", "-c", "android.intent.category.LAUNCHER", "1"],
                    device=serial,
                    timeout=10,
                )
                if launch["returncode"] == 0:
                    return {
                        "success": True,
                        "message": "Opened Tailscale on the selected Android device. Sign into the same tailnet there, then refresh remote devices.",
                        "adb_backend": launch["backend"],
                    }

                fallback = await run_adb_async(
                    [
                        "shell",
                        "am",
                        "start",
                        "-a",
                        "android.intent.action.VIEW",
                        "-d",
                        "https://tailscale.com/download/android",
                    ],
                    device=serial,
                    timeout=10,
                )
                return {
                    "success": fallback["returncode"] == 0,
                    "message": (
                        "Opened Tailscale download/setup page on the selected Android device."
                        if fallback["returncode"] == 0
                        else f"Could not open Tailscale on the selected device: {fallback['output']}"
                    ),
                    "adb_backend": fallback["backend"],
                }
            except Exception as e:
                logger.error(f"Open phone Tailscale setup failed: {e}")
                return {"success": False, "message": str(e)}

        elif action == "enable_adb_tcp":
            serial = input.get("serial", "")
            port = str(_configured_adb_port(cfg, input.get("port")))
            if not serial:
                return {"success": False, "message": "serial is required"}
            try:
                result = await run_adb_async(["tcpip", port], device=serial, timeout=15)
                return {
                    "success": result["returncode"] == 0,
                    "message": result["output"] or f"ADB TCP/IP requested on port {port}",
                    "adb_backend": result["backend"],
                }
            except Exception as e:
                logger.error(f"Enable ADB TCP/IP failed: {e}")
                return {"success": False, "message": str(e)}

        elif action == "generate_auth_url":
            try:
                return generate_auth_url()
            except Exception as e:
                return {"success": False, "message": str(e)}

        return {
            "success": False,
            "message": f"Unknown action: {action}",
        }
