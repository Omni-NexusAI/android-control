"""Normalize and validate LLM action outputs for Android Control.

Models phrase the same intent many ways ("click", "tap_element", "index",
"coordinates": "540,1200", "app" instead of "package"...). Everything is
mapped onto one canonical schema here so the executor only handles:

    tap / longpress   {"element": N} or {"x": int, "y": int}
    type              {"text": str, "element"?: N, "clear"?: bool, "submit"?: bool}
    swipe             {"direction": up|down|left|right, "element"?: N}   finger direction
    scroll            {"direction": up|down|left|right, "element"?: N}   content direction
    press             {"key": home|back|enter|recent|...}
    launch            {"package": str}
    wait              {"seconds": float}
    look              {}                       ask for an annotated screenshot
    done              {"summary": str}
    fail              {"reason": str}

Unusable output becomes {"action": "invalid", "error": ...} so the loop can
tell the model what was wrong instead of silently waiting.
"""

import logging
import re

logger = logging.getLogger('droidclaw')

CANONICAL_ACTIONS = {
    'tap', 'longpress', 'type', 'swipe', 'scroll', 'press', 'launch', 'wait', 'look', 'done', 'fail',
}

# alias -> (canonical action, extra fields)
_ALIASES = {
    'click': ('tap', {}),
    'tap_element': ('tap', {}),
    'press_element': ('tap', {}),
    'long_press': ('longpress', {}),
    'long_click': ('longpress', {}),
    'longpress_element': ('longpress', {}),
    'input': ('type', {}),
    'type_text': ('type', {}),
    'enter_text': ('type', {}),
    'write': ('type', {}),
    'scroll_up': ('scroll', {'direction': 'up'}),
    'scroll_down': ('scroll', {'direction': 'down'}),
    'scroll_left': ('scroll', {'direction': 'left'}),
    'scroll_right': ('scroll', {'direction': 'right'}),
    'swipe_up': ('swipe', {'direction': 'up'}),
    'swipe_down': ('swipe', {'direction': 'down'}),
    'swipe_left': ('swipe', {'direction': 'left'}),
    'swipe_right': ('swipe', {'direction': 'right'}),
    'home': ('press', {'key': 'home'}),
    'back': ('press', {'key': 'back'}),
    'enter': ('press', {'key': 'enter'}),
    'key': ('press', {}),
    'keyevent': ('press', {}),
    'open_recent_apps': ('press', {'key': 'recent'}),
    'recents': ('press', {'key': 'recent'}),
    'open_notifications': ('press', {'key': 'notifications'}),
    'open_quick_settings': ('press', {'key': 'quick_settings'}),
    'open_app': ('launch', {}),
    'start_app': ('launch', {}),
    'open': ('launch', {}),
    'sleep': ('wait', {}),
    'screenshot': ('look', {}),
    'observe': ('look', {}),
    'finish': ('done', {}),
    'finished': ('done', {}),
    'complete': ('done', {}),
    'task_complete': ('done', {}),
    'success': ('done', {}),
    'give_up': ('fail', {}),
    'abort': ('fail', {}),
    'impossible': ('fail', {}),
}

VALID_DIRECTIONS = {'up', 'down', 'left', 'right'}

# Kept for callers that introspect the accepted names.
VALID_ACTIONS = CANONICAL_ACTIONS | set(_ALIASES)


def _int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _element(action: dict):
    for key in ('element', 'index', 'element_id', 'element_index', 'mark', 'target', 'number', 'id'):
        if key in action:
            value = action[key]
            if isinstance(value, str):
                match = re.fullmatch(r'\s*\[?#?(\d+)\]?\s*', value)
                if not match:
                    continue
                value = match.group(1)
            number = _int(value)
            if number is not None:
                return number
    return None


def _point(action: dict):
    if 'x' in action and 'y' in action:
        x, y = _int(action['x']), _int(action['y'])
        if x is not None and y is not None:
            return x, y
    coords = action.get('coordinates', action.get('coords', action.get('point')))
    if isinstance(coords, str):
        parts = re.findall(r'-?\d+(?:\.\d+)?', coords)
        coords = parts[:2]
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        x, y = _int(coords[0]), _int(coords[1])
        if x is not None and y is not None:
            return x, y
    return None


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def validate_action(action: dict) -> dict:
    """Return a canonical action dict (see module docstring)."""
    if not isinstance(action, dict):
        return {'action': 'invalid', 'error': 'response was not a JSON object'}

    raw_name = str(action.get('action') or action.get('type') or action.get('name') or '').strip().lower()
    raw_name = raw_name.replace('-', '_').replace(' ', '_')
    name, extra = _ALIASES.get(raw_name, (raw_name, {}))
    if name not in CANONICAL_ACTIONS:
        return {
            'action': 'invalid',
            'error': f"unknown action '{raw_name or '(missing)'}'; use one of: {', '.join(sorted(CANONICAL_ACTIONS))}",
        }

    result = {'action': name, 'reason': str(action.get('reason') or action.get('thought') or '')[:300]}
    merged = {**action, **extra}

    element = _element(merged)
    point = _point(merged)

    if name in ('tap', 'longpress'):
        if element is not None:
            result['element'] = element
        elif point is not None:
            result['x'], result['y'] = point
        else:
            return {'action': 'invalid', 'error': f'{name} needs "element": <number> (or "x" and "y")'}

    elif name == 'type':
        text = merged.get('text', merged.get('value', merged.get('content')))
        if text is None or str(text) == '':
            return {'action': 'invalid', 'error': 'type needs "text"'}
        result['text'] = str(text)
        if element is not None:
            result['element'] = element
        result['clear'] = _truthy(merged.get('clear', False))
        result['submit'] = _truthy(merged.get('submit', merged.get('enter', False)))

    elif name in ('swipe', 'scroll'):
        direction = str(merged.get('direction', '')).strip().lower()
        if direction not in VALID_DIRECTIONS:
            return {'action': 'invalid', 'error': f'{name} needs "direction": up, down, left or right'}
        result['direction'] = direction
        if element is not None:
            result['element'] = element

    elif name == 'press':
        key = merged.get('key', merged.get('keycode', merged.get('button', '')))
        key = str(key).strip()
        if not key:
            return {'action': 'invalid', 'error': 'press needs "key" (home, back, enter, recent, ...)'}
        result['key'] = key

    elif name == 'launch':
        package = str(merged.get('package', merged.get('app', merged.get('package_name', '')))).strip()
        if not package:
            return {'action': 'invalid', 'error': 'launch needs "package" (e.g. com.android.settings)'}
        result['package'] = package

    elif name == 'wait':
        seconds = merged.get('seconds', merged.get('duration', merged.get('time', 1)))
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = 1.0
        result['seconds'] = min(max(seconds, 0.2), 10.0)

    elif name == 'done':
        result['summary'] = str(merged.get('summary') or merged.get('result') or merged.get('reason') or '')

    elif name == 'fail':
        result['reason'] = str(merged.get('reason') or merged.get('summary') or 'Task cannot be completed')

    return result
