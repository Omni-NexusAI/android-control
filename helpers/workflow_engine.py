"""Pre-recorded workflow execution engine for droidclaw."""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from usr.plugins.droidclaw.helpers.adb_backend import adb_cmd, resolve_device

logger = logging.getLogger('droidclaw')


@dataclass
class WorkflowStep:
    """A single step in a recorded workflow.

    Attributes:
        action: The droidclaw action name (e.g. 'tap', 'swipe', 'type').
        params: Parameters for the action (e.g. {'x': 540, 'y': 960}).
        wait_after: Seconds to wait after executing this step.
        condition: Optional condition dict to check before executing.
    """
    action: str
    params: dict = field(default_factory=dict)
    wait_after: float = 0.5
    condition: dict = field(default_factory=dict)


class Workflow:
    """A named sequence of workflow steps.

    Attributes:
        name: Human-readable workflow name.
        steps: List of WorkflowStep instances.
        description: Optional description of what the workflow does.
    """

    def __init__(self, name: str, steps: list[WorkflowStep], description: str = ""):
        """Initialize a Workflow.

        Args:
            name: Workflow name (used as filename, keep filesystem-safe).
            steps: List of WorkflowStep instances.
            description: Optional description of the workflow.
        """
        self.name = name
        self.steps = steps
        self.description = description

    def to_dict(self) -> dict:
        """Serialize workflow to a dict for JSON storage.

        Returns:
            Dict representation of the workflow.
        """
        return {
            'name': self.name,
            'description': self.description,
            'steps': [
                {
                    'action': step.action,
                    'params': step.params,
                    'wait_after': step.wait_after,
                    'condition': step.condition,
                }
                for step in self.steps
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Workflow':
        """Deserialize a workflow from a dict.

        Args:
            data: Dict with 'name', 'steps', and optional 'description'.

        Returns:
            Workflow instance.
        """
        name = data.get('name', 'unnamed')
        description = data.get('description', '')
        steps = []
        for step_data in data.get('steps', []):
            steps.append(WorkflowStep(
                action=step_data.get('action', 'wait'),
                params=step_data.get('params', {}),
                wait_after=step_data.get('wait_after', 0.5),
                condition=step_data.get('condition', {}),
            ))
        return cls(name=name, steps=steps, description=description)


def _build_adb_command(action: str, params: dict, device: str = None) -> list[str]:
    """Build an ADB shell command from an action and params.

    Args:
        action: Action name.
        params: Action parameters.
        device: Optional device serial.

    Returns:
        ADB command as a list of strings.
    """
    resolution = resolve_device(device if device is not None else "")
    adb_prefix = adb_cmd([], device=resolution.get("resolved_device") or device)

    if action == 'tap':
        x, y = params.get('x', 0), params.get('y', 0)
        return adb_prefix + ['shell', 'input', 'tap', str(x), str(y)]
    elif action == 'longpress':
        x, y = params.get('x', 0), params.get('y', 0)
        duration = params.get('duration', 1000)
        return adb_prefix + ['shell', 'input', 'swipe', str(x), str(y), str(x), str(y), str(duration)]
    elif action == 'swipe':
        x1 = params.get('x1', params.get('start_x', 500))
        y1 = params.get('y1', params.get('start_y', 1000))
        x2 = params.get('x2', params.get('end_x', 500))
        y2 = params.get('y2', params.get('end_y', 300))
        duration = params.get('duration', 300)
        return adb_prefix + ['shell', 'input', 'swipe', str(x1), str(y1), str(x2), str(y2), str(duration)]
    elif action == 'type':
        text = params.get('text', '')
        # Escape special characters for shell
        escaped = text.replace(' ', '%s').replace('&', '\\&').replace('<', '\\<').replace('>', '\\>')
        return adb_prefix + ['shell', 'input', 'text', escaped]
    elif action == 'press':
        keycode = params.get('keycode', params.get('key', 'KEYCODE_ENTER'))
        return adb_prefix + ['shell', 'input', 'keyevent', str(keycode)]
    elif action == 'home':
        return adb_prefix + ['shell', 'input', 'keyevent', 'KEYCODE_HOME']
    elif action == 'back':
        return adb_prefix + ['shell', 'input', 'keyevent', 'KEYCODE_BACK']
    elif action == 'enter':
        return adb_prefix + ['shell', 'input', 'keyevent', 'KEYCODE_ENTER']
    elif action == 'launch':
        package = params.get('package', params.get('app', ''))
        activity = params.get('activity', '')
        if activity:
            return adb_prefix + ['shell', 'am', 'start', '-n', f'{package}/{activity}']
        return adb_prefix + ['shell', 'monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER', '1']
    elif action == 'shell':
        cmd = params.get('command', params.get('cmd', 'echo'))
        return adb_prefix + ['shell'] + cmd.split()
    elif action == 'swipe_up':
        return adb_prefix + ['shell', 'input', 'swipe', '540', '1500', '540', '300', '300']
    elif action == 'swipe_down':
        return adb_prefix + ['shell', 'input', 'swipe', '540', '300', '540', '1500', '300']
    elif action == 'swipe_left':
        return adb_prefix + ['shell', 'input', 'swipe', '900', '960', '100', '960', '300']
    elif action == 'swipe_right':
        return adb_prefix + ['shell', 'input', 'swipe', '100', '960', '900', '960', '300']
    elif action == 'scroll_up':
        return adb_prefix + ['shell', 'input', 'swipe', '540', '1500', '540', '300', '300']
    elif action == 'scroll_down':
        return adb_prefix + ['shell', 'input', 'swipe', '540', '300', '540', '1500', '300']
    elif action == 'open_notifications':
        return adb_prefix + ['shell', 'input', 'keyevent', 'KEYCODE_NOTIFICATION']
    elif action == 'open_quick_settings':
        return adb_prefix + ['shell', 'input', 'keyevent', 'KEYCODE_QUICK_SETTINGS']
    elif action == 'open_recent_apps':
        return adb_prefix + ['shell', 'input', 'keyevent', 'KEYCODE_APP_SWITCH']
    else:
        logger.warning("Unknown action for ADB command: %s", action)
        return adb_prefix + ['shell', 'echo', f'unknown_action:{action}']


def execute_workflow(
    workflow: Workflow,
    device: str = None,
    on_step: Optional[Callable[[int, WorkflowStep, dict], None]] = None,
) -> dict:
    """Execute a workflow step by step.

    Runs each step via ADB, waits the specified delay, and optionally
    calls a callback after each step.

    Args:
        workflow: Workflow instance to execute.
        device: Optional ADB device serial.
        on_step: Optional callback(step_index, step, result) called after each step.

    Returns:
        Dict with 'success', 'completed_steps', 'total_steps', 'errors'.
    """
    results = []
    errors = []
    total = len(workflow.steps)

    for i, step in enumerate(workflow.steps):
        logger.info("Executing step %d/%d: %s", i + 1, total, step.action)

        # Build and execute ADB command
        cmd = _build_adb_command(step.action, step.params, device)
        step_result = {'step': i, 'action': step.action, 'success': False}

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            step_result['success'] = result.returncode == 0
            step_result['stdout'] = result.stdout.strip()
            step_result['stderr'] = result.stderr.strip()
            if result.returncode != 0:
                logger.warning(
                    "Step %d failed (rc=%d): %s",
                    i, result.returncode, result.stderr.strip(),
                )
                errors.append({'step': i, 'error': result.stderr.strip()})
        except subprocess.TimeoutExpired:
            logger.error("Step %d timed out", i)
            step_result['error'] = 'timeout'
            errors.append({'step': i, 'error': 'timeout'})
        except FileNotFoundError:
            logger.error("adb not found")
            step_result['error'] = 'adb not found'
            errors.append({'step': i, 'error': 'adb not found'})
            break
        except Exception as e:
            logger.error("Step %d exception: %s", i, e)
            step_result['error'] = str(e)
            errors.append({'step': i, 'error': str(e)})

        results.append(step_result)

        # Call the step callback
        if on_step:
            try:
                on_step(i, step, step_result)
            except Exception as e:
                logger.warning("on_step callback error at step %d: %s", i, e)

        # Wait after step
        if step.wait_after > 0:
            time.sleep(step.wait_after)

    completed = len(results)
    success = len(errors) == 0

    logger.info(
        "Workflow '%s' completed: %d/%d steps, %d errors",
        workflow.name, completed, total, len(errors),
    )

    return {
        'success': success,
        'completed_steps': completed,
        'total_steps': total,
        'errors': errors,
        'results': results,
    }


def save_workflow(workflow: Workflow, directory: str) -> str:
    """Save a workflow to a JSON file.

    Args:
        workflow: Workflow instance to save.
        directory: Directory to save the JSON file in.

    Returns:       Full path to the saved file.
    """
    os.makedirs(directory, exist_ok=True)
    # Sanitize name for filename
    safe_name = workflow.name.replace(' ', '_').replace('/', '_')
    filepath = os.path.join(directory, f'{safe_name}.json')

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(workflow.to_dict(), f, indent=2, ensure_ascii=True)

    logger.info("Saved workflow '%s' to %s", workflow.name, filepath)
    return filepath


def load_workflow(name: str, directory: str) -> Workflow:
    """Load a workflow from a JSON file.

    Args:
        name: Workflow name to load.
        directory: Directory containing workflow JSON files.

    Returns:
        Loaded Workflow instance.

    Raises:
        FileNotFoundError: If the workflow file does not exist.
    """
    safe_name = name.replace(' ', '_').replace('/', '_')
    filepath = os.path.join(directory, f'{safe_name}.json')

    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    logger.info("Loaded workflow '%s' from %s", name, filepath)
    return Workflow.from_dict(data)


def list_workflows(directory: str) -> list[str]:
    """List saved workflow names in a directory.

    Args:
        directory: Directory to scan for workflow JSON files.

    Returns:
        List of workflow names (without .json extension).
    """
    if not os.path.isdir(directory):
        return []

    names = []
    for filename in sorted(os.listdir(directory)):
        if filename.endswith('.json'):
            names.append(filename[:-5])  # Remove .json extension

    return names


def delete_workflow(name: str, directory: str) -> bool:
    """Delete a saved workflow file.

    Args:
        name: Workflow name to delete.
        directory: Directory containing workflow JSON files.

    Returns:
        True if the file was deleted, False if it did not exist.
    """
    safe_name = name.replace(' ', '_').replace('/', '_')
    filepath = os.path.join(directory, f'{safe_name}.json')

    if os.path.exists(filepath):
        os.remove(filepath)
        logger.info("Deleted workflow '%s'", name)
        return True

    logger.warning("Workflow '%s' not found for deletion", name)
    return False
