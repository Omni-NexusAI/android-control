"""Live H.264 stream of a device screen (see helpers/scrcpy_stream.py).

POST {"device", "fps", "max_size"} -> chunked binary stream for the browser's
WebCodecs decoder. On failure returns JSON {"success": false, "fallback":
"poll"} so the viewer can switch to screenshot polling.
The first packet carries a viewer id; the browser must POST {"action":
"ping", "viewer": id} every few seconds and {"action": "close"} when done.
Actions "status", "stop", "keyframe", "ping" and "close" return JSON.
"""

import asyncio

from helpers.api import ApiHandler, Request, Response
from usr.plugins.droidclaw.helpers import scrcpy_stream


class Stream(ApiHandler):
    async def process(self, input: dict, request: Request):
        action = str(input.get("action") or "open").lower()
        device = input.get("device") or ""

        if action == "status":
            return {"success": True, "sessions": scrcpy_stream.sessions_status(), "version": scrcpy_stream.SCRCPY_VERSION}
        if action == "stop":
            return {"success": await asyncio.to_thread(scrcpy_stream.stop_session, device)}
        if action == "ping":
            return {"success": scrcpy_stream.ping(str(input.get("viewer") or ""))}
        if action == "close":
            return {"success": scrcpy_stream.close_viewer(str(input.get("viewer") or ""))}
        if action == "keyframe":
            session = await asyncio.to_thread(scrcpy_stream.running_session, device)
            return {"success": bool(session and session.request_keyframe())}

        try:
            session = await asyncio.to_thread(
                scrcpy_stream.get_session, device, input.get("fps"), input.get("max_size")
            )
        except Exception as exc:
            return {"success": False, "fallback": "poll", "message": str(exc)}

        subscriber = session.subscribe()
        return Response(
            scrcpy_stream.iter_stream(session, subscriber),
            status=200,
            mimetype="application/octet-stream",
            headers={
                # Bypass A0's gzip middleware (it buffers) and any proxy buffering.
                "Content-Encoding": "identity",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-Droidclaw-Fps": str(session.fps),
                "X-Droidclaw-Device": session.serial,
            },
            direct_passthrough=True,
        )
