"""DCamUSB ownership and probe cleanup without Windows hardware."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import spectrometer_loader as loader
from spectrometers import minispec_spectrometer as minispec


class FakeSDK:
    """Model the single-device session, including thread-owned initialization."""

    def __init__(self):
        self.thread = None
        self.opened = False
        self.error = 0
        self.fail = None
        self.events = []
        self.functions = {}

    def __getattr__(self, name):
        if not name.startswith("Dcam"):
            raise AttributeError(name)
        if name not in self.functions:
            self.functions[name] = Mock(side_effect=lambda *args: self.call(name, *args))
        return self.functions[name]

    def call(self, name, *args):
        self.events.append((name, threading.get_ident()))
        if name == "DcamGetLastError":
            return self.error
        if name == self.fail:
            self.error = 4
            return 0
        if name == "DcamInitialize":
            assert self.thread is None, "previous SDK session leaked"
            self.thread = threading.get_ident()
        elif name == "DcamUninitialize":
            assert self.thread == threading.get_ident(), "cleanup moved to another thread"
            self.thread = None
            self.opened = False
        elif name == "DcamOpen":
            assert self.thread is not None
            self.opened = True
        elif name == "DcamClose":
            self.opened = False
        elif name == "DcamGetDeviceState":
            args[0]._obj.value = minispec.DCAM_DEVSTATE_DEVICE
        elif name == "DcamGetDeviceInformation":
            args[1].value = b"SN123" if args[0] == minispec.DCAM_DEVINF_SERIALNO else b"MiniSpec"
        elif name == "DcamGetImageSize":
            args[0]._obj.value = 8
            args[1]._obj.value = 1
        elif name == "DcamGetBitPerPixel":
            args[0]._obj.value = 16
        elif name == "DcamGetCaptureBytes":
            args[0]._obj.value = 16
        return 1


@pytest.fixture
def sdk_env(monkeypatch, tmp_path):
    monkeypatch.setattr(minispec, "_dcamusb_owner", None)
    monkeypatch.setattr(minispec, "MiniSpec_Spectrometer_Instances", {})
    monkeypatch.setattr(minispec, "_augment_minispec_error", lambda message, *args, **kwargs: message)
    libraries = {}
    directories = []

    def add_directory(path):
        handle = SimpleNamespace(path=path, close=Mock())
        directories.append(handle)
        return handle

    monkeypatch.setattr(minispec.os, "add_dll_directory", add_directory, raising=False)
    load = Mock(side_effect=lambda path: libraries[path])
    monkeypatch.setattr(minispec, "windll", SimpleNamespace(LoadLibrary=load))

    def add_sdk(name="DCamUSB.dll"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        sdk = libraries[str(path)] = FakeSDK()
        return path, sdk

    yield SimpleNamespace(add=add_sdk, load=load, directories=directories)
    owner = minispec._dcamusb_owner
    if owner is not None:
        owner.close_spec_dll()


def test_failed_probe_releases_its_thread_session_and_retry_can_switch_dll(sdk_env):
    first_path, first = sdk_env.add("first/DCamUSB.dll")
    second_path, second = sdk_env.add("second/DCamUSB.dll")
    first.fail = "DcamGetDeviceState"

    with ThreadPoolExecutor(max_workers=1) as worker:
        with pytest.raises(RuntimeError, match="count devices: No driver was found"):
            worker.submit(loader._discover_minispec, str(first_path)).result()

    assert first.thread is None
    assert first.DcamUninitialize.call_count == 1
    first_init = next(thread for name, thread in first.events if name == "DcamInitialize")
    first_cleanup = next(thread for name, thread in first.events if name == "DcamUninitialize")
    assert first_init == first_cleanup != threading.get_ident()

    assert loader._discover_minispec(str(second_path))[0]["serial"] == "SN123"
    assert second.DcamInitialize.call_count == second.DcamUninitialize.call_count == 1
    assert minispec._dcamusb_owner is None


def test_runtime_initializes_again_after_successful_discovery(sdk_env):
    path, sdk = sdk_env.add()
    assert loader._discover_minispec(str(path))
    spec = minispec.MiniSpec_Spectrometer()
    spec.dll_path = str(path)

    assert spec.connect() == "OK"
    assert sdk.DcamInitialize.call_count == 2
    assert sdk.DcamUninitialize.call_count == 1
    assert spec.disconnect() == "OK"
    assert sdk.DcamUninitialize.call_count == 2
    assert minispec._dcamusb_owner is None


@pytest.mark.parametrize("failure", ["DcamOpen", "DcamSetExposureTime"])
def test_connection_failure_frees_owned_session_and_allows_retry(sdk_env, failure):
    path, sdk = sdk_env.add()
    spec = minispec.MiniSpec_Spectrometer()
    spec.dll_path = str(path)
    sdk.fail = failure

    assert spec.connect() == "No driver was found"
    assert sdk.thread is None and not sdk.opened
    assert spec.dll_handler is None and not spec.is_open
    assert minispec._dcamusb_owner is None
    sdk_env.directories[0].close.assert_called_once()

    sdk.fail = None
    assert spec.connect() == "OK"
    assert sdk.DcamInitialize.call_count == 2
    assert spec.disconnect() == "OK"


def test_second_instance_cannot_claim_or_release_an_active_session(sdk_env):
    path, sdk = sdk_env.add()
    active = minispec.MiniSpec_Spectrometer()
    active.dll_path = str(path)
    assert active.connect() == "OK"

    other = minispec.MiniSpec_Spectrometer()
    other.dll_path = str(path)
    assert "already in use" in other.connect()
    assert other.disconnect() == "OK"
    assert sdk.DcamInitialize.call_count == 1
    assert sdk.DcamUninitialize.call_count == 0
    assert sdk.opened and minispec._dcamusb_owner is active
    assert active.disconnect() == "OK"


def test_already_initialized_sdk_is_not_adopted_or_uninitialized(sdk_env):
    path, sdk = sdk_env.add()
    sdk.error = 3
    sdk.DcamInitialize.side_effect = None
    sdk.DcamInitialize.return_value = 0
    spec = minispec.MiniSpec_Spectrometer()
    spec.dll_path = str(path)

    assert "Already in-use" in spec.connect()
    assert sdk.DcamOpen.call_count == 0
    assert sdk.DcamUninitialize.call_count == 0
    assert minispec._dcamusb_owner is None


def test_unsuccessful_uninitialize_keeps_ownership_until_cleanup_can_retry(sdk_env):
    path, sdk = sdk_env.add()
    spec = minispec.MiniSpec_Spectrometer()
    spec.dll_path = str(path)
    assert spec.connect() == "OK"
    sdk.fail = "DcamUninitialize"

    assert spec.disconnect() == "No driver was found"
    assert minispec._dcamusb_owner is spec
    assert spec.dll_handler is sdk
    sdk_env.directories[0].close.assert_not_called()
    sdk.fail = None
    assert spec.disconnect() == "OK"
    assert minispec._dcamusb_owner is None
    sdk_env.directories[0].close.assert_called_once()


def test_relative_dll_path_and_search_directory_lifetime(sdk_env, monkeypatch, tmp_path):
    path, sdk = sdk_env.add("sdk/DCamUSB.dll")
    monkeypatch.chdir(tmp_path)
    spec = minispec.MiniSpec_Spectrometer()
    spec.dll_path = "sdk/DCamUSB.dll"

    assert spec.load_spec_dll() == "OK"
    sdk_env.load.assert_called_once_with(str(path))
    handle = sdk_env.directories[0]
    assert handle.path == str(path.parent)
    handle.close.assert_not_called()
    assert spec.initialize_dll() == "OK"
    handle.close.assert_not_called()
    assert spec.close_spec_dll() == "OK"
    handle.close.assert_called_once()
    assert sdk.thread is None


def test_dll_load_failure_closes_search_directory(sdk_env):
    path, _sdk = sdk_env.add()
    sdk_env.load.side_effect = OSError("missing dependency")
    spec = minispec.MiniSpec_Spectrometer()
    spec.dll_path = str(path)

    assert "missing dependency" in spec.load_spec_dll()
    sdk_env.directories[0].close.assert_called_once()
    assert spec.dll_handler is None
