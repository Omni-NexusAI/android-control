# Android Control

Android Control is an Agent Zero plugin for controlling Android devices through ADB. It provides USB and wireless device discovery, wireless pairing, Tailscale remote connection support, screenshot capture, direct Android actions, and agent-guided task execution from the Agent Zero UI.

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
   - **USB ADB:** plug the device into the Agent Zero host, accept the Android RSA prompt, then refresh devices in Android Control.
   - **Wireless pairing:** open Android's Wireless debugging screen, choose pair by code or QR/manual details, then use **Pair New Device** from the Android Control panel.
   - **Tailscale remote device:** open the Tailscale Android app on the target phone and join the same tailnet first. If Android Control's A0/container node is not on the tailnet yet, Android Control shows an auth URL or QR code for authorizing the A0/container node. That QR or URL does not enroll the phone into Tailscale. Once both sides are online, use **Connect with Tailscale**. Android Control can either request classic ADB TCP/IP on the selected port, usually `5555`, or use the actual Wireless debugging connect port discovered from ADB mDNS when the phone is already advertising one.
   - **Host ADB server:** if ADB is already running outside the Agent Zero container, set Android Control to use the host ADB endpoint, usually `host.docker.internal:5037`.

3. Start using Android Control:
   - Open the Android Control panel from Agent Zero.
   - Select a connected device.
   - Use quick actions such as Wake, Home, Back, Recents, Screenshot, and Dump.
   - Run an Android task when you want the agent to operate apps or repeat a workflow.

## Features

- USB, wireless ADB, and Tailscale remote-device connection workflows
- Wireless ADB pairing and reconnect workflows
- Tailscale remote-device discovery with online or offline and ADB-ready status
- Tailscale ADB connection over a tailnet with dynamic ADB port probing
- Device selector with connected-device status
- Control panel with quick actions, screenshots, task launch, and workflow controls
- Direct ADB command API for focused actions
- `droidclaw_run` autonomous task tool for multi-step Android workflows
- System prompt context injection for connected Android device state

## Configuration

Android Control has its own plugin settings. The model assigned to Android Control can be separate from Agent Zero's built-in model profile, and it can point to either local or cloud providers depending on the provider and API endpoint you configure.

Important settings:

- `adb_backend`: chooses how Android Control reaches ADB. The marketplace default is `auto_container_first`, which starts and uses the plugin-owned ADB daemon inside Agent Zero. Use `host` only when you intentionally want an already-running host ADB server.
- `adb_host` / `adb_port`: host ADB server endpoint when `adb_backend` is set to `host`.
- `adb_path`: optional absolute path to an `adb` client. Leave empty to use PATH, bundled plugin-owned platform-tools, or common Android SDK locations.
- `tailscale_adb_port`: default classic ADB TCP/IP port to request when Android Control runs `adb tcpip`. `5555` is the conventional default, but Android Wireless debugging may advertise a different connect port and Android Control probes discovered mDNS ports too.
- `tailscale_android_only`: when enabled, limits Tailscale peer discovery to Android devices.
- `tailscale_online_only`: when enabled, limits the peer list to devices that are currently online in the tailnet.
- `tailscale_probe_ports`: controls whether Android Control probes discovered Tailscale peers for reachable ADB ports before offering quick-connect guidance.
- `tailscale_cache_ttl`: cache lifetime, in seconds, for discovered Tailscale peer state.
- `tailscale_preferred_peers`: optional allowlist for narrowing the Tailscale peer list to specific devices or hostnames.
- `device`: optional preferred Android device id.
- `provider`, `default_model`, `api_base`: plugin-local model provider, model name, and API endpoint for Android Control task execution.
- `model_supports_vision` / `vision_mode`: controls whether screenshots can be used by the task loop and how vision should be handled.
- `max_steps`, `step_delay`, `stuck_threshold`, `max_elements`, `auto_intervene`: task loop limits and safety controls.

## Tailscale Notes

- Put the phone on the tailnet through the Tailscale Android app before using **Connect with Tailscale**.
- If Android Control is not yet on the tailnet, the plugin can show an auth URL or QR code for the A0/container node.
- The Android Control auth URL or QR code authorizes the A0/container node only. It does not pair or enroll the phone.
- If a phone is online in Tailscale but ADB is not reachable, Android Control will report that separately so you can enable classic ADB TCP/IP, confirm the Wireless debugging connect port, or allow incoming Tailscale connections on the phone.

## Task Modes

- **Auto:** Android Control chooses the best route based on the task prompt and current context.
- **Tier 1:** Uses the plugin-local model configuration only. This is intended for local models or a separately configured Android Control model.
- **Tier 2:** Uses the current Agent Zero model profile when you want Android Control to follow the main A0 model configuration.
- **Workflow path:** Intended for recorded and repeatable tasks that can run without any LLM involved. This path is still work in progress and may have bugs while the recording and replay behavior is tested.

## Planned Updates

- Realtime framerate target selector for 12, 24, 30, 40, and 60 fps
- Better parallelized multi-device controls
- Self-corrective tasking system to help a wider range of models complete complex Android tasks over time
