### adb_screen

read android screen state via adb uiautomator xml dump and or screenshot capture
returns parsed ui element hierarchy with text bounds clickable state and resource ids
use to understand what is currently displayed on the device before taking actions
use mode xml for element data screenshot for image capture or both for both
usage:

~~~json
{
    "thoughts": [
        "Need to see what is on the android screen",
        "Will parse ui elements to find interactive items"
    ],
    "headline": "Reading android screen state",
    "tool_name": "adb_screen",
    "tool_args": {
        "mode": "xml",
        "device": "",
        "filter": "",
        "max_elements": 40
    }
}
~~~
