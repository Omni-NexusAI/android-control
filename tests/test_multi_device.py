import asyncio

import pytest

from usr.plugins.droidclaw.helpers import device_context
from usr.plugins.droidclaw.helpers.device_context import DeviceSelectionError

XML = (
    '<?xml version="1.0"?><hierarchy rotation="0"><node text="{label}" resource-id="" class="t" '
    'content-desc="" clickable="true" enabled="true" bounds="[0,0][100,100]" /></hierarchy>'
)


def _tool(cls, agent, **args):
    return cls(agent=agent, name=cls.__name__, method=None, args=args, message="", loop_data=None)


@pytest.fixture(autouse=True)
def _clear_leases(monkeypatch):
    monkeypatch.setattr(device_context, "_leases", {})
    monkeypatch.setattr(device_context, "LEASE_WAIT", 0.2)


def test_single_device_is_used_without_arguments(fake_adb, agent_factory):
    fake_adb.add_device("PHONE_A")
    assert asyncio.run(device_context.target_device(agent_factory())) == "PHONE_A"


def test_multiple_devices_without_choice_is_an_error_listing_devices(fake_adb, agent_factory):
    fake_adb.add_device("PHONE_A", model="Pixel")
    fake_adb.add_device("PHONE_B", model="Galaxy")
    with pytest.raises(DeviceSelectionError) as exc:
        asyncio.run(device_context.target_device(agent_factory()))
    assert "PHONE_A" in str(exc.value) and "PHONE_B" in str(exc.value)


def test_explicit_then_bound_then_config(fake_adb, agent_factory):
    fake_adb.add_device("PHONE_A")
    fake_adb.add_device("PHONE_B")
    agent = agent_factory(config={"device": "PHONE_B"})
    assert asyncio.run(device_context.target_device(agent)) == "PHONE_B"
    device_context.bind_device(agent, "PHONE_A")
    assert asyncio.run(device_context.target_device(agent)) == "PHONE_A"
    assert asyncio.run(device_context.target_device(agent, "PHONE_B")) == "PHONE_B"


def test_lease_blocks_second_agent_until_released(fake_adb, agent_factory):
    fake_adb.add_device("PHONE_A")
    a, b = agent_factory("A1"), agent_factory("A2")
    asyncio.run(device_context.claim(a, "PHONE_A"))
    with pytest.raises(DeviceSelectionError) as exc:
        asyncio.run(device_context.claim(b, "PHONE_A"))
    assert "A1" in str(exc.value)
    # The panel (no agent) is never locked out.
    asyncio.run(device_context.claim(None, "PHONE_A"))
    device_context.release_device(a)
    assert asyncio.run(device_context.claim(b, "PHONE_A")) == "PHONE_A"


def test_two_agents_drive_two_phones_in_parallel(fake_adb, agent_factory):
    from usr.plugins.droidclaw.tools.adb_tools import AdbTools

    fake_adb.add_device("PHONE_A")
    fake_adb.add_device("PHONE_B")
    fake_adb.state["delay"] = 0.3
    fake_adb.write()
    a, b = agent_factory("A1"), agent_factory("A2")

    async def main():
        return await asyncio.gather(
            _tool(AdbTools, a, action="tap", coordinates="10,20", device="PHONE_A").execute(),
            _tool(AdbTools, b, action="tap", coordinates="30,40", device="PHONE_B").execute(),
        )

    ra, rb = asyncio.run(main())
    assert "Tapped (10, 20)" in ra.message, ra.message
    assert "Tapped (30, 40)" in rb.message, rb.message
    devices = {d["serial"]: d for d in fake_adb.read()["servers"]["host"]["devices"]}
    assert ["shell", "input", "tap", "10", "20"] in devices["PHONE_A"]["log"]
    assert ["shell", "input", "tap", "30", "40"] in devices["PHONE_B"]["log"]
    assert ["shell", "input", "tap", "10", "20"] not in devices["PHONE_B"]["log"]


def test_concurrent_screen_dumps_do_not_collide(fake_adb, agent_factory):
    from usr.plugins.droidclaw.tools.adb_screen import AdbScreen

    fake_adb.add_device("PHONE_A")
    fake_adb.add_device("PHONE_B")
    fake_adb.state["shell_outputs"] = {
        "uiautomator dump /dev/tty": {"PHONE_A": XML.format(label="Alpha"), "PHONE_B": XML.format(label="Bravo")}
    }
    fake_adb.write()
    a, b = agent_factory("A1"), agent_factory("A2")

    async def main():
        return await asyncio.gather(
            _tool(AdbScreen, a, mode="xml", device="PHONE_A").execute(),
            _tool(AdbScreen, b, mode="xml", device="PHONE_B").execute(),
        )

    ra, rb = asyncio.run(main())
    assert "Alpha" in ra.message and "Bravo" not in ra.message
    assert "Bravo" in rb.message and "Alpha" not in rb.message


def test_wake_with_two_phones_requires_choice(fake_adb, agent_factory):
    from usr.plugins.droidclaw.tools.adb_device import AdbDevice

    fake_adb.add_device("PHONE_A")
    fake_adb.add_device("PHONE_B")
    result = asyncio.run(_tool(AdbDevice, agent_factory(), action="wake").execute())
    assert "Multiple Android devices" in result.message


def test_select_binds_agent(fake_adb, agent_factory):
    from usr.plugins.droidclaw.tools.adb_device import AdbDevice
    from usr.plugins.droidclaw.tools.adb_tools import AdbTools

    fake_adb.add_device("PHONE_A")
    fake_adb.add_device("PHONE_B")
    agent = agent_factory()
    result = asyncio.run(_tool(AdbDevice, agent, action="select", device="PHONE_B").execute())
    assert "Selected PHONE_B" in result.message
    result = asyncio.run(_tool(AdbTools, agent, action="home").execute())
    devices = {d["serial"]: d for d in fake_adb.read()["servers"]["host"]["devices"]}
    assert ["shell", "input", "keyevent", "KEYCODE_HOME"] in devices["PHONE_B"]["log"]


def test_disconnect_all_requires_explicit_flag(fake_adb, agent_factory):
    from usr.plugins.droidclaw.tools.adb_device import AdbDevice

    fake_adb.add_device("10.0.0.2:5555")
    result = asyncio.run(_tool(AdbDevice, agent_factory(), action="disconnect").execute())
    assert result.message.startswith("Error")
    assert not fake_adb.calls("disconnect")


def test_screenshot_paths_are_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(device_context, "SCREENSHOT_DIR", str(tmp_path))
    paths = {device_context.screenshot_path("PHONE_A") for _ in range(20)}
    assert len(paths) == 20


def test_identical_models_stay_separate_but_usb_and_wifi_merge(fake_adb):
    from usr.plugins.droidclaw.helpers import adb_backend

    srv = fake_adb.server("host")
    srv["devices"] = [
        {"serial": "R5CT111", "state": "device", "model": "Pixel_8", "product": "shiba"},
        {"serial": "R5CT222", "state": "device", "model": "Pixel_8", "product": "shiba"},
        {"serial": "adb-R5CT111-AbCd._adb-tls-connect._tcp", "state": "device", "model": "Pixel_8", "product": "shiba"},
    ]
    srv["mdns"] = [{"name": "adb-R5CT111-AbCd", "type": "_adb-tls-connect._tcp", "address": "192.168.1.7:40001"}]
    fake_adb.write()
    devices = adb_backend.canonical_devices()
    assert len(devices) == 2
    merged = next(d for d in devices if "R5CT111" in d.aliases)
    assert "192.168.1.7" in merged.aliases
