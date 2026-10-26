import asyncio
import json
from xml.sax.saxutils import quoteattr

import pytest

from usr.plugins.droidclaw.helpers import workflow_engine as wf
from usr.plugins.droidclaw.helpers.ui_parser import parse_ui_xml


def node(text="", rid="", desc="", bounds="[0,0][10,10]", clickable=True, cls="android.widget.TextView"):
    return (
        f"<node text={quoteattr(text)} content-desc={quoteattr(desc)} resource-id=\"{rid}\" class=\"{cls}\" "
        f"package=\"com.android.settings\" clickable=\"{str(clickable).lower()}\" enabled=\"true\" bounds=\"{bounds}\" />"
    )


def screen(*nodes, size="[0,0][1080,2400]"):
    return (
        '<?xml version="1.0"?><hierarchy rotation="0"><node text="" resource-id="" class="android.widget.FrameLayout" '
        f'clickable="false" enabled="true" bounds="{size}">' + "".join(nodes) + "</node></hierarchy>"
    )


PHONE_A = screen(
    node("Network & internet", rid="android:id/title", bounds="[0,600][1080,760]"),
    node("Display", rid="android:id/title", bounds="[0,760][1080,920]"),
)
# Same screen on a smaller phone, list shifted: "Display" moved.
PHONE_B = screen(
    node("Network & internet", rid="android:id/title", bounds="[0,300][720,400]"),
    node("Battery", rid="android:id/title", bounds="[0,400][720,500]"),
    node("Display", rid="android:id/title", bounds="[0,500][720,600]"),
    size="[0,0][720,1600]",
)


@pytest.fixture(autouse=True)
def _wf_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "WORKFLOWS_DIR", tmp_path / "workflows")
    monkeypatch.setattr(wf, "_recordings", {})
    monkeypatch.setattr(wf, "_runs", {})
    from usr.plugins.droidclaw.helpers import device_context

    monkeypatch.setattr(device_context, "_leases", {})


def _setup(fake_adb):
    fake_adb.add_device("PHONE_A", model="Pixel_8")
    fake_adb.add_device("PHONE_B", model="Pixel_4a")
    fake_adb.state["shell_outputs"] = {
        "uiautomator dump /dev/tty": {"PHONE_A": PHONE_A, "PHONE_B": PHONE_B},
        "wm size": {"PHONE_A": "Physical size: 1080x2400\n", "PHONE_B": "Physical size: 720x1600\n"},
        "getprop ro.product.model": {"PHONE_A": "Pixel 8\n", "PHONE_B": "Pixel 4a\n"},
        "monkey": "Events injected: 1\n",
    }
    fake_adb.write()


def _log(fake_adb, serial):
    devices = {d["serial"]: d for d in fake_adb.read()["servers"]["host"]["devices"]}
    return [c for c in devices[serial].get("log", []) if c[:2] == ["shell", "input"] or c[:2] == ["shell", "monkey"]]


def _tool(cls, agent, **args):
    return cls(agent=agent, name="t", method=None, args=args, message="", loop_data=None)


def test_find_target_rescales_and_prefers_matching_text():
    target = wf.target_for(parse_ui_xml(PHONE_A), 540, 840)
    assert target["text"] == "Display" and target["resource_id"] == "android:id/title"
    found = wf.find_target(parse_ui_xml(PHONE_B), target)
    assert found["text"] == "Display"
    assert wf.point_in(found, target["offset"]) == (360, 550)


def test_find_target_rejects_missing_element():
    target = wf.target_for(parse_ui_xml(PHONE_A), 540, 840)
    assert wf.find_target(parse_ui_xml(screen(node("Other", rid="x:id/y", bounds="[0,0][100,100]"))), target) is None


def test_record_with_agent_tools_then_replay_on_other_phone(fake_adb, agent_factory):
    from usr.plugins.droidclaw.tools.adb_tools import AdbTools
    from usr.plugins.droidclaw.tools.droidclaw_workflow import DroidClawWorkflow

    _setup(fake_adb)
    agent = agent_factory()
    started = asyncio.run(_tool(DroidClawWorkflow, agent, action="record_start", name="Open display", device="PHONE_A").execute())
    assert "Recording" in started.message, started.message
    for args in (
        {"action": "launch", "package": "com.android.settings"},
        {"action": "tap", "coordinates": "540,840"},
        {"action": "type", "text": "dark mode & more"},
        {"action": "back"},
        {"action": "shell", "command": "dumpsys battery"},  # diagnostics are not recorded
    ):
        result = asyncio.run(_tool(AdbTools, agent, device="PHONE_A", **args).execute())
        assert not result.message.startswith("Error"), result.message
    stopped = asyncio.run(_tool(DroidClawWorkflow, agent, action="record_stop", device="PHONE_A").execute())
    assert "4 steps" in stopped.message, stopped.message

    saved = wf.load_workflow("Open display")
    assert [s["action"] for s in saved["steps"]] == ["launch", "tap", "type", "key"]
    assert saved["steps"][1]["target"]["text"] == "Display"
    assert saved["steps"][2]["params"]["text"] == "dark mode & more"
    assert saved["device"]["size"] == [1080, 2400]

    fake_adb.reset_calls()
    played = asyncio.run(_tool(DroidClawWorkflow, agent, action="play", name="Open display", device="PHONE_B", speed=4).execute())
    assert "completed" in played.message, played.message
    log = _log(fake_adb, "PHONE_B")
    assert ["shell", "input", "tap", "360", "550"] in log, log
    assert any(c[:3] == ["shell", "input", "text"] for c in log)
    assert ["shell", "input", "keyevent", "KEYCODE_BACK"] in log


def test_playback_stops_at_missing_element(fake_adb, monkeypatch):
    _setup(fake_adb)
    workflow = {
        "name": "broken",
        "device": {"size": [1080, 2400]},
        "steps": [
            {"action": "tap", "params": {"x": 1, "y": 1}, "target": {"resource_id": "x:id/missing", "text": "Nope"}, "delay_ms": 300},
            {"action": "key", "params": {"key": "home"}},
        ],
    }
    report = asyncio.run(wf.play_workflow(workflow, "PHONE_A", target_timeout=0.3))
    assert report["status"] == "failed" and len(report["results"]) == 1
    report = asyncio.run(wf.play_workflow(workflow, "PHONE_A", target_timeout=0.3, continue_on_error=True))
    assert report["status"] == "completed_with_errors" and len(report["results"]) == 2


def test_coordinate_only_steps_scale_to_screen(fake_adb):
    _setup(fake_adb)
    workflow = {"name": "c", "device": {"size": [1080, 2400]}, "steps": [{"action": "tap", "params": {"x": 540, "y": 1200}}]}
    asyncio.run(wf.play_workflow(workflow, "PHONE_B"))
    assert ["shell", "input", "tap", "360", "800"] in _log(fake_adb, "PHONE_B")


def test_execution_loop_run_is_recorded_without_noop_steps(fake_adb):
    from usr.plugins.droidclaw.helpers import execution_loop

    _setup(fake_adb)
    wf.start_recording("from-run", "PHONE_A")
    screens = [PHONE_A, screen(node("Dark theme", rid="x:id/t", bounds="[0,0][500,100]"))]

    class Phone:
        def run(self, args, device, timeout):
            if args[:2] == ["shell", "input"] and len(screens) > 1:
                screens.pop(0)
            return ""

    class LLM:
        replies = [
            '{"action": "wait", "seconds": 0.2}',  # changes nothing -> dropped
            '{"action": "tap", "element": 2}',
            '{"action": "done"}',
        ]

        async def chat(self, messages, temperature=0.3):
            return self.replies.pop(0)

    phone = Phone()
    loop = execution_loop.ExecutionLoop(
        device="PHONE_A", llm_client=LLM(), step_delay=0,
        fn_run_adb=phone.run, fn_dump_ui=lambda d: parse_ui_xml(screens[0]),
        on_action=lambda a, raw, ex: wf.record_canonical("PHONE_A", a, raw, ex),
        on_no_change=lambda n: wf.drop_last_steps("PHONE_A", n),
    )
    result = asyncio.run(loop.run("open display"))
    assert result["status"] == "completed"
    stopped = wf.stop_recording("PHONE_A")
    steps = wf.load_workflow("from-run")["steps"]
    assert [s["action"] for s in steps] == ["tap"], steps
    assert steps[0]["target"]["text"] == "Display"


def test_panel_commands_are_recorded(fake_adb):
    from usr.plugins.droidclaw.api.adb_command import AdbCommand

    _setup(fake_adb)
    wf.start_recording("panel", "PHONE_A")
    handler = AdbCommand()
    result = asyncio.run(handler.process({"command": "shell input keyevent KEYCODE_HOME", "device": "PHONE_A"}, None))
    assert result["success"]
    asyncio.run(handler.process({"command": "shell input tap 540 680", "device": "PHONE_A"}, None))
    steps = wf.recording_for("PHONE_A").steps
    assert steps[0] == {**steps[0], "action": "key", "params": {"key": "KEYCODE_HOME"}}
    assert steps[1]["target"]["text"] == "Network & internet"


def test_api_background_playback(fake_adb):
    from usr.plugins.droidclaw.api.workflows import Workflows

    _setup(fake_adb)
    wf.save_workflow({"name": "home", "device": {"size": [1080, 2400]}, "steps": [{"action": "key", "params": {"key": "home"}}]})
    api = Workflows()

    async def main():
        listing = await api.process({"action": "list"}, None)
        assert listing["workflows"][0]["name"] == "home"
        started = await api.process({"action": "play", "name": "home", "device": "PHONE_A"}, None)
        assert started["success"], started
        for _ in range(50):
            status = await api.process({"action": "run_status", "run_id": started["run_id"]}, None)
            if status["run"]["status"] != "running":
                return status["run"]
            await asyncio.sleep(0.1)

    run = asyncio.run(main())
    assert run["status"] == "completed", run


def test_text_unescape_round_trip():
    from usr.plugins.droidclaw.helpers.vision import escape_input_text

    text = "it's 5$ & (more); ok?"
    escaped = escape_input_text(text)

    async def main():
        wf._recordings["D"] = wf.Recording(name="x", device="D")
        step = await wf.prepare_adb_args("D", ["shell", "input", "text", escaped])
        return step

    assert asyncio.run(main())["params"]["text"] == text
