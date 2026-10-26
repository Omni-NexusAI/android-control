import asyncio
import time

import pytest

from usr.plugins.droidclaw.helpers import adb_backend, adb_session


def test_backend_selection_is_sticky(fake_adb):
    fake_adb.add_device("SERIAL1")
    for _ in range(5):
        result = adb_backend.run_adb_result(["shell", "echo", "hi"], device="SERIAL1")
        assert result["returncode"] == 0
        assert result["stdout"] == "hi"
    probes = [c for c in fake_adb.calls("mdns") if c["args"][1] == "check"]
    assert len(probes) == 1, "backend should be probed once, then cached"
    devices_calls = fake_adb.calls("devices")
    assert len(devices_calls) <= 1, "device list should be served from cache"


def test_slow_host_probe_does_not_flip_to_container(fake_adb, monkeypatch):
    fake_adb.add_device("SERIAL1")
    calls = {"n": 0}
    real_probe = adb_backend._probe

    def flaky_probe(prefix, timeout=4):
        if "-H" in prefix:
            calls["n"] += 1
            if calls["n"] == 1:
                return False, "adb mdns check timed out"
        return real_probe(prefix, timeout)

    monkeypatch.setattr(adb_backend, "_probe", flaky_probe)
    assert adb_backend.select_backend().name == "host"


def test_exact_serial_skips_canonical_merge(fake_adb):
    fake_adb.add_device("SERIAL1")
    adb_backend.run_adb_result(["shell", "echo", "1"], device="SERIAL1")
    fake_adb.reset_calls()
    adb_backend.run_adb_result(["shell", "echo", "1"], device="SERIAL1")
    names = [c["args"][0] for c in fake_adb.calls()]
    assert names == ["shell"], names


def test_offline_transport_is_recovered_and_command_retried(fake_adb):
    fake_adb.add_device("192.168.1.20:5555", state="offline", recovers_on_reconnect=True)
    result = adb_backend.run_adb_result(["shell", "echo", "ok"], device="192.168.1.20:5555")
    assert result["returncode"] == 0, result
    assert result["stdout"] == "ok"
    assert result["recovered"] is True
    assert fake_adb.calls("reconnect")


def test_wireless_port_change_reconnects_via_mdns(fake_adb):
    # The phone was connected on an old port, Wireless debugging restarted on a
    # new port: the old transport is gone, mDNS advertises the new endpoint.
    adb_session.remember_endpoint("192.168.1.30:40001")
    srv = fake_adb.server("host")
    srv["mdns"] = [{"name": "adb-ABC-xyz", "type": "_adb-tls-connect._tcp", "address": "192.168.1.30:40777"}]
    srv["connectable"] = {"192.168.1.30:40777": {"model": "Pixel"}}
    fake_adb.write()

    result = adb_backend.run_adb_result(["shell", "echo", "back"], device="192.168.1.30:40001")
    assert result["returncode"] == 0, result
    assert result["stdout"] == "back"
    assert result["resolved_device"] == "192.168.1.30:40777"
    connects = [c["args"][1] for c in fake_adb.calls("connect")]
    assert connects[0] == "192.168.1.30:40777", "live mDNS port must be tried first"


def test_transient_error_closed_is_retried_once(fake_adb):
    fake_adb.add_device("SERIAL1")
    fake_adb.state["fail_once"] = {"SERIAL1": "error: closed\n"}
    fake_adb.write()
    result = adb_backend.run_adb_result(["shell", "echo", "x"], device="SERIAL1")
    assert result["returncode"] == 0
    assert result["recovered"] is True


def test_real_command_failure_is_not_retried(fake_adb):
    fake_adb.add_device("SERIAL1")
    fake_adb.state["fail_once"] = {"SERIAL1": "/system/bin/sh: foo: not found\n"}
    fake_adb.write()
    result = adb_backend.run_adb_result(["shell", "foo"], device="SERIAL1")
    assert result["returncode"] != 0
    assert result["recovered"] is False
    assert len(fake_adb.calls("shell")) == 1


def test_unrecoverable_device_reports_clearly(fake_adb):
    result = adb_backend.run_adb_result(["shell", "echo", "x"], device="10.0.0.9:5555")
    assert result["returncode"] != 0
    assert "not connected" in result["output"]


def test_server_error_reselects_backend(fake_adb):
    fake_adb.add_device("SERIAL1", server="host")
    fake_adb.add_device("SERIAL1", server="container")
    assert adb_backend.select_backend().name == "host"
    fake_adb.server("host")["up"] = False
    fake_adb.write()
    result = adb_backend.run_adb_result(["shell", "echo", "y"], device="SERIAL1")
    assert result["returncode"] == 0, result
    assert result["backend"] == "container"


def test_async_runner_does_not_block_event_loop(fake_adb):
    fake_adb.add_device("SERIAL1")
    fake_adb.state["delay"] = 0.3
    fake_adb.write()

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(ticker())
        await adb_backend.run_adb_async(["shell", "echo", "z"], device="SERIAL1")
        task.cancel()
        return ticks

    assert asyncio.run(main()) >= 10


def test_async_timeout_kills_process(fake_adb):
    fake_adb.add_device("SERIAL1")
    adb_backend.run_adb_result(["shell", "echo", "warm"], device="SERIAL1")
    fake_adb.read()
    fake_adb.state["delay"] = 3
    fake_adb.write()
    start = time.monotonic()
    result = asyncio.run(adb_backend.run_adb_async(["shell", "echo", "slow"], device="SERIAL1", timeout=0.5))
    assert result["returncode"] == -1
    assert time.monotonic() - start < 2.5


def test_binary_output(fake_adb):
    import base64

    fake_adb.add_device("SERIAL1")
    fake_adb.state["screencap_b64"] = base64.b64encode(b"\x89PNGdata").decode()
    fake_adb.write()
    result = asyncio.run(adb_backend.run_adb_async(["exec-out", "screencap", "-p"], device="SERIAL1", binary=True))
    assert result["stdout"] == b"\x89PNGdata"


def test_mdns_named_transport_gets_ip_alias(fake_adb):
    srv = fake_adb.server("host")
    srv["devices"] = [{"serial": "adb-ABC-xyz._adb-tls-connect._tcp", "state": "device"}]
    srv["mdns"] = [{"name": "adb-ABC-xyz", "type": "_adb-tls-connect._tcp", "address": "192.168.1.40:41234"}]
    fake_adb.write()
    resolution = adb_backend.resolve_device("192.168.1.40")
    assert resolution["resolved"]
    assert resolution["resolved_device"] == "adb-ABC-xyz._adb-tls-connect._tcp"


def test_connect_is_remembered(fake_adb):
    fake_adb.server("host")["connectable"] = {"192.168.1.50:5555": {}}
    fake_adb.write()
    result = adb_backend.run_adb_result(["connect", "192.168.1.50:5555"], resolve=False)
    assert result["returncode"] == 0
    assert any(r["ip"] == "192.168.1.50" for r in adb_session.known_devices())
