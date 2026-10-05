"""Startup must not import the analysis stack.

pandas and scipy together take ~1.4 s to import.  Nothing visible at startup
needs them (they are preloaded in the background afterwards, see
``main.preload_analysis_stack_later``), so importing the app and every tab
module must leave them out of ``sys.modules``.  Run in a subprocess so the
test's own imports cannot mask a regression.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

STARTUP_MODULES = (
    "app",
    "spectrometer_loader",
    "domain.measurement",
    "tabs.setup_tab",
    "tabs.live_view_tab",
    "tabs.measurements_tab",
    "tabs.analysis_tab",
    "tabs.check_resolution_tab",
    "tabs.eeprom_tab",
)

PROBE = f"""
import logging, sys
logging.disable(logging.CRITICAL)
for name in {STARTUP_MODULES!r}:
    __import__(name)
heavy = sorted({{m.split('.')[0] for m in sys.modules if m.split('.')[0] in ('pandas', 'scipy')}})
print("HEAVY=" + ",".join(heavy))
"""


def test_startup_modules_do_not_import_pandas_or_scipy():
    pytest.importorskip("_tkinter")
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    line = [ln for ln in result.stdout.splitlines() if ln.startswith("HEAVY=")]
    assert line, result.stdout
    heavy = line[-1][len("HEAVY="):]
    assert heavy == "", f"startup path imported: {heavy}"


def test_analysis_stack_preload_module_imports_pandas_and_scipy():
    pytest.importorskip("pandas")
    pytest.importorskip("scipy")
    probe = "import sys, services.analysis_stack_preload; print('pandas' in sys.modules and 'scipy' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("True")
