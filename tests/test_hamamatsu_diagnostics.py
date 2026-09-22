"""Regressions for healthy Windows PnP devices invisible to a vendor DLL."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from spectrometers import hamamatsu_diagnostics as diagnostics
from spectrometers import minispec_spectrometer as minispec


@pytest.fixture
def pnp_device():
    return {
        "instance_id": r"USB\VID_0661&PID_3400\example",
        "name": "Hamamatsu USB Camera Module",
        "status": "OK",
        "service": "WinUSB",
        "driver_inf": "oem42.inf",
        "problem_code": 0,
        "interface_guids": diagnostics.BUNDLED_DCAMUSB_INTERFACE_GUID,
    }


def mock_windows_response(monkeypatch, stdout, returncode=0, stderr=""):
    monkeypatch.setattr(diagnostics.sys, "platform", "win32")
    monkeypatch.setattr(
        diagnostics.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode),
    )


@pytest.mark.parametrize("as_array", [False, True])
def test_pnp_snapshot_preserves_healthy_driver_details(monkeypatch, pnp_device, as_array):
    mock_windows_response(monkeypatch, "\ufeff" + json.dumps([pnp_device] if as_array else pnp_device))
    snapshot = diagnostics.collect_windows_usb_diagnostics()
    assert snapshot["status"] == "ok"
    device = snapshot["devices"][0]
    assert device["product_id"] == "3400"
    assert device["interface_guids"] == [diagnostics.BUNDLED_DCAMUSB_INTERFACE_GUID]
    text = diagnostics.format_windows_usb_diagnostics(snapshot)
    assert "service=WinUSB" in text and "INF=oem42.inf" in text
    assert "status=OK" in text and "problem=0" in text


@pytest.mark.parametrize("output,code,error", [("", 1, "Access denied"), ("invalid json", 0, ""), ("null", 0, "")])
def test_failed_pnp_query_is_not_reported_as_missing_hardware(monkeypatch, output, code, error):
    mock_windows_response(monkeypatch, output, code, error)
    snapshot = diagnostics.collect_windows_usb_diagnostics()
    assert snapshot["status"] == "unavailable"
    assert "unavailable" in diagnostics.format_windows_usb_diagnostics(snapshot)
    monkeypatch.setattr(minispec, "collect_windows_usb_diagnostics", lambda: snapshot)
    message = minispec._augment_minispec_error("No driver was found")
    assert "no connected Hamamatsu" not in message


def test_pnp_timeout_remains_a_diagnostic_failure(monkeypatch):
    monkeypatch.setattr(diagnostics.sys, "platform", "win32")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("powershell.exe", 10)

    monkeypatch.setattr(diagnostics.subprocess, "run", timeout)
    assert diagnostics.collect_windows_usb_diagnostics()["status"] == "unavailable"


def test_confirmed_empty_pnp_list_has_cable_guidance(monkeypatch):
    mock_windows_response(monkeypatch, "[]")
    snapshot = diagnostics.collect_windows_usb_diagnostics()
    assert snapshot["status"] == "ok"
    assert "no connected Hamamatsu" in diagnostics.format_windows_usb_diagnostics(snapshot)


def test_c10785_diagnostic_does_not_misroute_to_hama5(monkeypatch, pnp_device):
    mock_windows_response(monkeypatch, json.dumps([pnp_device]))
    message = minispec._augment_minispec_error("No driver was found")
    assert "C10785 select MiniSpec" in message
    assert "Hama5 excludes this PID" in message
    assert "INF=oem42.inf" in message
    assert "could not upload firmware" not in message
    assert "are supported by the 'Hama5'" not in message
    assert "driver is not bound" not in message


def test_non_windows_diagnostic_never_runs_powershell(monkeypatch):
    monkeypatch.setattr(diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(diagnostics.subprocess, "run", lambda *a, **k: pytest.fail("Windows only"))
    assert diagnostics.collect_windows_usb_diagnostics()["status"] == "unavailable"
