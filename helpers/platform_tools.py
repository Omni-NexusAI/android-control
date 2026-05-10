"""Plugin-owned Android platform-tools discovery and installation."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_DATA_DIR = _PLUGIN_DIR / "data"
_TOOLS_DIR = _DATA_DIR / "platform-tools"

_DOWNLOADS = {
    "linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    "darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    "windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
}


def _adb_name() -> str:
    return "adb.exe" if platform.system().lower().startswith("win") else "adb"


def plugin_adb_path() -> Path:
    return _TOOLS_DIR / _adb_name()


def _configured_adb_path(config: dict | None = None) -> str:
    config = config or {}
    defaults = config.get("defaults", {}) if isinstance(config.get("defaults"), dict) else {}
    return str(config.get("adb_path") or defaults.get("adb_path") or "").strip()


def _is_file(path: str | Path) -> bool:
    try:
        return Path(path).is_file()
    except Exception:
        return False


def common_adb_paths() -> list[Path]:
    paths = [plugin_adb_path()]
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env_name)
        if root:
            paths.append(Path(root) / "platform-tools" / _adb_name())

    home = Path.home()
    paths.extend(
        [
            home / "Android" / "Sdk" / "platform-tools" / _adb_name(),
            Path("/opt/android-sdk/platform-tools") / _adb_name(),
            Path("/usr/lib/android-sdk/platform-tools") / _adb_name(),
            Path("/usr/local/android-sdk/platform-tools") / _adb_name(),
        ]
    )
    if platform.system().lower().startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            paths.append(Path(local) / "Android" / "Sdk" / "platform-tools" / _adb_name())
    return paths


def find_adb(config: dict | None = None) -> dict:
    checked: list[dict] = []

    configured = _configured_adb_path(config)
    if configured:
        checked.append({"source": "configured", "path": configured})
        if _is_file(configured):
            return {
                "available": True,
                "path": str(Path(configured)),
                "source": "configured",
                "message": "Using configured adb_path",
                "checked": checked,
            }

    path_adb = shutil.which("adb")
    if path_adb:
        checked.append({"source": "PATH", "path": path_adb})
        return {
            "available": True,
            "path": path_adb,
            "source": "PATH",
            "message": "Using adb from PATH",
            "checked": checked,
        }
    checked.append({"source": "PATH", "path": "adb"})

    seen: set[str] = set()
    for path in common_adb_paths():
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        checked.append({"source": "common", "path": key})
        if path.is_file():
            return {
                "available": True,
                "path": key,
                "source": "plugin" if path == plugin_adb_path() else "common",
                "message": "Using plugin-owned platform-tools adb" if path == plugin_adb_path() else "Using Android SDK adb",
                "checked": checked,
            }

    return {
        "available": False,
        "path": "",
        "source": "",
        "message": "ADB client was not found. Install Android platform-tools or let Android Control download plugin-owned platform-tools.",
        "checked": checked,
        "plugin_adb_path": str(plugin_adb_path()),
    }


def _platform_key() -> str:
    system = platform.system().lower()
    if system.startswith("linux"):
        return "linux"
    if system.startswith("darwin"):
        return "darwin"
    if system.startswith("win"):
        return "windows"
    raise RuntimeError(f"Unsupported platform-tools platform: {platform.system()}")


def _mark_executable(path: Path) -> None:
    if platform.system().lower().startswith("win") or not path.exists():
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def ensure_platform_tools(force: bool = False) -> dict:
    existing = plugin_adb_path()
    if existing.is_file() and not force:
        _mark_executable(existing)
        return {
            "available": True,
            "installed": False,
            "path": str(existing),
            "source": "plugin",
            "message": "Plugin-owned platform-tools adb is already installed",
        }

    key = _platform_key()
    url = _DOWNLOADS[key]
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="platform-tools-", dir=str(_DATA_DIR)) as tmp:
            tmp_dir = Path(tmp)
            archive = tmp_dir / "platform-tools.zip"
            with urllib.request.urlopen(url, timeout=60) as response:
                with archive.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
            extract_dir = tmp_dir / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as zip_file:
                zip_file.extractall(extract_dir)

            extracted_tools = extract_dir / "platform-tools"
            if not (extracted_tools / _adb_name()).is_file():
                raise RuntimeError("Downloaded platform-tools archive did not contain adb")

            if _TOOLS_DIR.exists():
                shutil.rmtree(_TOOLS_DIR)
            shutil.move(str(extracted_tools), str(_TOOLS_DIR))
    except Exception as exc:
        raise RuntimeError(f"Failed to install Android platform-tools from {url}: {exc}") from exc

    _mark_executable(existing)
    if not existing.is_file():
        raise RuntimeError(f"Android platform-tools install completed but adb is missing at {existing}")

    return {
        "available": True,
        "installed": True,
        "path": str(existing),
        "source": "plugin",
        "message": "Installed plugin-owned Android platform-tools",
        "download_url": url,
    }
