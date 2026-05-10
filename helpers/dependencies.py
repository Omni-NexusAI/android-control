from __future__ import annotations

import importlib
import importlib.util
import shutil
import subprocess
import sys
import threading
from pathlib import Path


_LOCK = threading.Lock()
_CHECKED = False
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_REQUIREMENTS_FILE = _PLUGIN_DIR / "requirements.txt"
_REQUIRED_MODULES = ("zeroconf", "cryptography")


def missing_dependencies() -> list[str]:
    return [
        module
        for module in _REQUIRED_MODULES
        if importlib.util.find_spec(module) is None
    ]


def check_dependencies() -> dict:
    missing = missing_dependencies()
    return {
        "available": not missing,
        "missing": missing,
        "install_command": (
            f"uv pip install --python {sys.executable} -r {_REQUIREMENTS_FILE}"
            if missing
            else ""
        ),
    }


def ensure_runtime_dependencies(include_adb: bool = True) -> dict:
    """Install Python QR dependencies and, optionally, plugin-owned platform-tools."""
    ensure_dependencies()
    result = {
        "python_dependencies": check_dependencies(),
        "platform_tools": None,
    }
    if include_adb:
        from usr.plugins.droidclaw.helpers.platform_tools import ensure_platform_tools

        result["platform_tools"] = ensure_platform_tools()
    return result


def ensure_dependencies() -> None:
    global _CHECKED

    if _CHECKED and not missing_dependencies():
        return

    with _LOCK:
        missing = missing_dependencies()
        if _CHECKED and not missing:
            return
        if not missing:
            _CHECKED = True
            return

        _install_dependencies(missing)
        importlib.invalidate_caches()

        missing = missing_dependencies()
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(
                f"Android Control QR dependency still unavailable after installation: {joined}"
            )

        _CHECKED = True


def _install_dependencies(missing: list[str]) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Android Control requires 'uv' to install QR pairing dependencies automatically")
    if not _REQUIREMENTS_FILE.is_file():
        raise RuntimeError(f"Android Control requirements file not found: {_REQUIREMENTS_FILE}")

    cmd = [
        uv,
        "pip",
        "install",
        "--python",
        sys.executable,
        "-r",
        str(_REQUIREMENTS_FILE),
    ]

    try:
        subprocess.check_call(cmd, cwd=str(_PLUGIN_DIR))
    except Exception as e:
        raise RuntimeError(
            f"Failed to install Android Control QR dependencies: {e}"
        ) from e
