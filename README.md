# Android Control

Android Control is an Agent Zero plugin for controlling Android devices through ADB. It provides device discovery, wireless pairing, screenshot capture, direct Android actions, and agent-guided task execution from the Agent Zero UI.

## What makes Android Control useful?

Android Control gives an agent access to the same phone interface a person would normally use: installed apps, signed-in accounts, notifications, browser sessions, files, settings, and app-specific workflows.

That can reduce the need to build or install a dedicated MCP for every service. If a task can already be done through an Android app, Android Control can let the agent use that existing app interface instead. With hundreds of thousands of Android apps available, this opens a practical path for agents to work across tools and services that do not have MCP servers, public APIs, or mature automation integrations.

## Get Started

1. Prepare your Android device:
   - Open **Settings > About phone** and tap **Build number** seven times to enable Developer Options.
   - Open **Settings > System > Developer options**.
   - Enable **USB debugging** for cable connections.
   - Enable **Wireless debugging** if you want to pair over Wi-Fi.

2. Connect a device:
   - By default, use Android's built-in manual ADB pairing flow. Pair the phone from Android's Wireless debugging screen, then connect it from Android Control with the device address and pairing code.
   - Android Control installs its own ADB client inside A0, so no Android SDK install is required inside the container.
   - QR pairing and wired USB passthrough are optional convenience features. They need extra host/container visibility because A0 may not be able to see Android Wireless ADB discovery or physical USB devices from inside Docker.

3. Optional: enable QR pairing or wired USB:
   - Set up the Android Control bridge if you want QR pairing or wired USB device detection.
   - The plugin includes the `android-control-bridge-setup` skill for this. Use an agent outside the A0 container, such as Codex, Agent 0 CLI, or another terminal-capable helper, and have it connect to A0's MCP/tools so it can read the plugin skill and verify the bridge from both sides.
   - A simple prompt for that external agent is:

```text
Use A0's android-control-bridge-setup skill to set up Android Control bridge access for QR pairing and USB device detection. Verify that A0 can reach the host ADB bridge and that Android Control reports QR/USB ready.
```

4. Start using Android Control:
   - Open the Android Control panel from Agent Zero.
   - Select a connected device.
   - Use quick actions such as Wake, Home, Back, Recents, Screenshot, and Dump.
   - Run an Android task when you want the agent to operate apps or repeat a workflow.

## Bridge Setup

Manual ADB pairing is the default path because it works without giving the A0 container direct access to host networking or USB devices. QR pairing and wired USB are different: they require A0 to see Android Wireless ADB discovery, physical USB devices, or a host ADB server that can see them.

If the pairing screen shows **Bridge setup needed**, Android Control is installed correctly, but QR pairing or USB passthrough is not available from the current container view yet.

### Windows Docker Host Bridge

On Windows Docker Desktop, start host ADB so the A0 container can reach it:

```powershell
adb kill-server
adb -a -P 5037 nodaemon server
```

Keep that terminal open while testing. Then verify from inside A0:

```bash
/a0/usr/plugins/droidclaw/data/platform-tools/adb -H host.docker.internal -P 5037 mdns check
/a0/usr/plugins/droidclaw/data/platform-tools/adb -H host.docker.internal -P 5037 devices -l
```

Android Control also includes the `android-control-bridge-setup` skill, which an external helper such as Agent 0 CLI, Codex, or another terminal-capable agent can use to guide and verify the bridge setup.

## Features

- Wireless ADB pairing and reconnect workflows
- Device selector with connected-device status
- Control panel with quick actions, screenshots, task launch, and workflow controls
- Direct ADB command API for focused actions
- `droidclaw_run` autonomous task tool for multi-step Android workflows
- System prompt context injection for connected Android device state

## Configuration

Android Control has its own plugin settings. The model assigned to Android Control can be separate from Agent Zero's built-in model profile, and it can point to either local or cloud providers depending on the provider and API endpoint you configure.

Important settings:

- `adb_backend`: chooses how Android Control reaches ADB. The marketplace default is `auto_host_first`, which uses a reachable host ADB bridge when available and falls back to the plugin-owned ADB daemon inside Agent Zero.
- `adb_host` / `adb_port`: host ADB server endpoint when `adb_backend` is set to `host`.
- `adb_path`: optional absolute path to an `adb` client. Leave empty to use PATH, bundled plugin-owned platform-tools, or common Android SDK locations.
- `device`: optional preferred Android device id.
- `provider`, `default_model`, `api_base`: plugin-local model provider, model name, and API endpoint for Android Control task execution.
- `model_supports_vision` / `vision_mode`: controls whether screenshots can be used by the task loop and how vision should be handled.
- `max_steps`, `step_delay`, `stuck_threshold`, `max_elements`, `auto_intervene`: task loop limits and safety controls.

## Task Modes

- **Auto:** Android Control chooses the best route based on the task prompt and current context.
- **Tier 1:** Uses the plugin-local model configuration only. This is intended for local models or a separately configured Android Control model.
- **Tier 2:** Uses the current Agent Zero model profile when you want Android Control to follow the main A0 model configuration.
- **Workflow path:** Intended for recorded and repeatable tasks that can run without any LLM involved. This path is still work in progress and may have bugs while the recording and replay behavior is tested.

## Planned Updates

- Tailscale connection support
- Realtime framerate target selector for 12, 24, 30, 40, and 60 fps
- Better parallelized multi-device controls
- Self-corrective tasking system to help a wider range of models complete complex Android tasks over time
