import asyncio

from test_vision import SETTINGS


def test_device_status_light_mode_skips_diagnostics(fake_adb, monkeypatch):
    from usr.plugins.droidclaw.api import device_status

    called = {"diag": 0, "caps": 0}
    monkeypatch.setattr(device_status, "adb_diagnostics", lambda: called.__setitem__("diag", called["diag"] + 1) or {})
    monkeypatch.setattr(device_status, "bridge_capabilities", lambda force=False: called.__setitem__("caps", called["caps"] + 1) or {})
    from usr.plugins.droidclaw.helpers.adb_backend import select_backend

    fake_adb.add_device("PHONE", model="Pixel_8")
    select_backend()  # warm the backend cache
    fake_adb.reset_calls()

    light = asyncio.run(device_status.DeviceStatus().process({"light": True}, None))
    assert light["light"] and light["devices"][0]["serial"] == "PHONE" and light["connected"]
    assert called == {"diag": 0, "caps": 0}
    assert not [c for c in fake_adb.calls("mdns") if c["args"][1] == "check"], "light mode must not re-probe servers"

    full = asyncio.run(device_status.DeviceStatus().process({}, None))
    assert "adb_diagnostics" in full and called == {"diag": 1, "caps": 1}


def test_screen_elements_api(fake_adb):
    from usr.plugins.droidclaw.api.screen_elements import ScreenElements

    fake_adb.add_device("PHONE")
    fake_adb.state["shell_outputs"] = {"uiautomator dump /dev/tty": SETTINGS}
    fake_adb.write()
    result = asyncio.run(ScreenElements().process({"device": "PHONE"}, None))
    assert result["success"] and result["width"] == 1080 and result["count"] == 5
    assert '"Wi-Fi"' in result["text"] and "toggle" in result["text"]
