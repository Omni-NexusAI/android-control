"""Marketplace install hooks for Android Control."""

from __future__ import annotations


def install() -> dict:
    from usr.plugins.droidclaw.helpers.dependencies import ensure_runtime_dependencies

    return {
        "success": True,
        "message": "Android Control dependencies installed",
        "dependencies": ensure_runtime_dependencies(include_adb=True),
    }
