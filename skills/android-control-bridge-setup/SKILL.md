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

This setup runs on the Windows host, not inside the A0 container. The ADB bridge
is shared by every Docker container on the same host through
`host.docker.internal:5037`, so one working bridge can make Android Control work
in multiple A0 or Agentspine containers.

Use the temporary setup first. Only create the persistent logon task after the
user explicitly agrees to a host-level startup entry.

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

Keep that terminal open while testing.

4. Verify from A0:

```bash
/a0/usr/plugins/droidclaw/data/platform-tools/adb -H host.docker.internal -P 5037 mdns check
/a0/usr/plugins/droidclaw/data/platform-tools/adb -H host.docker.internal -P 5037 devices -l
```

5. In Android Control, recheck bridge status. QR pairing should become available
when the host bridge is reachable. Wired USB devices should appear after the
phone is plugged in and USB debugging is authorized.

### Windows Persistent Startup

If the temporary bridge works and the user wants QR/USB support after a reboot,
create a current-user scheduled task that starts the host ADB bridge at logon.

First, explain what will be created:

- a script at `%LOCALAPPDATA%\AndroidControlBridge\start-adb-bridge.ps1`
- a hidden wrapper at `%LOCALAPPDATA%\AndroidControlBridge\start-adb-bridge-hidden.vbs`
- a scheduled task named `AndroidControlAdbBridge`
- the task runs at Windows logon and starts `adb -a -P 5037 start-server`
  without leaving a visible terminal window

Then run this from the Windows host, replacing `$adb` automatically from
`where.exe adb`:

```powershell
$adb = (where.exe adb | Select-Object -First 1).Trim()
if (-not $adb) { throw "adb.exe was not found on PATH" }

$bridgeDir = Join-Path $env:LOCALAPPDATA "AndroidControlBridge"
New-Item -ItemType Directory -Force -Path $bridgeDir | Out-Null

$scriptPath = Join-Path $bridgeDir "start-adb-bridge.ps1"
$vbsPath = Join-Path $bridgeDir "start-adb-bridge-hidden.vbs"
@"
`$adb = '$adb'
if (-not (Test-Path -LiteralPath `$adb)) { exit 1 }
& `$adb kill-server *>`$null
Start-Sleep -Seconds 1
& `$adb -a -P 5037 start-server *>`$null
Start-Sleep -Seconds 1
"@ | Set-Content -LiteralPath $scriptPath -Encoding UTF8

@"
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$scriptPath""", 0, False
"@ | Set-Content -LiteralPath $vbsPath -Encoding ASCII

$action = New-ScheduledTaskAction `
  -Execute "wscript.exe" `
  -Argument "`"$vbsPath`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

Register-ScheduledTask `
  -TaskName "AndroidControlAdbBridge" `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Starts Android Control host ADB bridge on logon for Docker containers." `
  -Force | Out-Null

wscript.exe "$vbsPath"
```

Verify persistence setup:

```powershell
Get-ScheduledTask -TaskName AndroidControlAdbBridge
netstat -ano | Select-String ":5037"
```

Verify from A0 again:

```bash
/a0/usr/plugins/droidclaw/data/platform-tools/adb -H host.docker.internal -P 5037 mdns check
/a0/usr/plugins/droidclaw/data/platform-tools/adb -H host.docker.internal -P 5037 devices -l
```

To remove the persistent bridge later:

```powershell
Unregister-ScheduledTask -TaskName AndroidControlAdbBridge -Confirm:$false
Remove-Item -LiteralPath "$env:LOCALAPPDATA\AndroidControlBridge" -Recurse -Force
adb kill-server
```

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
