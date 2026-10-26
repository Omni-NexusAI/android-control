"""A scriptable stand-in for the ``adb`` client used by the test-suite.

State lives in a JSON file named by ``FAKE_ADB_STATE`` so tests can shape the
world (devices, mDNS services, failures) and inspect the calls that were made.

Two servers are modelled: ``host`` (selected by ``-H``) and ``container``.
"""

import base64
import fcntl
import fnmatch
import json
import os
import sys
import time


def _load(fh):
    fh.seek(0)
    return json.loads(fh.read() or "{}")


def _save(fh, state):
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(state))
    fh.flush()


def main(argv):
    path = os.environ["FAKE_ADB_STATE"]
    with open(path, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        state = _load(fh)
        rc, out, err, binary = run(state, argv)
        hold = state.pop("_hold", 0)
        _save(fh, state)
    delay = state.get("delay", 0) + hold
    if delay:
        time.sleep(delay)
    if binary is not None:
        sys.stdout.buffer.write(binary)
    else:
        sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


def _file(state, dev, remote):
    for scope in (dev["serial"], "*"):
        for pattern, value in state.get("files", {}).get(scope, {}).items():
            if fnmatch.fnmatch(remote, pattern):
                return value
    return None


def run(state, argv):
    args = list(argv)
    server = "container"
    serial = None
    while args and args[0] in ("-H", "-P", "-s"):
        flag, value = args[0], args[1]
        args = args[2:]
        if flag == "-H":
            server = "host"
        elif flag == "-s":
            serial = value
    state.setdefault("calls", []).append({"server": server, "serial": serial, "args": args})
    srv = state["servers"].setdefault(server, {"up": True, "devices": [], "mdns": [], "connectable": {}})

    if not srv.get("up", True):
        return 1, "", "* cannot connect to daemon at tcp:5037: Connection refused\n", None

    cmd = args[0] if args else ""
    if cmd == "mdns":
        if args[1] == "check":
            return 0, "mdns daemon version [Openscreen discovery 0.0.0]\n", "", None
        lines = ["List of discovered mdns services"]
        for svc in srv.get("mdns", []):
            lines.append(f"{svc['name']}\t{svc['type']}\t{svc['address']}")
        return 0, "\n".join(lines) + "\n", "", None
    if cmd == "devices":
        lines = ["List of devices attached"]
        for dev in srv["devices"]:
            lines.append(f"{dev['serial']}\t{dev['state']} product:{dev.get('product','p')} model:{dev.get('model','m')} device:d transport_id:1")
        return 0, "\n".join(lines) + "\n", "", None
    if cmd == "connect":
        endpoint = args[1]
        target = srv.get("connectable", {}).get(endpoint)
        if target is None:
            return 1, f"failed to connect to {endpoint}\n", "", None
        srv["devices"] = [d for d in srv["devices"] if d["serial"] != endpoint]
        srv["devices"].append({"serial": endpoint, "state": "device", "model": target.get("model", "m"), "product": target.get("product", "p")})
        return 0, f"connected to {endpoint}\n", "", None
    if cmd == "disconnect":
        if len(args) > 1:
            srv["devices"] = [d for d in srv["devices"] if d["serial"] != args[1]]
        else:
            srv["devices"] = [d for d in srv["devices"] if ":" not in d["serial"]]
        return 0, "disconnected\n", "", None
    if cmd == "reconnect":
        for dev in srv["devices"]:
            if dev["state"] == "offline" and dev.get("recovers_on_reconnect"):
                dev["state"] = "device"
        return 0, "reconnecting\n", "", None
    if cmd in ("start-server", "version"):
        return 0, "", "", None

    # Device-scoped commands.
    online = [d for d in srv["devices"] if d["state"] == "device"]
    if serial is None:
        if len(online) > 1:
            return 1, "", "adb: more than one device/emulator\n", None
        if not online:
            return 1, "", "adb: no devices/emulators found\n", None
        dev = online[0]
    else:
        matches = [d for d in srv["devices"] if d["serial"] == serial]
        if not matches:
            return 1, "", f"adb: device '{serial}' not found\n", None
        dev = matches[0]
        if dev["state"] == "offline":
            return 1, "", "adb: device offline\n", None

    fail = state.get("fail_once", {}).pop(dev["serial"], None)
    if fail:
        return 1, "", fail, None

    dev.setdefault("log", []).append(args)
    if cmd == "exec-out" and args[1:3] == ["screencap", "-p"]:
        return 0, "", "", base64.b64decode(state.get("screencap_b64", ""))
    if cmd == "exec-out" and args[1:2] == ["cat"]:
        content = _file(state, dev, args[2])
        if content is None:
            return 1, "", f"cat: {args[2]}: No such file or directory\n", None
        return 0, content, "", None
    if cmd == "shell" and any("app_process" in a for a in args):
        # A long-running device-side server (scrcpy): stay alive a while.
        state["_hold"] = state.get("app_process_seconds", 4)
        return 0, "[server] INFO: Device: fake\n", "", None
    if cmd in ("shell", "exec-out"):
        text = " ".join(args[1:])
        outputs = state.get("shell_outputs", {})
        for prefix, value in outputs.items():
            if text.startswith(prefix):
                if isinstance(value, dict):
                    value = value.get(dev["serial"], value.get("*", ""))
                return 0, value, "", None
        if text.startswith("echo "):
            return 0, text[5:] + "\n", "", None
        return 0, "", "", None
    if cmd == "pull":
        remote, local = args[1], args[2]
        content = _file(state, dev, remote)
        if content is None:
            return 1, "", f"adb: error: remote object '{remote}' does not exist\n", None
        with open(local, "w", encoding="utf-8") as out:
            out.write(content)
        return 0, f"{remote}: 1 file pulled\n", "", None
    if cmd == "push":
        return 0, "1 file pushed\n", "", None
    return 0, "", "", None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
