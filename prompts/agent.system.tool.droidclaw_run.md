### droidclaw_run

autonomous android device control agent that executes a goal using llm-guided navigation
provide a natural language goal and it will tap swipe type and navigate the device to accomplish it
uses adb for device interaction and an llm for decision making
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
        "max_steps": 30,
        "intervene": false
    }
}
~~~
