"""Test harness: stub the Agent Zero runtime and provide a scriptable fake adb.

The plugin is imported as ``usr.plugins.droidclaw`` exactly as inside A0, with
minimal stand-ins for the A0 ``helpers`` modules it touches.
"""

import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


# --- A0 stand-ins ---------------------------------------------------------

PLUGIN_CONFIG: dict = {}


def get_plugin_config(name, agent=None, **kwargs):
    agent_cfg = getattr(agent, "_plugin_config", None) if agent is not None else None
    if agent_cfg is not None:
        return dict(agent_cfg)
    return dict(PLUGIN_CONFIG)


class Response:
    def __init__(self, message="", break_loop=False, additional=None, **kwargs):
        self.message = message
        self.break_loop = break_loop
        self.additional = additional


class Tool:
    def __init__(self, agent, name, method, args, message, loop_data, **kwargs):
        self.agent = agent
        self.name = name
        self.method = method
        self.args = args
        self.message = message
        self.loop_data = loop_data

    async def set_progress(self, content):
        self.progress = content

    def add_progress(self, content):
        pass


class Extension:
    def __init__(self, agent=None, **kwargs):
        self.agent = agent


class ApiHandler:
    def __init__(self, app=None, thread_lock=None):
        self.app = app
        self.thread_lock = thread_lock


class FlaskLikeResponse:
    def __init__(self, response=None, status=200, mimetype=None, headers=None, **kwargs):
        self.response = response
        self.status = status
        self.mimetype = mimetype
        self.headers = dict(headers or {})


class PrintStyle:
    @staticmethod
    def warning(*a, **k):
        pass

    @staticmethod
    def error(*a, **k):
        pass


helpers_pkg = _module("helpers")
helpers_pkg.__path__ = []
helpers_pkg.plugins = _module("helpers.plugins", get_plugin_config=get_plugin_config)
helpers_pkg.tool = _module("helpers.tool", Tool=Tool, Response=Response)
helpers_pkg.extension = _module("helpers.extension", Extension=Extension)
helpers_pkg.api = _module("helpers.api", ApiHandler=ApiHandler, Request=object, Response=FlaskLikeResponse)
helpers_pkg.print_style = _module("helpers.print_style", PrintStyle=PrintStyle)

usr = _module("usr")
usr.__path__ = []
usr_plugins = _module("usr.plugins")
usr_plugins.__path__ = []
droidclaw = _module("usr.plugins.droidclaw")
droidclaw.__path__ = [str(ROOT)]


class FakeAgent:
    def __init__(self, name="A0", config=None):
        self.agent_name = name
        self.data = {}
        self._plugin_config = config
        self.context = types.SimpleNamespace(id=f"ctx-{name}", log=None)

    def get_data(self, key):
        return self.data.get(key)

    def set_data(self, key, value):
        self.data[key] = value


# --- fake adb ---------------------------------------------------------------

class FakeAdb:
    def __init__(self, tmp_path: Path):
        self.state_path = tmp_path / "adb_state.json"
        self.exe = tmp_path / "adb"
        self.exe.write_text(f"#!{sys.executable}\nimport runpy, sys\nsys.argv[0] = {str(TESTS / 'fake_adb.py')!r}\nrunpy.run_path({str(TESTS / 'fake_adb.py')!r}, run_name='__main__')\n")
        self.exe.chmod(self.exe.stat().st_mode | stat.S_IXUSR)
        self.state = {
            "servers": {
                "host": {"up": True, "devices": [], "mdns": [], "connectable": {}},
                "container": {"up": True, "devices": [], "mdns": [], "connectable": {}},
            },
            "calls": [],
        }
        self.write()

    def write(self):
        self.state_path.write_text(json.dumps(self.state))

    def read(self) -> dict:
        self.state = json.loads(self.state_path.read_text())
        return self.state

    def server(self, name="host") -> dict:
        return self.state["servers"][name]

    def add_device(self, serial, state="device", server="host", **extra):
        self.server(server)["devices"].append({"serial": serial, "state": state, **extra})
        self.write()

    def calls(self, cmd=None) -> list:
        calls = self.read()["calls"]
        if cmd:
            return [c for c in calls if c["args"] and c["args"][0] == cmd]
        return calls

    def reset_calls(self):
        self.read()
        self.state["calls"] = []
        self.write()


@pytest.fixture
def fake_adb(tmp_path, monkeypatch):
    adb = FakeAdb(tmp_path)
    monkeypatch.setenv("FAKE_ADB_STATE", str(adb.state_path))
    PLUGIN_CONFIG.clear()
    PLUGIN_CONFIG.update(
        {
            "adb_path": str(adb.exe),
            "adb_backend": "auto_host_first",
            "adb_host": "host.docker.internal",
            "adb_port": 5037,
            "adb_keepalive": False,
        }
    )

    from usr.plugins.droidclaw.helpers import adb_backend, adb_session

    monkeypatch.setattr(adb_session, "_STATE_FILE", tmp_path / "known_devices.json")
    monkeypatch.setattr(adb_session, "_records", None)
    monkeypatch.setattr(adb_session, "_recent_recovery", {})
    monkeypatch.setattr(adb_backend, "_ts_refreshed_at", float("inf"))
    adb_backend.invalidate_config_cache()
    adb_backend.invalidate_backend()
    adb_backend.invalidate_device_cache()
    yield adb
    adb_backend.invalidate_backend()
    adb_backend.invalidate_device_cache()


@pytest.fixture
def agent_factory():
    return FakeAgent
