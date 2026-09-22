"""Run on the Windows bench PC: python tools/diagnose_hamamatsu.py [--dll PATH].

Prints JSON to share when a connection fails. Reads files and PnP/registry device
properties only: no DLL initialization, firmware upload, or driver changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spectrometers.hamamatsu_diagnostics import BUNDLED_DCAMUSB_INTERFACE_GUID, collect_windows_usb_diagnostics


def dll_details(path):
    path = Path(path).expanduser().resolve()
    result = {"path": str(path), "exists": path.is_file()}
    if not result["exists"]:
        return result
    try:
        data = path.read_bytes()
        offset = struct.unpack_from("<I", data, 0x3C)[0]
        if data[:2] != b"MZ" or data[offset:offset + 4] != b"PE\0\0":
            raise ValueError("Not a Windows PE DLL")
        machine = struct.unpack_from("<H", data, offset + 4)[0]
        bits = {0x014C: 32, 0x8664: 64, 0xAA64: 64}.get(machine)
        result.update({
            "machine": {0x014C: "x86", 0x8664: "x64", 0xAA64: "ARM64"}.get(machine, hex(machine)),
            "bits": bits,
            "matches_python_bitness": bits == struct.calcsize("P") * 8 if bits else None,
            "sha256": hashlib.sha256(data).hexdigest(),
            "contains_c10785_model_name": b"C10785\0" in data,
        })
    except (OSError, ValueError, struct.error) as exc:
        result["error"] = str(exc)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path, default=ROOT / "spectrometers dll files" / "DCamUSB.dll")
    args = parser.parse_args()
    print(json.dumps({
        "platform": platform.platform(),
        "python": sys.version,
        "python_executable": sys.executable,
        "python_bits": struct.calcsize("P") * 8,
        "dll": dll_details(args.dll),
        "bundled_dll_interface_guid": BUNDLED_DCAMUSB_INTERFACE_GUID,
        "windows_usb": collect_windows_usb_diagnostics(),
        "next_step": "Use MiniSpec with the matching vendor DCamUSB.dll and close other acquisition applications.",
    }, indent=2))


if __name__ == "__main__":
    main()
