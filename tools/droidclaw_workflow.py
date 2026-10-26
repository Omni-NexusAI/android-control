"""Android Control workflows: record inputs on a phone and replay them without an LLM."""

import asyncio

from helpers.tool import Tool, Response
from usr.plugins.droidclaw.helpers import workflow_engine
from usr.plugins.droidclaw.helpers.device_context import DeviceSelectionError, claim, release_device

ACTIONS = "record_start, record_stop, record_cancel, status, play, list, show, delete"


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class DroidClawWorkflow(Tool):
    async def execute(self, **kwargs) -> Response:
        action = str(self.args.get("action") or self.method or "").strip().lower()
        name = str(self.args.get("name") or "").strip()
        device = self.args.get("device", "")

        try:
            if action == "list":
                message = self._list()
            elif action == "show":
                message = self._show(name)
            elif action == "delete":
                message = (
                    f"Deleted workflow '{name}'." if name and workflow_engine.delete_workflow(name)
                    else f"Error: workflow '{name}' not found."
                )
            elif action == "status":
                message = self._status()
            elif action == "record_start":
                serial = await claim(self.agent, device)
                result = await asyncio.to_thread(
                    workflow_engine.start_recording, name, serial, self.args.get("description", ""), "agent"
                )
                message = result["message"] + (
                    " Every input you send to this phone with adb_tools or droidclaw_run is now captured; "
                    "finish with droidclaw_workflow action record_stop." if result["success"] else ""
                )
            elif action in ("record_stop", "record_cancel"):
                serial = await claim(self.agent, device)
                result = workflow_engine.stop_recording(serial, save=action == "record_stop")
                message = result["message"]
            elif action == "play":
                message = await self._play(name, device)
            else:
                message = f"Error: unknown action '{action}'. Valid actions: {ACTIONS}"
        except DeviceSelectionError as exc:
            message = f"Error: {exc}"

        return Response(message=message, break_loop=False)

    def _list(self) -> str:
        items = workflow_engine.list_workflows()
        if not items:
            return "No saved workflows."
        lines = [f"Saved workflows ({len(items)}):"]
        for item in items:
            extra = f" - {item['description']}" if item["description"] else ""
            lines.append(f"- {item['name']} ({item['steps']} steps){extra}")
        return "\n".join(lines)

    def _show(self, name: str) -> str:
        workflow = workflow_engine.load_workflow(name) if name else None
        if not workflow:
            return f"Error: workflow '{name}' not found."
        lines = [f"Workflow '{workflow['name']}' ({len(workflow.get('steps', []))} steps)"]
        if workflow.get("description"):
            lines.append(workflow["description"])
        for index, step in enumerate(workflow.get("steps", []), start=1):
            lines.append(f"{index}. {workflow_engine.describe_step(step)}")
        return "\n".join(lines)

    def _status(self) -> str:
        active = workflow_engine.recording_status()
        if not active:
            return "No recordings in progress."
        return "\n".join(f"Recording '{r['name']}' on {r['device']}: {r['steps']} steps so far" for r in active)

    async def _play(self, name: str, device) -> str:
        workflow = workflow_engine.load_workflow(name) if name else None
        if not workflow:
            available = ", ".join(item["name"] for item in workflow_engine.list_workflows()) or "none"
            return f"Error: workflow '{name}' not found. Available: {available}"
        serial = await claim(self.agent, device)
        if workflow_engine.is_recording(serial):
            return f"Error: {serial} is recording; stop the recording before playing a workflow on it."
        try:
            speed = float(self.args.get("speed", 1.0) or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        await self.set_progress(f"Playing workflow '{workflow['name']}' on {serial}")
        try:
            report = await workflow_engine.play_workflow(
                workflow,
                serial,
                continue_on_error=_truthy(self.args.get("continue_on_error", False)),
                speed=speed,
            )
        finally:
            release_device(self.agent, serial)
        lines = [f"Workflow '{workflow['name']}' on {serial}: {report['status']} ({report['message']})"]
        for row in report["results"]:
            mark = "ok" if row["ok"] else "FAILED"
            lines.append(f"{row['step']}. {row['action']} - {mark}: {row['detail']}")
        return "\n".join(lines)
