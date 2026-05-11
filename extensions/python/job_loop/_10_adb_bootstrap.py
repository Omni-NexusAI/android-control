from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


class DroidClawAdbBootstrap(Extension):
    async def execute(self, **kwargs: Any) -> None:
        try:
            from usr.plugins.droidclaw.helpers.adb_runtime import bootstrap_adb_runtime

            state = bootstrap_adb_runtime(force=False)
            if state.get("success"):
                return
            message = state.get("message") or "Android Control ADB bootstrap did not complete"
            PrintStyle.warning(f"Android Control: {message}")
        except Exception as exc:
            PrintStyle.warning(f"Android Control: ADB bootstrap failed: {exc}")
