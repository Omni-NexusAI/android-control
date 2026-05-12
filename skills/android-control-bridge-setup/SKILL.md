---
name: android-control-bridge-setup
description: >
  Set up the external ADB visibility that Android Control needs for QR pairing
  and wired USB detection when Agent Zero runs inside Docker.
---

# Android Control Bridge Setup

Use this skill when Android Control reports that full device access is not ready,
QR pairing is unavailable, USB devices are not visible, or the A0 container can
run `adb` but cannot see phones.

## What Must Be True

Android Control installs its own ADB client inside A0, but Docker may still hide
the phone from the container.

- QR pairing requires an ADB backend that can discover Android Wireless ADB mDNS
  services: `_adb-tls-pairing._tcp` and `_adb-tls-connect._tcp`.
- Wired USB requires an ADB backend that can see the physical USB device.
- On Windows Docker Desktop, the recommended bridge is a Windows host ADB server
  reachable from A0 at `host.docker.internal:5037`.

## Windows Host ADB Bridge

1. Find `adb.exe` on the Windows host:

```powershell
where.exe adb
```

2. Stop any localhost-only ADB server:

```powershell
adb kill-server
```

3. Start ADB bound to all interfaces so the A0 container can reach it:

```powershell
adb -a -P 5037 nodaemon server
```

Keep that terminal open while testing. A future host-side service or scheduled
task can make this persistent, but Android Control itself cannot create or start
Windows host services from inside Docker.

4. Verify from A0:

```bash
/a0/usr/plugins/droidclaw/data/platform-tools/adb -H host.docker.internal -P 5037 mdns check
/a0/usr/plugins/droidclaw/data/platform-tools/adb -H host.docker.internal -P 5037 devices -l
```

5. In Android Control, recheck bridge status. QR pairing should become available
when the host bridge is reachable. Wired USB devices should appear after the
phone is plugged in and USB debugging is authorized.

## Linux Host or Native Docker

Prefer one of these setups:

- run A0 with host networking when appropriate, so mDNS can reach the container;
- pass USB devices into the container, commonly `/dev/bus/usb`;
- or run a host ADB server reachable from the container and set Android Control
  to use that host/port.

Verify inside A0:

```bash
/a0/usr/plugins/droidclaw/data/platform-tools/adb mdns check
/a0/usr/plugins/droidclaw/data/platform-tools/adb mdns services
/a0/usr/plugins/droidclaw/data/platform-tools/adb devices -l
```

## Expected Results

- QR ready: Android Control can run mDNS checks through a backend that reaches
  Wireless ADB services.
- USB ready: Android Control can query an ADB backend that sees authorized USB
  devices.
- If either remains unavailable, report the backend diagnostics from Android
  Control instead of retrying QR scans repeatedly.
