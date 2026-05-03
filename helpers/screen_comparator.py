"""Compare two UI dumps to detect changes."""

import logging

logger = logging.getLogger('droidclaw')


def elements_match(e1: dict, e2: dict) -> bool:
    """Check if two elements match by bounds, text, and resource_id.

    Two elements are considered matching if they share the same bounds
    string, text content, and resource_id.

    Args:
        e1: First element dict.
        e2: Second element dict.

    Returns:
        True if elements match on bounds, text, and resource_id.
    """
    return (
        e1.get('bounds', '') == e2.get('bounds', '')
        and e1.get('text', '') == e2.get('text', '')
        and e1.get('resource_id', '') == e2.get('resource_id', '')
    )


def compare_dumps(prev: list[dict], curr: list[dict]) -> dict:
    """Compare two UI dumps and detect changes.

    Identifies elements that were added, removed, or changed between
    the previous and current UI state dumps.

    Args:
        prev: Previous UI dump as list of element dicts.
        curr: Current UI dump as list of element dicts.

    Returns:
        Dict with keys:
            changed: bool - Whether any differences were detected.
            added: list - Elements present in curr but not in prev.
            removed: list - Elements present in prev but not in curr.
            changed_elements: list - Dicts with 'before' and 'after' for
                elements that exist in both but have different properties.
    """
    added = []
    removed = []
    changed_elements = []

    # Build lookup for matching
    prev_matched = set()
    curr_matched = set()

    # Find matching pairs and identify changes
    for i, c_elem in enumerate(curr):
        for j, p_elem in enumerate(prev):
            if j in prev_matched:
                continue
            if elements_match(c_elem, p_elem):
                prev_matched.add(j)
                curr_matched.add(i)
                # Check if properties changed
                if c_elem != p_elem:
                    changed_elements.append({
                        'before': p_elem,
                        'after': c_elem,
                    })
                break

    # Elements in curr not matched are added
    for i, c_elem in enumerate(curr):
        if i not in curr_matched:
            added.append(c_elem)

    # Elements in prev not matched are removed
    for j, p_elem in enumerate(prev):
        if j not in prev_matched:
            removed.append(p_elem)

    has_changes = bool(added or removed or changed_elements)

    logger.debug(
        "Screen comparison: added=%d, removed=%d, changed=%d",
        len(added), len(removed), len(changed_elements),
    )

    return {
        'changed': has_changes,
        'added': added,
        'removed': removed,
        'changed_elements': changed_elements,
    }
