### adb_screen

read what is on an android screen
"device": serial of the phone to act on; leave empty to use the phone selected with adb_device select (required when several phones are connected and none is selected)
modes:
- xml: numbered list of meaningful elements in reading order with kind, label, id, center (device pixels) and state; numbers can be used with adb_tools tap "element"
- annotated: the same list plus a screenshot where each element is boxed and labelled with its number; call vision_load on the returned path to see it
- screenshot: plain screenshot file
filter: only elements whose text, description or id contain this text
usage:

~~~json
{
    "thoughts": [
        "Need to see what is on the android screen",
        "Icons may be unlabelled so I want the annotated screenshot too"
    ],
    "headline": "Reading android screen state",
    "tool_name": "adb_screen",
    "tool_args": {
        "mode": "annotated",
        "device": "",
        "filter": "",
        "max_elements": 40
    }
}
~~~
