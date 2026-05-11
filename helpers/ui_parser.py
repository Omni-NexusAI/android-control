"""Parse uiautomator XML dump to structured data."""

import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from usr.plugins.droidclaw.helpers.adb_backend import adb_cmd, resolve_device

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

    for node in root.iter():
        bounds_str = node.get('bounds', '')
        center = _parse_bounds(bounds_str)

        element = {
            'text': node.get('text', ''),
            'content_desc': node.get('content-desc', ''),
            'bounds': bounds_str,
            'clickable': node.get('clickable', 'false').lower() == 'true',
            'enabled': node.get('enabled', 'true').lower() == 'true',
            'class': node.get('class', ''),
            'resource_id': node.get('resource-id', ''),
            'center': center,
        }
        elements.append(element)

    return elements


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

    Dumps UI hierarchy to /sdcard/window_dump.xml on the device,
    pulls it locally, parses it, and cleans up.

    Args:
        device: Optional ADB device serial. If None, uses default device.

    Returns:
        List of element dicts parsed from the UI dump.
    """
    resolution = resolve_device(device if device is not None else "")
    if not resolution.get("resolved_device"):
        logger.error("ADB device not connected: %s", device or "auto")
        return []
    adb_prefix = adb_cmd([], device=resolution["resolved_device"])

    remote_path = '/sdcard/window_dump.xml'

    # Dump UI hierarchy on device
    dump_cmd = adb_prefix + ['shell', 'uiautomator', 'dump', remote_path]
    try:
        result = subprocess.run(dump_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning("uiautomator dump failed: %s", result.stderr)
            # Try with compressed flag
            dump_cmd = adb_prefix + ['shell', 'uiautomator', 'dump', '--compressed', remote_path]
            result = subprocess.run(dump_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.error("uiautomator dump failed (compressed): %s", result.stderr)
                return []
    except subprocess.TimeoutExpired:
        logger.error("uiautomator dump timed out")
        return []
    except FileNotFoundError:
        logger.error("ADB client is unavailable; check Android Control dependency diagnostics")
        return []

    # Pull the dump file to a temp location
    tmp_dir = tempfile.mkdtemp(prefix='droidclaw_')
    local_path = os.path.join(tmp_dir, 'window_dump.xml')

    pull_cmd = adb_prefix + ['pull', remote_path, local_path]
    try:
        result = subprocess.run(pull_cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            logger.error("adb pull failed: %s", result.stderr)
            _cleanup(tmp_dir, adb_prefix, remote_path)
            return []
    except subprocess.TimeoutExpired:
        logger.error("adb pull timed out")
        _cleanup(tmp_dir, adb_prefix, remote_path)
        return []

    # Read and parse the file
    try:
        with open(local_path, 'r', encoding='utf-8') as f:
            xml_content = f.read()
    except (OSError, IOError) as e:
        logger.error("Failed to read dump file: %s", e)
        _cleanup(tmp_dir, adb_prefix, remote_path)
        return []

    elements = parse_ui_xml(xml_content)

    # Cleanup
    _cleanup(tmp_dir, adb_prefix, remote_path)

    logger.debug("Parsed %d UI elements", len(elements))
    return elements


def _cleanup(tmp_dir: str, adb_prefix: list, remote_path: str):
    """Clean up temp files and remote dump file.

    Args:
        tmp_dir: Local temp directory to remove.
        adb_prefix: ADB command prefix for device commands.
        remote_path: Remote file path to delete from device.
    """
    # Remove local temp file
    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    # Remove remote file
    rm_cmd = adb_prefix + ['shell', 'rm', '-f', remote_path]
    try:
        subprocess.run(rm_cmd, capture_output=True, text=True, timeout=10)
    except Exception:
        pass


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
