"""Parse uiautomator XML dump to structured data."""

import logging
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET

from usr.plugins.droidclaw.helpers.adb_backend import run_adb_result
from usr.plugins.droidclaw.helpers.device_context import remote_temp_path

logger = logging.getLogger('droidclaw')


def parse_ui_xml(xml_string: str) -> list[dict]:
    """Parse UI automator XML string to a list of element dicts.

    Args:
        xml_string: Raw XML string from uiautomator dump.

    Returns:
        List of element dicts with parsed bounds and center coordinates.
    """
    elements = []
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as e:
        logger.error("Failed to parse UI XML: %s", e)
        return elements

    for node in root.iter('node'):
        bounds_str = node.get('bounds', '')
        center = _parse_bounds(bounds_str)

        element = {
            'text': node.get('text', ''),
            'content_desc': node.get('content-desc', ''),
            'bounds': bounds_str,
            'rect': parse_rect(bounds_str),
            'clickable': _flag(node, 'clickable'),
            'long_clickable': _flag(node, 'long-clickable'),
            'scrollable': _flag(node, 'scrollable'),
            'checkable': _flag(node, 'checkable'),
            'checked': _flag(node, 'checked'),
            'focusable': _flag(node, 'focusable'),
            'focused': _flag(node, 'focused'),
            'selected': _flag(node, 'selected'),
            'password': _flag(node, 'password'),
            'enabled': node.get('enabled', 'true').lower() == 'true',
            'class': node.get('class', ''),
            'package': node.get('package', ''),
            'resource_id': node.get('resource-id', ''),
            'hint': node.get('hint', ''),
            'center': center,
        }
        elements.append(element)

    return elements


def _flag(node, name: str) -> bool:
    return (node.get(name, 'false') or 'false').lower() == 'true'


def parse_rect(bounds_str: str) -> list[int]:
    """Parse ``[x1,y1][x2,y2]`` to ``[x1, y1, x2, y2]`` ([0,0,0,0] on failure)."""
    match = re.match(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', bounds_str or '')
    if not match:
        return [0, 0, 0, 0]
    return [int(v) for v in match.groups()]


def _parse_bounds(bounds_str: str) -> list[int]:
    """Parse bounds string like [x1,y1][x2,y2] to center coordinates.

    Args:
        bounds_str: Bounds string in format [x1,y1][x2,y2].

    Returns:
        List of [center_x, center_y] or [0, 0] if parsing fails.
    """
    if not bounds_str:
        return [0, 0]

    match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
    if not match:
        return [0, 0]

    x1, y1, x2, y2 = int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))
    return [(x1 + x2) // 2, (y1 + y2) // 2]


def dump_ui(device: str = None) -> list[dict]:
    """Get UI elements from device via uiautomator dump.

    Dumps the UI hierarchy on the device, streams it back over ``exec-out``
    when possible (one round-trip, no temp files) and parses it.

    Args:
        device: Optional ADB device serial. If None, uses default device.

    Returns:
        List of element dicts parsed from the UI dump.
    """
    xml_content = dump_ui_xml(device)
    if not xml_content:
        return []
    elements = parse_ui_xml(xml_content)
    logger.debug("Parsed %d UI elements", len(elements))
    return elements


def dump_ui_xml(device: str = None) -> str:
    """Return the raw uiautomator XML for ``device`` ('' on failure)."""
    target = device if device is not None else ""
    remote_path = remote_temp_path('dump', 'xml')

    # Fast path: dump straight to stdout (Android 7+), no pull round-trip.
    result = run_adb_result(
        ['exec-out', 'uiautomator', 'dump', '/dev/tty'], device=target, timeout=30
    )
    xml_content = _extract_xml(result.get('stdout') or '') if result['returncode'] == 0 else ''
    if xml_content:
        return xml_content

    for flags in ([], ['--compressed']):
        result = run_adb_result(
            ['shell', 'uiautomator', 'dump', *flags, remote_path], device=target, timeout=30
        )
        if result['returncode'] == 0:
            break
        logger.warning("uiautomator dump failed %s: %s", flags or '', result['output'])
    else:
        return ''

    result = run_adb_result(['exec-out', 'cat', remote_path], device=target, timeout=15)
    xml_content = _extract_xml(result.get('stdout') or '') if result['returncode'] == 0 else ''
    if not xml_content:
        tmp_dir = tempfile.mkdtemp(prefix='droidclaw_')
        local_path = os.path.join(tmp_dir, 'window_dump.xml')
        try:
            pulled = run_adb_result(['pull', remote_path, local_path], device=target, timeout=15)
            if pulled['returncode'] == 0:
                with open(local_path, 'r', encoding='utf-8') as f:
                    xml_content = f.read()
            else:
                logger.error("adb pull failed: %s", pulled['output'])
        except (OSError, IOError) as e:
            logger.error("Failed to read dump file: %s", e)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    run_adb_result(['shell', 'rm', '-f', remote_path], device=target, timeout=10, recover=False)
    return xml_content


def _extract_xml(output: str) -> str:
    """Pull the <hierarchy> document out of mixed uiautomator output."""
    start = output.find('<?xml')
    if start < 0:
        start = output.find('<hierarchy')
    end = output.rfind('</hierarchy>')
    if start < 0 or end < 0:
        return ''
    return output[start:end + len('</hierarchy>')]


def find_element(
    elements: list,
    text: str = None,
    desc: str = None,
    resource_id: str = None,
    clickable: bool = None,
) -> dict:
    """Find an element matching the given criteria.

    All non-None parameters must match. String matches are case-insensitive
    and use substring matching.

    Args:
        elements: List of element dicts to search.
        text: Substring to match against element text (case-insensitive).
        desc: Substring to match against content_desc (case-insensitive).
        resource_id: Substring to match against resource_id (case-insensitive).
        clickable: If set, must match element clickable flag.

    Returns:
        First matching element dict, or None if no match found.
    """
    for elem in elements:
        if text is not None:
            if text.lower() not in elem.get('text', '').lower():
                continue
        if desc is not None:
            if desc.lower() not in elem.get('content_desc', '').lower():
                continue
        if resource_id is not None:
            if resource_id.lower() not in elem.get('resource_id', '').lower():
                continue
        if clickable is not None:
            if elem.get('clickable') != clickable:
                continue
        return elem

    return None


def get_element_center(element: dict) -> tuple[int, int]:
    """Get center coordinates from an element dict.

    Args:
        element: Element dict with 'center' or 'bounds' field.

    Returns:
        Tuple of (x, y) center coordinates.
    """
    center = element.get('center', [0, 0])
    if center and len(center) == 2:
        return (int(center[0]), int(center[1]))

    # Fallback: parse from bounds string
    bounds = element.get('bounds', '')
    parsed = _parse_bounds(bounds)
    return (int(parsed[0]), int(parsed[1]))
