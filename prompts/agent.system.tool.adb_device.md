### adb_device

manage adb device connections and query device state
actions: status connect pair disconnect foreground_app wake sleep
use to check connected devices connect or pair wirelessly and get device info
usage:

~~~json
{
    "thoughts": ["Need to check which devices are connected via adb"],
    "headline": "Checking adb device status",
    "tool_name": "adb_device",
    "tool_args": {"action": "status", "device": ""}
}
~~~

connect:
~~~json
{
    "thoughts": ["Need to connect to a wireless adb device"],
    "headline": "Connecting to device via adb",
    "tool_name": "adb_device",
    "tool_args": {"action": "connect", "ip_port": "192.168.1.10:39877", "device": ""}
}
~~~

pair:
~~~json
{
    "thoughts": ["Need to pair with a device using a code"],
    "headline": "Pairing with android device",
    "tool_name": "adb_device",
    "tool_args": {"action": "pair", "ip_port": "192.168.1.10:37425", "code": "123456", "device": ""}
}
~~~

foreground app:
~~~json
{
    "thoughts": ["Need to know which app is in the foreground"],
    "headline": "Getting foreground app from device",
    "tool_name": "adb_device",
    "tool_args": {"action": "foreground_app", "device": ""}
}
~~~
