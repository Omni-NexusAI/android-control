### droidclaw_run

"device": serial of the phone to act on; leave empty to use the phone selected with adb_device select (required when several phones are connected and none is selected)
autonomous android device control agent that executes a goal using llm-guided navigation
provide a natural language goal and it will tap swipe type and navigate the device to accomplish it
uses adb for device interaction and an llm for decision making; it reads numbered screen elements and, depending on the vision setting, an annotated screenshot
the result lists every action with whether it changed the screen
this is the primary tool for complex multi-step android tasks
for single adb actions use adb_tools instead
usage:

~~~json
{
    "thoughts": [
        "Need to automate an android task",
        "The task requires multiple steps",
        "Android Control can handle this autonomously"
    ],
    "headline": "Running Android Control to accomplish task on android device",
    "tool_name": "droidclaw_run",
    "tool_args": {
        "goal": "Open Settings and enable Developer Mode",
        "device": "",
        "max_steps": 30
    }
}
~~~

several phones at once: wrap one droidclaw_run per phone in the parallel tool, each with its own "device"; runs on different phones proceed concurrently and a phone already being driven by another agent is refused
