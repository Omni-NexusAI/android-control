"""Validate LLM action outputs for droidclaw."""

import logging

logger = logging.getLogger('droidclaw')

VALID_ACTIONS = {
    'tap',
    'longpress',
    'swipe',
    'type',
    'press',
    'home',
    'back',
    'enter',
    'wait',
    'scroll_up',
    'scroll_down',
    'scroll_left',
    'scroll_right',
    'launch',
    'screenshot',
    'shell',
    'screen_dump',
    'swipe_up',
    'swipe_down',
    'swipe_left',
    'swipe_right',
    'tap_element',
    'longpress_element',
    'scroll_to_text',
    'scroll_to_id',
    'clear_text',
    'select_all',
    'copy',
    'paste',
    'cut',
    'open_notifications',
    'open_quick_settings',
    'open_recent_apps',
    'power_dialog',
    'done',
}

# Valid directions for swipe action
VALID_DIRECTIONS = {'up', 'down', 'left', 'right'}

# Actions that require coordinates
COORD_ACTIONS = {'tap', 'longpress', 'tap_element', 'longpress_element'}

# Actions that require a text/string parameter
TEXT_ACTIONS = {'type', 'scroll_to_text', 'shell'}

# Actions that require a resource_id
ID_ACTIONS = {'scroll_to_id'}

# Actions that require a direction
DIRECTION_ACTIONS = {'swipe'}

# Actions that require a package name
PACKAGE_ACTIONS = {'launch'}


def _sanitize_coordinates(action: dict) -> dict:
    """Ensure coordinates are exactly 2 integers.

    Parses x/y from the action dict, converting to int and clamping
    to exactly two values.

    Args:
        action: Action dict that may contain 'x', 'y' or 'coordinates' keys.

    Returns:
        Action dict with sanitized 'x' and 'y' as integers.
    """
    if 'coordinates' in action:
        coords = action.pop('coordinates')
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                action['x'] = int(coords[0])
                action['y'] = int(coords[1])
            except (ValueError, TypeError):
                action['x'] = 0
                action['y'] = 0
        else:
            action['x'] = 0
            action['y'] = 0
    elif 'x' in action and 'y' in action:
        try:
            action['x'] = int(action['x'])
            action['y'] = int(action['y'])
        except (ValueError, TypeError):
            action['x'] = 0
            action['y'] = 0
    return action


def _validate_direction(action: dict) -> dict:
    """Validate direction field for swipe actions.

    Args:
        action: Action dict with a 'direction' key.

    Returns:
        Action dict with validated direction, or 'up' as default.
    """
    direction = action.get('direction', 'up')
    if direction not in VALID_DIRECTIONS:
        logger.warning("Invalid direction '%s', defaulting to 'up'", direction)
        action['direction'] = 'up'
    return action


def validate_action(action: dict) -> dict:
    """Validate and sanitize an action dict from LLM output.

    Checks that the action name is valid, sanitizes coordinates,
    validates directions, and ensures required fields are present.
    Falls back to a wait action for unrecognized action names.

    Args:
        action: Action dict with at minimum an 'action' key.

    Returns:
        Validated and sanitized action dict.
    """
    if not isinstance(action, dict):
        logger.warning("Action is not a dict, returning wait fallback")
        return {'action': 'wait', 'reason': 'Invalid action format'}

    action_name = action.get('action', '')

    # Validate action name
    if not action_name or action_name not in VALID_ACTIONS:
        logger.warning("Invalid action name: '%s'", action_name)
        return {'action': 'wait', 'reason': 'Invalid action'}

    result = dict(action)

    # Sanitize coordinates for tap/longpress actions
    if action_name in COORD_ACTIONS:
        result = _sanitize_coordinates(result)

    # Validate direction for swipe
    if action_name in DIRECTION_ACTIONS:
        result = _validate_direction(result)

    # Check required text field
    if action_name in TEXT_ACTIONS:
        if 'text' not in result and 'value' not in result:
            logger.warning("Action '%s' missing text/value field", action_name)
            result['text'] = ''

    # Check required resource_id
    if action_name in ID_ACTIONS:
        if 'resource_id' not in result and 'id' not in result:
            logger.warning("Action '%s' missing resource_id field", action_name)
            result['resource_id'] = ''

    # Check required package for launch
    if action_name in PACKAGE_ACTIONS:
        if 'package' not in result and 'app' not in result:
            logger.warning("Action '%s' missing package field", action_name)
            result['package'] = ''

    # Ensure duration/timeout defaults for wait
    if action_name == 'wait':
        if 'duration' not in result and 'timeout' not in result:
            result['duration'] = 1.0

    return result
