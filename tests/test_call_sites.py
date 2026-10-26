import asyncio
import base64

from usr.plugins.droidclaw.helpers import ui_parser

XML = (
    '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
    '<node index="0" text="Settings" resource-id="android:id/title" class="android.widget.TextView" '
    'content-desc="" clickable="true" enabled="true" bounds="[0,100][1080,200]" />'
    "</hierarchy>"
)


def test_dump_ui_fast_path(fake_adb):
    fake_adb.add_device("S1")
    fake_adb.state["shell_outputs"] = {"uiautomator dump /dev/tty": XML + "UI hierchary dumped to: /dev/tty\n"}
    fake_adb.write()
    elements = ui_parser.dump_ui("S1")
    assert any(e["text"] == "Settings" and e["center"] == [540, 150] for e in elements)
    assert not fake_adb.calls("pull")


def test_dump_ui_falls_back_to_file(fake_adb):
    fake_adb.add_device("S1")
    fake_adb.state["files"] = {"*": {"*droidclaw_dump_*": XML}}
    fake_adb.write()
    elements = ui_parser.dump_ui("S1")
    assert any(e["text"] == "Settings" for e in elements)


def test_tool_screenshot_uses_runner(fake_adb, tmp_path, monkeypatch):
    from usr.plugins.droidclaw.tools import adb_screen

    fake_adb.add_device("S1")
    fake_adb.state["screencap_b64"] = base64.b64encode(b"\x89PNG....").decode()
    fake_adb.write()
    from usr.plugins.droidclaw.helpers import device_context

    monkeypatch.setattr(device_context, "SCREENSHOT_DIR", str(tmp_path))
    tool = adb_screen.AdbScreen(None, "adb_screen", None, {"mode": "screenshot", "device": "S1"}, "", None)
    message = asyncio.run(tool.execute()).message
    assert "Screenshot saved" in message, message


def test_system_prompt_extension_is_async_and_cached(fake_adb):
    from usr.plugins.droidclaw.extensions.python.system_prompt import _50_droidclaw_context as ext

    fake_adb.add_device("S1", model="Pixel_8")
    fake_adb.state["shell_outputs"] = {"getprop ro.product.model": "Pixel 8\n", "getprop": "14\n"}
    fake_adb.write()
    prompt = []
    asyncio.run(ext.DroidClawContext(agent=None).execute(system_prompt=prompt))
    assert "S1" in prompt[0] and "Pixel 8" in prompt[0]
    fake_adb.reset_calls()
    prompt = []
    asyncio.run(ext.DroidClawContext(agent=None).execute(system_prompt=prompt))
    getprops = [c for c in fake_adb.calls("shell") if "getprop" in " ".join(c["args"])]
    assert not getprops, "device properties should be cached"
