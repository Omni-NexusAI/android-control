### adb_device

manage adb device connections, choose which phone you control, and query device state
actions: status list select release connect pair disconnect foreground_app wake sleep
status/list show every connected phone, which one you are bound to, and which phones other agents are using

device targeting (all android control tools):
- "device" argument wins; otherwise the phone you bound with select; otherwise the configured default; otherwise the only connected phone
- if several phones are connected and none of those apply the tool returns an error listing serials: pick one, never guess
- a phone being driven by another agent is locked to that agent; the error names the holder. use another phone or wait
- release frees your claim when you are done with a phone

bind this agent to one phone (do this first when you were told which phone to use):
~~~json
{
    "thoughts": ["I was asked to work on phone 192.168.1.23:5555", "Bind it so every call targets it"],
    "headline": "Selecting Android device",
    "tool_name": "adb_device",
    "tool_args": {"action": "select", "device": "192.168.1.23:5555"}
}
~~~

list devices:
~~~json
{
    "thoughts": ["Need to check which devices are connected"],
    "headline": "Checking adb device status",
    "tool_name": "adb_device",
    "tool_args": {"action": "status"}
}
~~~

connect:
~~~json
{
    "thoughts": ["Need to connect to a wireless adb device"],
    "headline": "Connecting to device via adb",
    "tool_name": "adb_device",
    "tool_args": {"action": "connect", "ip_port": "192.168.1.10:39877"}
}
~~~

pair:
~~~json
{
    "thoughts": ["Need to pair with a device using a code"],
    "headline": "Pairing with android device",
    "tool_name": "adb_device",
    "tool_args": {"action": "pair", "ip_port": "192.168.1.10:37425", "code": "123456"}
}
~~~

disconnect needs ip_port; disconnecting every phone requires "all": true and interrupts other agents

foreground app:
~~~json
{
    "thoughts": ["Need to know which app is in the foreground"],
    "headline": "Getting foreground app from device",
    "tool_name": "adb_device",
    "tool_args": {"action": "foreground_app", "device": ""}
}
~~~

several phones at once:
- independent tasks on different phones can run concurrently: use the parallel tool with one call per phone
- either wrap droidclaw_run calls directly, each with its own "device"
- or wrap call_subordinate calls; name the serial in each message and tell the subordinate to start with adb_device select on it
- never let two workers share one phone
