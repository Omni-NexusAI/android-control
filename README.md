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
- `adb_keepalive` / `adb_keepalive_interval`: keep in-use wireless ADB transports alive with a light ping (default on, every 20 seconds).
- `device`: optional preferred Android device id.
- `provider`, `default_model`, `api_base`: plugin-local model provider, model name, and API endpoint for Android Control task execution.
- `model_supports_vision` / `vision_mode` / `vision_image_max_edge`: whether the task loop may attach screenshots, when (`off`, `fallback`, `always`), and how large they are (default 1280 px long edge, JPEG).
- `max_steps`, `step_delay`, `stuck_threshold`, `max_elements`, `auto_intervene`: task loop limits and safety controls.

## Connection Reliability

Android Control keeps the ADB connection fast and self-healing:

- The selected ADB server (host bridge or container-local) is cached and only re-probed when it actually fails, so a single slow probe can no longer silently switch to a server that cannot see your phone.
- Device lists are cached for a couple of seconds and Tailscale discovery refreshes in the background, so each tap or swipe runs one ADB command instead of five or six.
- When a command fails because the transport dropped (wireless debugging went idle, the phone changed its Wireless debugging port, or the transport went `offline`), Android Control reconnects using the live mDNS port or the last known endpoint and retries the command once. The agent sees the successful result instead of spending tool calls reconnecting.
- Known wireless endpoints are remembered in `data/known_devices.json`.

## Vision

Android Control uses a "set-of-marks" approach so screenshots add information instead of confusing the model:

- Every step lists the meaningful on-screen elements in reading order, numbered `[1]..[N]`, with kind, label, state and center.
- When a screenshot is attached, each listed element is boxed on it and labelled with the same number. The model answers `{"action": "tap", "element": 7}` and Android Control taps that element's real center, so image downscaling can never cause a wrong tap.
- In `fallback` mode the screenshot is attached only when it helps: no or very few labelled elements, many unlabelled icons, web/canvas content, the last action changed nothing, or the model asks with `{"action": "look"}`. `always` attaches it every step.
- Screenshots are downscaled JPEGs, not full-resolution PNGs, and only the current one is sent.
- The model sees its recent actions and whether each one changed the screen; when stuck, recovery escalates (back, then scroll, then home) and the model is told what happened.
- If the model or endpoint rejects images, the run continues text-only and the summary says so.
- The main Agent Zero agent can use `adb_screen` mode `annotated` to get the numbered list plus the marked screenshot (to open with `vision_load`) and then `adb_tools` `tap` with `"element": N`.

## Workflows (record once, replay without an LLM)

- **Record from the panel:** name the workflow, press **Record**, then drive the phone with the quick actions, the agent, or any Android Control tool. Every input sent to that phone is captured. Press **Stop & Save**.
- **Record from the agent:** `droidclaw_workflow` action `record_start` / `record_stop`, or simply run `droidclaw_run` with `"record_as": "<name>"`. A completed run is saved and actions that changed nothing are left out.
- **Replay:** **Play** in the panel (live per-step progress) or `droidclaw_workflow` action `play`. Each tap re-finds its element by resource id, text and description, waits up to 8 seconds for it to appear, and taps its current position, so replays survive layout shifts and different screen sizes. Steps without an identifiable element are scaled to the target screen. Playback stops at the first failed step unless `continue_on_error` is set.
- Workflows are stored as JSON in `data/workflows/`. The old "saved prompts" list in the panel is still available under **Run Task > Saved prompts**.

## Multiple Phones

Android Control can drive several phones at the same time, for example one subordinate agent per phone.

- **Choosing a phone:** every Android Control tool uses, in order, the `device` argument, the phone the agent bound with `adb_device` action `select`, the configured default `device`, or the only connected phone. If several phones are connected and none of these applies, the tool returns the list of serials instead of guessing.
- **No collisions:** a phone being driven by one agent is claimed for it; another agent gets a clear "in use by" message. Claims lapse automatically 90 seconds after the agent's last action, and actions from the panel are never blocked. Screen dumps and screenshots use unique per-device files, so concurrent agents never read each other's screens.
- **Running in parallel:** use Agent Zero's `parallel` tool, with one `droidclaw_run` per phone (each with its own `device`), or one `call_subordinate` per phone whose message names the serial and tells the subordinate to `select` it first.
- Two identical phone models stay separate devices; the same phone attached over USB and Wi-Fi is shown once.

## Tailscale Notes

- Put the phone on the tailnet through the Tailscale Android app before using **Connect with Tailscale**.
- If Android Control is not yet on the tailnet, the plugin can show an auth URL or QR code for the A0/container node.
- The Android Control auth URL or QR code authorizes the A0/container node only. It does not pair or enroll the phone.
- If a phone is online in Tailscale but ADB is not reachable, Android Control will report that separately so you can enable classic ADB TCP/IP, confirm the Wireless debugging connect port, or allow incoming Tailscale connections on the phone.

## Task Modes

- **Auto:** Android Control chooses the best route based on the task prompt and current context.
- **Tier 1:** Uses the plugin-local model configuration only. This is intended for local models or a separately configured Android Control model.
- **Tier 2:** Uses the current Agent Zero model profile when you want Android Control to follow the main A0 model configuration.
- **Workflows:** recorded, repeatable tasks that replay without any LLM (see below).

## Planned Updates

- Realtime framerate target selector for 12, 24, 30, 40, and 60 fps
- Self-corrective tasking system to help a wider range of models complete complex Android tasks over time
