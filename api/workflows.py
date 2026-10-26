"""Workflow record/playback API for the Android Control panel."""

import asyncio

from helpers.api import ApiHandler, Request, Response
from usr.plugins.droidclaw.helpers import workflow_engine
from usr.plugins.droidclaw.helpers.adb_backend import resolve_device


async def _serial(device) -> str:
    resolution = await asyncio.to_thread(resolve_device, device or "")
    return resolution.get("resolved_device") or ""


class Workflows(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        action = str(input.get("action") or "list").strip().lower()
        name = str(input.get("name") or "").strip()

        if action == "list":
            return {
                "success": True,
                "workflows": workflow_engine.list_workflows(),
                "recordings": workflow_engine.recording_status(),
            }
        if action == "show":
            workflow = workflow_engine.load_workflow(name)
            if not workflow:
                return {"success": False, "message": f"Workflow '{name}' not found"}
            steps = [workflow_engine.describe_step(step) for step in workflow.get("steps", [])]
            return {"success": True, "workflow": workflow, "steps": steps}
        if action == "delete":
            deleted = workflow_engine.delete_workflow(name)
            return {"success": deleted, "message": f"Deleted '{name}'" if deleted else f"Workflow '{name}' not found"}
        if action == "run_status":
            run = workflow_engine.get_run(str(input.get("run_id") or ""))
            return {"success": run is not None, "run": run}
        if action == "cancel":
            return {"success": workflow_engine.cancel_run(str(input.get("run_id") or ""))}

        serial = await _serial(input.get("device"))
        if not serial:
            return {"success": False, "message": "No connected device for this action"}

        if action == "record_start":
            return await asyncio.to_thread(
                workflow_engine.start_recording, name, serial, str(input.get("description") or ""), "panel"
            )
        if action in ("record_stop", "record_cancel"):
            return workflow_engine.stop_recording(serial, save=action == "record_stop")
        if action == "play":
            workflow = workflow_engine.load_workflow(name)
            if not workflow:
                return {"success": False, "message": f"Workflow '{name}' not found"}
            if workflow_engine.is_recording(serial):
                return {"success": False, "message": "Stop the recording on this device before playing"}
            run_id = workflow_engine.start_playback(
                workflow,
                serial,
                continue_on_error=bool(input.get("continue_on_error", False)),
                speed=float(input.get("speed", 1.0) or 1.0),
            )
            return {"success": True, "run_id": run_id, "device": serial, "total": len(workflow.get("steps", []))}

        return {"success": False, "message": f"Unknown action '{action}'"}
