"""Read-only Windows driver diagnostics; never opens or rebinds a USB device."""
from __future__ import annotations

import json
import re
import subprocess
import sys


# SetupAPI interface used by the DCamUSB.dll distributed with this project.
# This is a device-interface GUID, not the Device Manager setup-class GUID.
BUNDLED_DCAMUSB_INTERFACE_GUID = "{2ae95266-dd2b-4dc4-ae37-972b9c2f02f3}"

_PNP_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$devices = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_0661*' })
$result = @($devices | ForEach-Object {
    $dev = $_
    $service = Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName 'DEVPKEY_Device_Service' -ErrorAction SilentlyContinue
    $inf = Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName 'DEVPKEY_Device_DriverInfPath' -ErrorAction SilentlyContinue
    $problem = Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction SilentlyContinue
    $parameters = Get-ItemProperty -LiteralPath ('Registry::HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Enum\' + $dev.InstanceId + '\Device Parameters') -ErrorAction SilentlyContinue
    [PSCustomObject]@{
        instance_id = $dev.InstanceId
        name = $dev.FriendlyName
        status = [string]$dev.Status
        service = $service.Data
        driver_inf = $inf.Data
        problem_code = $problem.Data
        interface_guids = @((@($parameters.DeviceInterfaceGUIDs) + @($parameters.DeviceInterfaceGUID)) | Where-Object { $_ })
    }
})
ConvertTo-Json -InputObject $result -Depth 4 -Compress
"""


def collect_windows_usb_diagnostics():
    """Return a snapshot, distinguishing an empty USB list from a failed query."""
    if sys.platform != "win32":
        return {"status": "unavailable", "devices": [], "error": "Windows USB diagnostics require Windows."}
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PNP_QUERY],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or f"PowerShell exited with {completed.returncode}")
        devices = json.loads(completed.stdout.lstrip("\ufeff").strip())
        if isinstance(devices, dict):
            devices = [devices]
        if not isinstance(devices, list) or any(not isinstance(device, dict) for device in devices):
            raise ValueError("Unexpected Windows USB diagnostic response")
        for device in devices:
            match = re.search(r"PID_([0-9A-F]{4})", str(device.get("instance_id", "")), re.IGNORECASE)
            device["product_id"] = match.group(1).upper() if match else "unknown"
            guids = device.get("interface_guids") or []
            device["interface_guids"] = [guids] if isinstance(guids, str) else guids
        return {"status": "ok", "devices": devices}
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        return {"status": "unavailable", "devices": [], "error": str(exc)}


def format_windows_usb_diagnostics(snapshot):
    if snapshot["status"] != "ok":
        return "Windows USB diagnostics unavailable: " + snapshot.get("error", "query failed")
    if not snapshot["devices"]:
        return "Windows reports no connected Hamamatsu USB devices (VID 0661). Check power and the USB cable."
    descriptions = []
    for device in snapshot["devices"]:
        details = [f"PID {device['product_id']}"]
        for key, label in (("name", "name"), ("status", "status"), ("service", "service"), ("driver_inf", "INF")):
            if device.get(key):
                details.append(f"{label}={device[key]}")
        if device.get("problem_code") is not None:
            details.append(f"problem={device['problem_code']}")
        guids = device.get("interface_guids", [])
        if guids:
            details.append("interface GUIDs=" + ", ".join(guids))
        descriptions.append("; ".join(details))
    return "Windows USB: " + " | ".join(descriptions)
