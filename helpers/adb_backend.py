"""Shared ADB backend selection and command helpers for Android Control."""

from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from helpers import plugins
from usr.plugins.droidclaw.helpers.platform_tools import find_adb

try:
    import yaml
except Exception:  # pragma: no cover - Agent Zero normally includes PyYAML
    yaml = None

PLUGIN_NAME = "droidclaw"
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_FILE = _PLUGIN_DIR / "default_config.yaml"


@dataclass(frozen=True)
class AdbBackend:
    name: str
    command_prefix: list[str]
    message: str = ""


@dataclass
class AdbDevice:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""
    ip: str = ""
    port: str = ""
    aliases: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "state": self.state,
            "model": self.model,
            "product": self.product,
            "device": self.device,
            "transport_id": self.transport_id,
            "ip": self.ip,
            "port": self.port,
            "aliases": self.aliases or [],
        }


@dataclass
class MdnsService:
    name: str
    service_type: str
    address: str

    @property
    def host(self) -> str:
        if ":" not in self.address:
            return self.address
        return self.address.rsplit(":", 1)[0]

    @property
    def port(self) -> str:
        if ":" not in self.address:
            return ""
        return self.address.rsplit(":", 1)[1]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "service_type": self.service_type,
            "address": self.address,
            "host": self.host,
            "port": self.port,
        }


def _load_config() -> dict:
    defaults = {}
    try:
        if yaml is not None:
            defaults = yaml.safe_load(_DEFAULT_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        defaults = {}

    try:
        configured = plugins.get_plugin_config(PLUGIN_NAME) or {}
    except Exception:
        configured = {}

    merged = dict(defaults)
    nested = configured.get("defaults") if isinstance(configured, dict) else None
    if isinstance(nested, dict):
        merged.update(nested)
    if isinstance(configured, dict):
        merged.update({key: value for key, value in configured.items() if key != "defaults"})
    return merged


def _backend_mode(config: Optional[dict] = None) -> str:
    config = config if config is not None else _load_config()
    defaults = config.get("defaults", {}) if isinstance(config.get("defaults"), dict) else {}
    mode = str(config.get("adb_backend") or defaults.get("adb_backend") or "auto")
    return mode.lower().strip()


def _host_settings(config: Optional[dict] = None) -> tuple[str, str]:
    config = config if config is not None else _load_config()
    defaults = config.get("defaults", {}) if isinstance(config.get("defaults"), dict) else {}
    host = str(config.get("adb_host") or defaults.get("adb_host") or "host.docker.internal")
    port = str(config.get("adb_port") or defaults.get("adb_port") or "5037")
    return host, port


def _adb_client(config: Optional[dict] = None) -> dict:
    return find_adb(config if config is not None else _load_config())


def _adb_prefix(config: Optional[dict] = None) -> list[str]:
    adb = _adb_client(config)
    return [adb["path"]] if adb.get("available") and adb.get("path") else ["adb"]


def _container_backend(message: str = "Using container-local ADB server", config: Optional[dict] = None) -> AdbBackend:
    return AdbBackend(name="container", command_prefix=_adb_prefix(config), message=message)


def _host_backend(host: str, port: str, message: str = "", config: Optional[dict] = None) -> AdbBackend:
    return AdbBackend(
        name="host",
        command_prefix=_adb_prefix(config) + ["-H", host, "-P", str(port)],
        message=message or f"Using host ADB server at {host}:{port}",
    )


def _probe(prefix: list[str], timeout: int = 4) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            prefix + ["mdns", "check"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "adb executable was not found"
    except subprocess.TimeoutExpired:
        return False, "adb mdns check timed out"
    except Exception as exc:
        return False, str(exc)

    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    ).strip()
    if result.returncode == 0:
        return True, output
    return False, output or f"adb mdns check exited with {result.returncode}"


def select_backend(prefer: str | None = None) -> AdbBackend:
    """Select the ADB server Android Control should use for all commands."""
    config = _load_config()
    mode = (prefer or _backend_mode(config)).lower().strip()
    host, port = _host_settings(config)
    adb = _adb_client(config)
    if not adb.get("available"):
        return _container_backend(f"ADB client is not available: {adb.get('message')}", config)

    if mode in {"container", "local"}:
        return _container_backend("Configured to use container-local ADB server", config)

    if mode == "host":
        ok, message = _probe(_adb_prefix(config) + ["-H", host, "-P", str(port)])
        if ok:
            return _host_backend(host, port, message, config)
        return _host_backend(host, port, f"Configured host ADB was not reachable: {message}", config)

    if mode in {"auto", "auto_container_first", ""}:
        container_ok, container_message = _probe(_adb_prefix(config))
        if container_ok:
            return _container_backend(
                f"Using container-local ADB server ({container_message})",
                config,
            )

        ok, message = _probe(_adb_prefix(config) + ["-H", host, "-P", str(port)])
        if ok:
            return _host_backend(
                host,
                port,
                f"Container-local ADB was not available ({container_message}); using reachable host ADB ({message})",
                config,
            )

        return _container_backend(
            f"Container-local ADB check failed ({container_message}); host ADB at {host}:{port} also failed ({message})",
            config,
        )

    if mode == "auto_host_first":
        ok, message = _probe(_adb_prefix(config) + ["-H", host, "-P", str(port)])
        if ok:
            return _host_backend(host, port, message, config)

        container_ok, container_message = _probe(_adb_prefix(config))
        if container_ok:
            return _container_backend(
                f"Host ADB at {host}:{port} was not reachable ({message}); using container-local ADB",
                config,
            )

        return _container_backend(
            f"Host ADB at {host}:{port} was not reachable ({message}); container ADB check failed ({container_message})",
            config,
        )

    ok, message = _probe(_adb_prefix(config) + ["-H", host, "-P", str(port)])
    if ok:
        return _host_backend(host, port, message, config)

    container_ok, container_message = _probe(_adb_prefix(config))
    if container_ok:
        return _container_backend(
            f"Host ADB at {host}:{port} was not reachable ({message}); using container-local ADB",
            config,
        )

    return _container_backend(
        f"Host ADB at {host}:{port} was not reachable ({message}); container ADB check failed ({container_message})",
        config,
    )


def adb_cmd(args: list[str], device: str | None = None, backend: AdbBackend | None = None) -> list[str]:
    selected = backend or select_backend()
    cmd = list(selected.command_prefix)
    if device:
        cmd.extend(["-s", device])
    cmd.extend([str(arg) for arg in args])
    return cmd


def _extract_ip_port(value: str) -> tuple[str, str]:
    match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?", value or "")
    if not match:
        return "", ""
    return match.group(1), match.group(2) or ""


def parse_devices(output: str) -> list[AdbDevice]:
    devices: list[AdbDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        state = parts[1]
        fields = {}
        for part in parts[2:]:
            if ":" in part:
                key, value = part.split(":", 1)
                fields[key] = value

        ip, port = _extract_ip_port(serial)
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                model=fields.get("model", ""),
                product=fields.get("product", ""),
                device=fields.get("device", ""),
                transport_id=fields.get("transport_id", ""),
                ip=ip,
                port=port,
                aliases=[],
            )
        )
    return devices


def parse_mdns_services(output: str) -> list[MdnsService]:
    services: list[MdnsService] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of discovered"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        address = parts[-1]
        service_type = parts[-2]
        name = " ".join(parts[:-2])
        if service_type.startswith("_adb"):
            services.append(MdnsService(name=name, service_type=service_type, address=address))
    return services


def list_mdns_services(backend: AdbBackend | None = None) -> list[MdnsService]:
    try:
        result = run_adb_result(["mdns", "services"], timeout=8, backend=backend, resolve=False)
    except Exception:
        return []
    return parse_mdns_services(result["output"])


def list_connected_devices(backend: AdbBackend | None = None) -> list[AdbDevice]:
    try:
        result = run_adb_result(["devices", "-l"], timeout=8, backend=backend, resolve=False)
    except Exception:
        return []
    return parse_devices(result["output"])


def _device_key(device: AdbDevice) -> str:
    if device.ip:
        return f"ip:{device.ip}"
    if device.product and device.model:
        return f"product:{device.product}:{device.model}"
    return f"serial:{device.serial}"


def canonical_devices(backend: AdbBackend | None = None) -> list[AdbDevice]:
    devices = [d for d in list_connected_devices(backend) if d.state == "device"]
    mdns_by_ip: dict[str, list[MdnsService]] = {}
    for service in list_mdns_services(backend):
        if service.host:
            mdns_by_ip.setdefault(service.host, []).append(service)

    grouped: dict[str, AdbDevice] = {}
    for device in devices:
        aliases = {device.serial}
        if device.ip:
            aliases.add(device.ip)
            if device.port:
                aliases.add(f"{device.ip}:{device.port}")
        if device.ip in mdns_by_ip:
            for service in mdns_by_ip[device.ip]:
                aliases.add(service.address)
                aliases.add(service.name)
                aliases.add(f"{service.name}.{service.service_type}")

        device.aliases = sorted(aliases)
        key = _device_key(device)
        if not device.ip and device.product and device.model:
            for existing_key, existing_device in grouped.items():
                if existing_device.product == device.product and existing_device.model == device.model:
                    key = existing_key
                    break
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = device
            continue

        existing_aliases = set(existing.aliases or [])
        existing_aliases.update(device.aliases or [])
        prefer_new = bool(device.ip and not existing.ip)
        if prefer_new:
            device.aliases = sorted(existing_aliases)
            grouped[key] = device
        else:
            existing.aliases = sorted(existing_aliases)

    return list(grouped.values())


def resolve_device(device: str | None = None, backend: AdbBackend | None = None) -> dict:
    requested = (device or "").strip()
    if requested.lower() in {"auto", "(auto)", "auto-detect"}:
        requested = ""

    selected = backend or select_backend()
    devices = canonical_devices(selected)
    connected = list_connected_devices(selected)

    if requested:
        for raw in connected:
            if raw.serial == requested and raw.state == "device":
                return {
                    "requested_device": device or "",
                    "resolved_device": raw.serial,
                    "device": raw.to_dict(),
                    "devices": [d.to_dict() for d in devices],
                    "adb_backend": selected.name,
                    "adb_backend_message": selected.message,
                    "resolved": True,
                    "reason": "exact",
                }

        req_ip, _ = _extract_ip_port(requested)
        for dev in devices:
            aliases = set(dev.aliases or [])
            if requested in aliases or (req_ip and dev.ip == req_ip):
                return {
                    "requested_device": device or "",
                    "resolved_device": dev.serial,
                    "device": dev.to_dict(),
                    "devices": [d.to_dict() for d in devices],
                    "adb_backend": selected.name,
                    "adb_backend_message": selected.message,
                    "resolved": True,
                    "reason": "alias" if requested in aliases else "matching_ip",
                }

        return {
            "requested_device": device or "",
            "resolved_device": "",
            "device": None,
            "devices": [d.to_dict() for d in devices],
            "adb_backend": selected.name,
            "adb_backend_message": selected.message,
            "resolved": False,
            "reason": "not_connected",
        }

    if devices:
        dev = devices[0]
        return {
            "requested_device": device or "",
            "resolved_device": dev.serial,
            "device": dev.to_dict(),
            "devices": [d.to_dict() for d in devices],
            "adb_backend": selected.name,
            "adb_backend_message": selected.message,
            "resolved": True,
            "reason": "auto",
        }

    return {
        "requested_device": device or "",
        "resolved_device": "",
        "device": None,
        "devices": [],
        "adb_backend": selected.name,
        "adb_backend_message": selected.message,
        "resolved": False,
        "reason": "no_devices",
    }


def parse_adb_command(command: str) -> list[str]:
    command = (command or "").strip()
    if not command:
        return []
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if parts and parts[0] == "adb":
        parts = parts[1:]
    if parts and parts[0] == "shell":
        shell_text = command
        if shell_text.startswith("adb "):
            shell_text = shell_text[4:].lstrip()
        if shell_text.startswith("shell "):
            shell_text = shell_text[6:].lstrip()
        return ["shell", shell_text]
    return parts


def run_adb_result(
    args: list[str],
    device: str | None = None,
    timeout: int = 15,
    input_text: str | None = None,
    backend: AdbBackend | None = None,
    resolve: bool = True,
) -> dict:
    selected = backend or select_backend()
    resolution = resolve_device(device, selected) if resolve and device is not None else {
        "requested_device": device or "",
        "resolved_device": device or "",
        "resolved": bool(device) if device is not None else True,
        "reason": "raw" if device else "none",
        "device": None,
    }
    resolved_device = resolution.get("resolved_device") or None
    if resolve and device is not None and not resolution.get("resolved"):
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"ADB device is not connected: {device}",
            "output": f"ADB device is not connected: {device}",
            "backend": selected.name,
            "backend_message": selected.message,
            "cmd": [],
            "requested_device": device or "",
            "resolved_device": "",
            "device_resolution": resolution,
        }
    cmd = adb_cmd(args, device=resolved_device, backend=selected)
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError:
        message = f"ADB client was not found at command path: {cmd[0] if cmd else 'adb'}"
        return {
            "returncode": 127,
            "stdout": "",
            "stderr": message,
            "output": message,
            "backend": selected.name,
            "backend_message": selected.message,
            "cmd": cmd,
            "requested_device": device or "",
            "resolved_device": resolved_device or "",
            "device_resolution": resolution,
        }
    except subprocess.TimeoutExpired:
        message = f"ADB command timed out after {timeout}s"
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": message,
            "output": message,
            "backend": selected.name,
            "backend_message": selected.message,
            "cmd": cmd,
            "requested_device": device or "",
            "resolved_device": resolved_device or "",
            "device_resolution": resolution,
        }
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    output = "\n".join(part for part in (stdout, stderr) if part).strip()
    return {
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "output": output,
        "backend": selected.name,
        "backend_message": selected.message,
        "cmd": cmd,
        "requested_device": device or "",
        "resolved_device": resolved_device or "",
        "device_resolution": resolution,
    }


async def run_adb_async(
    args: list[str],
    device: str | None = None,
    timeout: int = 15,
    input_text: str | None = None,
    backend: AdbBackend | None = None,
    resolve: bool = True,
) -> dict:
    selected = backend or select_backend()
    resolution = resolve_device(device, selected) if resolve and device is not None else {
        "requested_device": device or "",
        "resolved_device": device or "",
        "resolved": bool(device) if device is not None else True,
        "reason": "raw" if device else "none",
        "device": None,
    }
    resolved_device = resolution.get("resolved_device") or None
    if resolve and device is not None and not resolution.get("resolved"):
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"ADB device is not connected: {device}",
            "output": f"ADB device is not connected: {device}",
            "backend": selected.name,
            "backend_message": selected.message,
            "cmd": [],
            "requested_device": device or "",
            "resolved_device": "",
            "device_resolution": resolution,
        }
    cmd = adb_cmd(args, device=resolved_device, backend=selected)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_text.encode() if input_text is not None else None),
            timeout=timeout,
        )
    except FileNotFoundError:
        message = f"ADB client was not found at command path: {cmd[0] if cmd else 'adb'}"
        return {
            "returncode": 127,
            "stdout": "",
            "stderr": message,
            "output": message,
            "backend": selected.name,
            "backend_message": selected.message,
            "cmd": cmd,
            "requested_device": device or "",
            "resolved_device": resolved_device or "",
            "device_resolution": resolution,
        }
    except asyncio.TimeoutError:
        message = f"ADB command timed out after {timeout}s"
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": message,
            "output": message,
            "backend": selected.name,
            "backend_message": selected.message,
            "cmd": cmd,
            "requested_device": device or "",
            "resolved_device": resolved_device or "",
            "device_resolution": resolution,
        }
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    output = "\n".join(part for part in (out, err) if part).strip()
    return {
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "output": output,
        "backend": selected.name,
        "backend_message": selected.message,
        "cmd": cmd,
        "requested_device": device or "",
        "resolved_device": resolved_device or "",
        "device_resolution": resolution,
    }


def get_connected_device_serials(backend: AdbBackend | None = None) -> list[str]:
    return [device.serial for device in canonical_devices(backend)]


def diagnostics() -> dict:
    config = _load_config()
    host, port = _host_settings(config)
    adb = _adb_client(config)
    selected = select_backend()
    adb_prefix = _adb_prefix(config)
    host_ok, host_message = _probe(adb_prefix + ["-H", host, "-P", str(port)])
    container_ok, container_message = _probe(adb_prefix)
    services = ""
    try:
        services = run_adb_result(["mdns", "services"], timeout=8, backend=selected, resolve=False)["output"]
    except Exception as exc:
        services = str(exc)

    return {
        "mode": _backend_mode(config),
        "selected": selected.name,
        "selected_message": selected.message,
        "host": host,
        "port": str(port),
        "adb_client": adb,
        "adb_path": adb.get("path") or "",
        "adb_client_available": bool(adb.get("available")),
        "adb_client_message": adb.get("message") or "",
        "host_available": host_ok,
        "host_message": host_message,
        "container_available": container_ok,
        "container_message": container_message,
        "mdns_services": services,
    }
