import asyncio
import base64
import io
import json

import pytest
from PIL import Image

from usr.plugins.droidclaw.helpers import execution_loop, vision
from usr.plugins.droidclaw.helpers.action_validator import validate_action
from usr.plugins.droidclaw.helpers.ui_parser import parse_ui_xml


from xml.sax.saxutils import quoteattr


def node(text="", desc="", rid="", cls="android.widget.TextView", bounds="[0,0][10,10]", clickable=False, **flags):
    attrs = " ".join(f'{k.replace("_", "-")}="{str(v).lower()}"' for k, v in flags.items())
    return (
        f'<node text={quoteattr(text)} content-desc={quoteattr(desc)} resource-id="{rid}" class="{cls}" '
        f'clickable="{str(clickable).lower()}" enabled="true" bounds="{bounds}" {attrs} />'
    )


def hierarchy(*nodes, size="[0,0][1080,2400]"):
    root = f'<node text="" content-desc="" resource-id="" class="android.widget.FrameLayout" clickable="false" enabled="true" bounds="{size}">'
    return '<?xml version="1.0"?><hierarchy rotation="0">' + root + "".join(nodes) + "</node></hierarchy>"


SETTINGS = hierarchy(
    node("Network & internet", rid="android:id/title", bounds="[0,600][1080,760]", clickable=True),
    node("Connected devices", rid="android:id/title", bounds="[0,760][1080,920]", clickable=True),
    node("", desc="Search settings", cls="android.widget.ImageButton", bounds="[900,100][1060,260]", clickable=True),
    node("Settings", bounds="[40,120][400,240]"),
    node("Wi-Fi", cls="android.widget.Switch", bounds="[0,920][1080,1080]", clickable=True, checkable=True, checked=True),
)


# --- parsing -----------------------------------------------------------------

@pytest.mark.parametrize(
    "reply",
    [
        '{"action": "tap", "element": 3}',
        'I can see the Settings app. {"action": "tap", "element": 3, "reason": "open it"}',
        '<think>The user wants {"action": "back"} maybe... no.</think>\n{"action": "tap", "element": 3}',
        'Thinking...</think>{"action": "tap", "element": 3}',
        '```json\n{"action": "tap", "element": 3}\n```',
        'Option A {"action": "swipe", "direction": "up"} is wrong. Final: {"action": "tap", "element": 3}',
        '{"action": "tap", "element": 3, "reason": "label says {braces}"}',
    ],
)
def test_parser_extracts_final_action(reply):
    action = validate_action(execution_loop.parse_llm_response(reply))
    assert action["action"] == "tap" and action["element"] == 3


def test_parser_never_raises_on_garbage():
    for reply in ["", None, "no json here", "{broken", "}{", "[1,2]"]:
        result = execution_loop.parse_llm_response(reply)
        assert isinstance(result, dict)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"action": "click", "index": "[5]"}, {"action": "tap", "element": 5}),
        ({"action": "tap", "coordinates": "540, 1200"}, {"action": "tap", "x": 540, "y": 1200}),
        ({"action": "tap", "x": "10.6", "y": 20}, {"action": "tap", "x": 11, "y": 20}),
        ({"action": "scroll_down"}, {"action": "scroll", "direction": "down"}),
        ({"action": "back"}, {"action": "press", "key": "back"}),
        ({"action": "type", "value": "hi", "enter": "true"}, {"action": "type", "text": "hi", "submit": True}),
        ({"action": "open_app", "app": "com.x"}, {"action": "launch", "package": "com.x"}),
        ({"action": "wait", "seconds": "99"}, {"action": "wait", "seconds": 10.0}),
        ({"action": "task_complete", "summary": "ok"}, {"action": "done", "summary": "ok"}),
    ],
)
def test_validator_normalizes_aliases(raw, expected):
    result = validate_action(raw)
    for key, value in expected.items():
        assert result[key] == value, result


@pytest.mark.parametrize("raw", [{"action": "tap"}, {"action": "teleport"}, {"action": "swipe", "direction": "sideways"}, "x"])
def test_validator_reports_invalid(raw):
    result = validate_action(raw)
    assert result["action"] == "invalid" and result["error"]


# --- element selection & annotation -------------------------------------------

def test_select_elements_reading_order_and_filtering():
    raw = parse_ui_xml(SETTINGS)
    selected = vision.select_elements(raw, 40, (1080, 2400))
    labels = [e.label for e in selected]
    assert labels[0] in ("Search settings", "Settings")
    assert labels.index("Network & internet") < labels.index("Connected devices") < labels.index("Wi-Fi")
    assert all(e.number == i for i, e in enumerate(selected, 1))
    wifi = next(e for e in selected if e.label == "Wi-Fi")
    assert wifi.kind == "toggle" and "checked" in wifi.flags
    assert "FrameLayout" not in vision.format_elements(selected)


def _png(size=(1080, 2400)):
    out = io.BytesIO()
    Image.new("RGB", size, (250, 250, 250)).save(out, format="PNG")
    return out.getvalue()


def test_annotate_downscales_and_draws_boxes():
    raw = parse_ui_xml(SETTINGS)
    selected = vision.select_elements(raw, 40, (1080, 2400))
    jpeg, size = vision.annotate(_png(), selected, max_edge=1000)
    assert size == (1080, 2400)
    img = Image.open(io.BytesIO(jpeg))
    assert img.format == "JPEG" and max(img.size) == 1000
    scale = 1000 / 2400
    wifi = next(e for e in selected if e.label == "Wi-Fi")
    # A pixel on the box's left edge is coloured, not the white background.
    px = img.getpixel((int((wifi.rect[0] + 1) * scale), int(((wifi.rect[1] + wifi.rect[3]) // 2) * scale)))
    assert px != (250, 250, 250)


def test_needs_vision_heuristics():
    rich = vision.select_elements(parse_ui_xml(SETTINGS), 40, (1080, 2400))
    assert vision.needs_vision(rich, 6) == ""
    assert vision.needs_vision([], 0)
    icons = hierarchy(*[node(cls="android.widget.ImageView", bounds=f"[{i*100},0][{i*100+90},90]", clickable=True) for i in range(6)])
    assert vision.needs_vision(vision.select_elements(parse_ui_xml(icons), 40, (1080, 2400)), 7) == "many unlabelled icons"


# --- end-to-end loop with a fake phone + fake LLM -----------------------------

class FakeLLM:
    def __init__(self, replies, fail_vision=False):
        self.replies = list(replies)
        self.calls = []
        self.fail_vision = fail_vision

    async def chat(self, messages, temperature=0.3):
        self.calls.append({"messages": messages, "image": None})
        return self.replies.pop(0)

    async def chat_with_vision(self, messages, image_b64, temperature=0.3, mime="image/png"):
        if self.fail_vision:
            raise RuntimeError("model does not support images")
        self.calls.append({"messages": messages, "image": image_b64, "mime": mime})
        return self.replies.pop(0)


class FakePhone:
    def __init__(self, screens):
        self.screens = list(screens)
        self.commands = []

    def run_adb(self, args, device, timeout):
        self.commands.append(args)
        if args[:2] == ["shell", "input"] and len(self.screens) > 1:
            self.screens.pop(0)
        return ""

    def dump(self, device):
        return parse_ui_xml(self.screens[0])

    def screenshot(self, device):
        return _png()


def make_loop(phone, llm, **kw):
    return execution_loop.ExecutionLoop(
        device="PHONE",
        llm_client=llm,
        step_delay=0,
        fn_run_adb=phone.run_adb,
        fn_dump_ui=phone.dump,
        fn_wake_device=lambda d: True,
        fn_screenshot=phone.screenshot,
        **kw,
    )


def test_tap_by_element_uses_device_center_and_completes():
    after = hierarchy(node("Wi-Fi preferences", bounds="[0,300][1080,400]"))
    phone = FakePhone([SETTINGS, after])
    selected = vision.select_elements(parse_ui_xml(SETTINGS), 40, (1080, 2400))
    number = next(e.number for e in selected if e.label == "Network & internet")
    llm = FakeLLM([json.dumps({"action": "tap", "element": number}), '{"action": "done", "summary": "opened"}'])
    result = asyncio.run(make_loop(phone, llm, vision_mode="fallback").run("open network settings"))
    assert result["status"] == "completed"
    assert ["shell", "input", "tap", "540", "680"] in phone.commands
    second_prompt = llm.calls[1]["messages"][1]["content"]
    assert "-> screen changed" in second_prompt
    assert len(llm.calls[0]["messages"]) == 2, "one system + one user message per step"


def test_fallback_sends_annotated_image_only_when_needed():
    sparse = hierarchy(node(cls="android.webkit.WebView", bounds="[0,0][1080,2400]", clickable=True))
    phone = FakePhone([SETTINGS])
    llm = FakeLLM(['{"action": "done"}'])
    asyncio.run(make_loop(phone, llm, vision_mode="fallback").run("x"))
    assert llm.calls[0]["image"] is None

    phone = FakePhone([sparse])
    llm = FakeLLM(['{"action": "done"}'])
    asyncio.run(make_loop(phone, llm, vision_mode="fallback").run("x"))
    assert llm.calls[0]["image"] and llm.calls[0]["mime"] == "image/jpeg"
    assert "attached because" in llm.calls[0]["messages"][1]["content"]
    img = Image.open(io.BytesIO(base64.b64decode(llm.calls[0]["image"])))
    assert max(img.size) <= 1280


def test_vision_off_never_sends_images():
    phone = FakePhone([hierarchy()])
    llm = FakeLLM(['{"action": "done"}'])
    asyncio.run(make_loop(phone, llm, vision_mode="off").run("x"))
    assert llm.calls[0]["image"] is None


def test_look_forces_image_next_step():
    phone = FakePhone([SETTINGS])
    llm = FakeLLM(['{"action": "look"}', '{"action": "done"}'])
    asyncio.run(make_loop(phone, llm, vision_mode="fallback").run("x"))
    assert llm.calls[0]["image"] is None and llm.calls[1]["image"]


def test_vision_failure_falls_back_to_text():
    phone = FakePhone([SETTINGS])
    llm = FakeLLM(['{"action": "done"}'], fail_vision=True)
    result = asyncio.run(make_loop(phone, llm, vision_mode="always").run("x"))
    assert result["status"] == "completed"
    assert any("vision turned off" in n for n in result["notes"])


def test_stuck_recovery_escalates_and_warns_model():
    phone = FakePhone([SETTINGS])  # screen never changes
    replies = ['{"action": "tap", "element": 1}'] * 12 + ['{"action": "done"}']
    llm = FakeLLM(replies)
    result = asyncio.run(make_loop(phone, llm, vision_mode="off", stuck_threshold=2, max_steps=12).run("x"))
    recoveries = [a["reason"] for a in result["actions"] if a["action"] == "stuck_recovery"]
    assert recoveries[:3] == ["pressed back", "scrolled down", "went to the home screen"]
    assert any("WARNING" in c["messages"][1]["content"] for c in llm.calls)


def test_invalid_reply_is_reported_back():
    phone = FakePhone([SETTINGS])
    llm = FakeLLM(['{"action": "tap"}', '{"action": "done"}'])
    result = asyncio.run(make_loop(phone, llm).run("x"))
    assert result["status"] == "completed"
    assert "invalid reply" in llm.calls[1]["messages"][1]["content"]


def test_unknown_element_is_an_error_not_a_tap_at_origin():
    phone = FakePhone([SETTINGS])
    llm = FakeLLM(['{"action": "tap", "element": 99}', '{"action": "done"}'])
    result = asyncio.run(make_loop(phone, llm).run("x"))
    assert not any(c[:3] == ["shell", "input", "tap"] for c in phone.commands)
    assert "error(tap" in result["actions"][0]["description"]


def test_scroll_uses_screen_size_and_content_direction():
    wide = hierarchy(node("A", bounds="[0,0][720,200]"), size="[0,0][720,1600]")
    phone = FakePhone([wide])
    llm = FakeLLM(['{"action": "scroll", "direction": "down"}', '{"action": "done"}'])
    asyncio.run(make_loop(phone, llm).run("x"))
    swipe = next(c for c in phone.commands if c[:3] == ["shell", "input", "swipe"])
    x1, y1, x2, y2 = (int(v) for v in swipe[3:7])
    assert x1 == x2 == 360 and y1 > y2, "scrolling down moves the finger up"


def test_merge_config_keeps_vision_on_when_unset():
    from usr.plugins.droidclaw.tools import droidclaw_run

    merged = droidclaw_run._merge_config({}, {})
    assert droidclaw_run._bool(merged["model_supports_vision"], True) is True
    assert merged["vision_mode"] == "fallback"


def test_agent_tools_tap_by_element_number(fake_adb, agent_factory):
    from usr.plugins.droidclaw.tools.adb_screen import AdbScreen
    from usr.plugins.droidclaw.tools.adb_tools import AdbTools

    fake_adb.add_device("PHONE")
    fake_adb.state["shell_outputs"] = {"uiautomator dump /dev/tty": SETTINGS}
    fake_adb.write()
    agent = agent_factory()
    listing = asyncio.run(AdbScreen(agent, "adb_screen", None, {"mode": "xml"}, "", None).execute())
    line = next(l for l in listing.message.splitlines() if "Connected devices" in l)
    number = int(line.split("]")[0].strip("["))
    result = asyncio.run(AdbTools(agent, "adb_tools", None, {"action": "tap", "element": number}, "", None).execute())
    assert "Tapped (540, 840)" in result.message, result.message
