import asyncio
import json
import os
import sys
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

from usr.plugins.droidclaw.helpers.adb_backend import adb_cmd, resolve_device

# Default workflows directory
WORKFLOWS_DIR = '/a0/usr/plugins/droidclaw/workflows'

# Recording state file
RECORDING_FLAG = '/tmp/droidclaw_recording.json'


def _adb_device_cmd(args: list, device: str = '') -> list:
    resolution = resolve_device(device if device is not None else "")
    return adb_cmd(args, device=resolution.get("resolved_device") or device)


@dataclass
class WorkflowStep:
    """A single step in a workflow."""
    action: str
    params: Dict = field(default_factory=dict)
    delay_after: float = 0.5


@dataclass
class Workflow:
    """A named sequence of workflow steps."""
    name: str
    steps: List[WorkflowStep] = field(default_factory=list)
    created: str = ''
    device: str = ''

    def __post_init__(self):
        if not self.created:
            self.created = time.strftime('%Y-%m-%d %H:%M:%S')


def save_workflow(workflow: Workflow, workflows_dir: str = WORKFLOWS_DIR) -> str:
    """Save a workflow to a JSON file.

    Args:
        workflow: Workflow object to save
        workflows_dir: Directory to save workflow files

    Returns:
        Status message string
    """
    os.makedirs(workflows_dir, exist_ok=True)

    # Sanitize name for filename
    safe_name = workflow.name.replace(' ', '_').replace('/', '_')
    safe_name = ''.join(c for c in safe_name if c.isalnum() or c in ('_', '-'))
    filepath = os.path.join(workflows_dir, f'{safe_name}.json')

    # Convert to serializable dict
    data = {
        'name': workflow.name,
        'created': workflow.created,
        'device': workflow.device,
        'steps': [
            {
                'action': step.action,
                'params': step.params,
                'delay_after': step.delay_after,
            }
            for step in workflow.steps
        ]
    }

    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

    return f'Workflow "{workflow.name}" saved ({len(workflow.steps)} steps) -> {filepath}'


def load_workflow(name: str, workflows_dir: str = WORKFLOWS_DIR) -> Workflow:
    """Load a workflow from a JSON file.

    Args:
        name: Workflow name to load
        workflows_dir: Directory containing workflow files

    Returns:
        Workflow object

    Raises:
        FileNotFoundError: If workflow file not found
        ValueError: If workflow data is invalid
    """
    safe_name = name.replace(' ', '_').replace('/', '_')
    safe_name = ''.join(c for c in safe_name if c.isalnum() or c in ('_', '-'))
    filepath = os.path.join(workflows_dir, f'{safe_name}.json')

    if not os.path.exists(filepath):
        raise FileNotFoundError(f'Workflow "{name}" not found at {filepath}')

    with open(filepath, 'r') as f:
        data = json.load(f)

    steps = []
    for step_data in data.get('steps', []):
        steps.append(WorkflowStep(
            action=step_data.get('action', ''),
            params=step_data.get('params', {}),
            delay_after=step_data.get('delay_after', 0.5),
        ))

    return Workflow(
        name=data.get('name', name),
        steps=steps,
        created=data.get('created', ''),
        device=data.get('device', ''),
    )


def list_workflows(workflows_dir: str = WORKFLOWS_DIR) -> List[str]:
    """List all saved workflow names.

    Args:
        workflows_dir: Directory containing workflow files

    Returns:
        List of workflow names (without .json extension)
    """
    if not os.path.isdir(workflows_dir):
        return []

    names = []
    for filename in sorted(os.listdir(workflows_dir)):
        if filename.endswith('.json'):
            name = filename[:-5]  # Remove .json
            names.append(name)

    return names


def delete_workflow(name: str, workflows_dir: str = WORKFLOWS_DIR) -> bool:
    """Delete a workflow file.

    Args:
        name: Workflow name to delete
        workflows_dir: Directory containing workflow files

    Returns:
        True if deleted, False if not found
    """
    safe_name = name.replace(' ', '_').replace('/', '_')
    safe_name = ''.join(c for c in safe_name if c.isalnum() or c in ('_', '-'))
    filepath = os.path.join(workflows_dir, f'{safe_name}.json')

    if os.path.exists(filepath):
        os.remove(filepath)
        return True
    return False


def execute_workflow(workflow: Workflow, device: str = '') -> str:
    """Execute a workflow by running each step via ADB.

    Args:
        workflow: Workflow to execute
        device: ADB device address (auto-detect if empty)

    Returns:
        Formatted execution results
    """
    if not device:
        device = workflow.device

    if not device:
        device = _auto_detect_device()

    if not device:
        return 'Error: No ADB device found. Connect a device first.'

    results = []
    results.append(f'Executing workflow: {workflow.name}')
    results.append(f'Device: {device}')
    results.append(f'Steps: {len(workflow.steps)}')
    results.append('=' * 50)

    success_count = 0
    fail_count = 0

    for i, step in enumerate(workflow.steps):
        step_result = _execute_step(step, device)
        status = 'OK' if step_result else 'FAIL'

        if step_result:
            success_count += 1
        else:
            fail_count += 1

        results.append(f'  [{i}] {step.action} -> {status}')

        if step.params:
            params_str = json.dumps(step.params)
            if len(params_str) > 80:
                params_str = params_str[:77] + '...'
            results.append(f'       params: {params_str}')

        # Delay between steps
        if step.delay_after > 0 and i < len(workflow.steps) - 1:
            time.sleep(step.delay_after)

    results.append('=' * 50)
    results.append(f'Results: {success_count} succeeded, {fail_count} failed')

    return '\n'.join(results)


def _auto_detect_device() -> str:
    """Auto-detect connected ADB device."""
    resolution = resolve_device("")
    return resolution.get("resolved_device") or ''


def _execute_step(step: WorkflowStep, device: str) -> bool:
    """Execute a single workflow step via ADB.

    Supported action types:
        - tap: params x, y
        - swipe: params x1, y1, x2, y2, duration (ms)
        - type: params text
        - press: params key (KEYCODE name)
        - wait: params seconds
        - shell: params command
        - start: params package, activity
        - force_stop: params package

    Returns:
        True if step succeeded, False otherwise
    """
    params = step.params
    action = step.action.lower()

    try:
        if action == 'tap':
            x = params.get('x', 0)
            y = params.get('y', 0)
            result = subprocess.run(
                _adb_device_cmd(['shell', 'input', 'tap', str(x), str(y)], device=device),
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0

        elif action == 'swipe':
            x1 = params.get('x1', 0)
            y1 = params.get('y1', 0)
            x2 = params.get('x2', 0)
            y2 = params.get('y2', 0)
            duration = params.get('duration', 300)
            result = subprocess.run(
                _adb_device_cmd(['shell', 'input', 'swipe',
                 str(x1), str(y1), str(x2), str(y2), str(duration)], device=device),
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0

        elif action == 'type':
            text = params.get('text', '')
            # Use escaped text for shell
            escaped = text.replace(' ', '%s').replace('&', '\\&')
            result = subprocess.run(
                _adb_device_cmd(['shell', 'input', 'text', escaped], device=device),
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0

        elif action == 'press':
            key = params.get('key', 'KEYCODE_BACK')
            result = subprocess.run(
                _adb_device_cmd(['shell', 'input', 'keyevent', key], device=device),
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0

        elif action == 'wait':
            seconds = float(params.get('seconds', 1.0))
            time.sleep(seconds)
            return True

        elif action == 'shell':
            command = params.get('command', '')
            if not command:
                return False
            result = subprocess.run(
                _adb_device_cmd(['shell'] + command.split(), device=device),
                capture_output=True, text=True, timeout=30
            )
            return result.returncode == 0

        elif action == 'start':
            package = params.get('package', '')
            activity = params.get('activity', '')
            if not package:
                return False
            if activity:
                component = f'{package}/{activity}'
            else:
                component = package
            result = subprocess.run(
                _adb_device_cmd(['shell', 'am', 'start', '-n', component], device=device),
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0

        elif action == 'force_stop':
            package = params.get('package', '')
            if not package:
                return False
            result = subprocess.run(
                _adb_device_cmd(['shell', 'am', 'force-stop', package], device=device),
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0

        else:
            return False

    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


async def execute(**kwargs) -> str:
    """Execute workflow management actions.

    Args:
        action: Operation to perform:
            - run: Load and execute a workflow by name
            - list: List all saved workflows
            - save: Save a new workflow with given name and steps
            - delete: Delete a workflow by name
            - record_start: Start recording ADB actions
            - record_stop: Stop recording and save as workflow
        name: Workflow name (for run, save, delete, record_stop)
        steps: List of step dicts (for save) [{action, params, delay_after}]
        device: ADB device address

    Returns:
        Formatted result string
    """
    action = kwargs.get('action', 'list')
    name = kwargs.get('name', '')
    steps_data = kwargs.get('steps', [])
    device = kwargs.get('device', '')
    workflows_dir = WORKFLOWS_DIR

    if action == 'list':
        names = list_workflows(workflows_dir)
        if not names:
            return 'No saved workflows found.'
        lines = ['Saved Workflows:', '=' * 40]
        for i, wf_name in enumerate(names):
            lines.append(f'  [{i}] {wf_name}')
        lines.append(f'\nTotal: {len(names)} workflows')
        return '\n'.join(lines)

    elif action == 'save':
        if not name:
            return 'Error: workflow name is required for save action'
        if not steps_data:
            return 'Error: steps data is required for save action'

        steps = []
        for step_data in steps_data:
            if isinstance(step_data, dict):
                steps.append(WorkflowStep(
                    action=step_data.get('action', ''),
                    params=step_data.get('params', {}),
                    delay_after=step_data.get('delay_after', 0.5),
                ))
            elif isinstance(step_data, WorkflowStep):
                steps.append(step_data)

        workflow = Workflow(name=name, steps=steps, device=device)
        return save_workflow(workflow, workflows_dir)

    elif action == 'run':
        if not name:
            return 'Error: workflow name is required for run action'

        try:
            workflow = load_workflow(name, workflows_dir)
        except FileNotFoundError as e:
            return str(e)

        result = execute_workflow(workflow, device)
        return result

    elif action == 'delete':
        if not name:
            return 'Error: workflow name is required for delete action'

        if delete_workflow(name, workflows_dir):
            return f'Workflow "{name}" deleted successfully.'
        else:
            return f'Workflow "{name}" not found.'

    elif action == 'record_start':
        os.makedirs(os.path.dirname(RECORDING_FLAG), exist_ok=True)
        record_data = {
            'recording': True,
            'start_time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'device': device,
            'actions': []
        }
        with open(RECORDING_FLAG, 'w') as f:
            json.dump(record_data, f, indent=2)
        return 'Recording started. ADB actions will be captured. Call record_stop to save.'

    elif action == 'record_stop':
        if not name:
            return 'Error: workflow name is required for record_stop action'

        if not os.path.exists(RECORDING_FLAG):
            return 'Error: No active recording found. Call record_start first.'

        with open(RECORDING_FLAG, 'r') as f:
            record_data = json.load(f)

        recorded_actions = record_data.get('actions', [])
        rec_device = record_data.get('device', device)

        # Convert recorded actions to workflow steps
        steps = []
        for act in recorded_actions:
            steps.append(WorkflowStep(
                action=act.get('action', ''),
                params=act.get('params', {}),
                delay_after=act.get('delay_after', 0.5),
            ))

        # If no actions were recorded, return info
        if not steps:
            # Clean up flag file
            os.remove(RECORDING_FLAG)
            return 'No actions recorded. Workflow not saved.'

        workflow = Workflow(name=name, steps=steps, device=rec_device)
        result = save_workflow(workflow, workflows_dir)

        # Clean up flag file
        try:
            os.remove(RECORDING_FLAG)
        except OSError:
            pass

        return result

    else:
        return f'Error: Unknown action "{action}". Valid actions: run, list, save, delete, record_start, record_stop'
