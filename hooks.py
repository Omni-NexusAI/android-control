"""Marketplace install hooks for Android Control."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


PLUGIN_SKILL_NAMESPACE = "android_control"


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent


def _skills_destination_root() -> Path:
    try:
        from helpers import files

        return Path(files.get_abs_path("usr", "skills"))
    except Exception:
        return Path("/a0/usr/skills")


def ensure_bundled_skills() -> dict[str, Any]:
    """Install plugin-bundled skills into A0's normal skills directory."""

    source_root = _plugin_root() / "skills"
    dest_root = _skills_destination_root() / PLUGIN_SKILL_NAMESPACE
    result: dict[str, Any] = {
        "success": True,
        "source": str(source_root),
        "destination": str(dest_root),
        "installed": [],
        "skipped": [],
        "message": "No bundled Android Control skills found",
    }

    if not source_root.exists():
        return result

    dest_root.mkdir(parents=True, exist_ok=True)

    for skill_md in sorted(source_root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        rel = skill_dir.relative_to(source_root)
        target = dest_root / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_dir, target, dirs_exist_ok=True)
            result["installed"].append(str(target))
        except Exception as exc:
            result["success"] = False
            result["skipped"].append({"skill": str(skill_dir), "error": str(exc)})

    count = len(result["installed"])
    result["message"] = f"Installed {count} bundled Android Control skill(s)"
    return result


def install() -> dict[str, Any]:
    from usr.plugins.droidclaw.helpers.dependencies import ensure_runtime_dependencies

    return {
        "success": True,
        "message": "Android Control dependencies and bundled skills installed",
        "dependencies": ensure_runtime_dependencies(include_adb=True),
        "skills": ensure_bundled_skills(),
    }
