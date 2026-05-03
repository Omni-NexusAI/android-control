"""Task dispatch API endpoint for Android Control."""

import uuid

from agent import UserMessage
from helpers.api import ApiHandler, Request, Response
from helpers import message_queue as mq, plugins
from helpers.state_monitor_integration import mark_dirty_for_context

PLUGIN_NAME = "droidclaw"


class RunTask(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        goal = input.get("goal", "").strip()
        if not goal:
            return {"success": False, "message": "goal is required"}

        dispatch = input.get("dispatch", "chat_and_panel")
        if dispatch == "direct":
            return await self._run_direct(input, goal)

        context_id = input.get("context", "") or input.get("context_id", "")
        context = self.use_context(context_id)
        device = (input.get("device") or "").strip()
        device_label = (input.get("device_label") or device or "Auto").strip()
        prompt = _build_chat_prompt(goal, device_label, device)
        message_id = str(uuid.uuid4())
        running = context.is_running()

        if running:
            mq.add(context, prompt, [], message_id)
            mark_dirty_for_context(context.id, reason="droidclaw.run_task.queue")
            return {
                "success": True,
                "mode": "chat_and_panel",
                "context": context.id,
                "message_id": message_id,
                "queued": True,
                "status": "queued",
                "headline": "Queued in current chat",
                "message": "The Android Control task was queued because the current chat is already running.",
                "device": device,
                "device_label": device_label,
            }

        mq.log_user_message(context, prompt, [], message_id=message_id, source=" (Android Control panel)")
        context.communicate(UserMessage(message=prompt, attachments=[], id=message_id))
        mark_dirty_for_context(context.id, reason="droidclaw.run_task.dispatch")
        return {
            "success": True,
            "mode": "chat_and_panel",
            "context": context.id,
            "message_id": message_id,
            "queued": False,
            "status": "sent",
            "headline": "Sent to current chat",
            "message": "The Android Control task was sent to the current chat.",
            "device": device,
            "device_label": device_label,
        }

    async def _run_direct(self, input: dict, goal: str) -> dict:
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
        try:
            from usr.plugins.droidclaw.tools.droidclaw_run import DroidClawRun
        except ImportError:
            from tools.droidclaw_run import DroidClawRun

        args = dict(cfg)
        args.update(input)
        tool = DroidClawRun(
            agent=None,
            name="droidclaw_run",
            method=None,
            args=args,
            message=goal,
            loop_data=None,
        )
        result = await tool.execute()

        return {
            "success": not result.message.startswith("Error:"),
            "mode": "direct",
            "context": "",
            "message_id": "",
            "queued": False,
            "status": "completed" if not result.message.startswith("Error:") else "failed",
            "headline": "Direct task completed" if not result.message.startswith("Error:") else "Direct task failed",
            "message": result.message,
        }


def _build_chat_prompt(goal: str, device_label: str, device: str = "") -> str:
    target_line = f"Target device: {device_label or 'Auto'}"
    device_instruction = (
        f"When calling Android Control tools, pass device: {device}"
        if device
        else "Target mode: Auto. Use Android Control's normal device auto-detection unless the user goal names a different target."
    )
    return (
        "Android Control panel task\n"
        f"{target_line}\n"
        f"{device_instruction}\n"
        f"User goal: {goal}\n\n"
        "Use Android Control Android/ADB tools to complete this task on the target device. "
        "If the user goal explicitly names a different device or says to use Auto, follow the user goal."
    )
