### adb_tools

direct android device control via adb for single atomic actions
actions: tap longpress swipe type press launch home back enter shell screenshot screen_dump
use for individual precise interactions when you know exactly what to do
for complex multi-step goals use droidclaw_run instead
usage:

~~~json
{
    "thoughts": [
        "Need to tap a specific coordinate on the android device",
        "I know the exact position from a previous screen dump"
    ],
    "headline": "Tapping on android device screen",
    "tool_name": "adb_tools",
    "tool_args": {
        "action": "tap",
        "coordinates": "540,264",
        "device": ""
    }
}
~~~

other actions:

swipe direction:
~~~json
{
    "thoughts": ["Need to scroll down"],
    "headline": "Scrolling down on device",
    "tool_name": "adb_tools",
    "tool_args": {"action": "swipe", "direction": "up", "device": ""}
}
~~~

type text:
~~~json
{
    "thoughts": ["Need to type text into a field"],
    "headline": "Typing text on device",
    "tool_name": "adb_tools",
    "tool_args": {"action": "type", "text": "hello world", "device": ""}
}
~~~

key press:
~~~json
{
    "thoughts": ["Need to press a key"],
    "headline": "Pressing keycode on device",
    "tool_name": "adb_tools",
    "tool_args": {"action": "press", "keycode": "KEYCODE_BACK", "device": ""}
}
~~~

launch app:
~~~json
{
    "thoughts": ["Need to launch an app"],
    "headline": "Launching app on device",
    "tool_name": "adb_tools",
    "tool_args": {"action": "launch", "package": "com.android.settings", "device": ""}
}
~~~

shell command:
~~~json
{
    "thoughts": ["Need to run an adb shell command"],
    "headline": "Running shell command on device",
    "tool_name": "adb_tools",
    "tool_args": {"action": "shell", "command": "dumpsys battery", "device": ""}
}
~~~

screenshot:
~~~json
{
    "thoughts": ["Need to capture current screen"],
    "headline": "Taking screenshot of device",
    "tool_name": "adb_tools",
    "tool_args": {"action": "screenshot", "device": ""}
}
~~~

screen dump:
~~~json
{
    "thoughts": ["Need to read ui elements on screen"],
    "headline": "Dumping ui hierarchy from device",
    "tool_name": "adb_tools",
    "tool_args": {"action": "screen_dump", "device": ""}
}
~~~
