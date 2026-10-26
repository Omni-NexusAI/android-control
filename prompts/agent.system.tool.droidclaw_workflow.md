### droidclaw_workflow

record what you do on a phone once, then replay it any time without an llm
"device": serial of the phone; leave empty to use the phone selected with adb_device select
actions:
- record_start: start capturing every input sent to the phone (adb_tools taps/swipes/typing/keys/launches, droidclaw_run steps, panel input); args name, description
- record_stop: save the recording; record_cancel discards it
- play: replay a saved workflow; args name, optional continue_on_error (default false: stop at the first failed step), speed (0.25-4)
- list, show (name), delete (name), status
replay re-finds each tapped element by id/text/description, waits up to 8s for it to appear, and scales coordinates to the phone's screen size
quicker alternative: droidclaw_run with "record_as": "<name>" saves the run as a workflow if it completes
usage:

~~~json
{
    "thoughts": ["The user wants this routine repeatable without an LLM", "Replay the saved workflow"],
    "headline": "Replaying Android workflow",
    "tool_name": "droidclaw_workflow",
    "tool_args": {"action": "play", "name": "enable-dark-mode", "device": ""}
}
~~~

record while working:
~~~json
{
    "thoughts": ["Record the steps so they can be replayed later"],
    "headline": "Recording Android workflow",
    "tool_name": "droidclaw_workflow",
    "tool_args": {"action": "record_start", "name": "enable-dark-mode", "description": "Settings > Display > Dark theme on", "device": ""}
}
~~~
