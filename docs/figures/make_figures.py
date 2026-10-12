"""Generate the PNG figures for docs/design-document.md.

Run from the SciLab repository root:

    python docs/figures/make_figures.py

Figures that show behaviour are produced by running the real code:
  * the measurement orchestrator against the simulated spectrometer (auto-IT)
  * analysis.calculations.compute_characterization on a bench CSV fixture
  * analysis.calculations.slit_func for the slit-function family
The remaining figures chart numbers measured in this checkout (lines of code,
tests) or quoted from README.md / CHANGELOG.md (timings).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path.cwd()
assert (ROOT / "main.py").is_file(), "run from the SciLab repository root"
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

OUT = ROOT / "docs" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

# --- dataviz reference palette (light mode), validated with validate_palette.js ---
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
DEEMPH = "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
BLUE_RAMP = {100: "#cde2fb", 150: "#b7d3f6", 200: "#9ec5f4", 250: "#86b6ef", 300: "#6da7ec", 350: "#5598e7",
             400: "#3987e5", 450: "#2a78d6", 500: "#256abf", 550: "#1c5cab", 600: "#184f95", 650: "#104281",
             700: "#0d366b"}
STATUS_WARNING = "#fab219"

installed = {f.name for f in font_manager.fontManager.ttflist}
FAMILY = next((f for f in ("Segoe UI", "DejaVu Sans") if f in installed), "sans-serif")
plt.rcParams.update({
    "font.family": FAMILY,
    "font.size": 9.5,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS,
    "axes.linewidth": 0.8,
    "axes.labelcolor": INK2,
    "axes.titlecolor": INK,
    "axes.titleweight": "semibold",
    "axes.titlesize": 10.5,
    "axes.titlelocation": "left",
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelcolor": INK2,
    "ytick.labelcolor": INK2,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "grid.linestyle": "-",
    "axes.axisbelow": True,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "lines.linewidth": 2.0,
    "lines.solid_capstyle": "round",
    "lines.solid_joinstyle": "round",
    "legend.frameon": False,
    "legend.fontsize": 9,
    "legend.labelcolor": INK2,
})
DPI = 150


def save(fig, name):
    path = OUT / name
    fig.savefig(path, dpi=DPI, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    print(f"wrote {path.relative_to(ROOT)}")


def tidy(ax, x_grid=False):
    ax.grid(axis="y", visible=True)
    ax.grid(axis="x", visible=x_grid)
    ax.tick_params(length=0)


def horizontal(ax):
    """Horizontal-bar chrome: vertical hairlines only, no left spine."""
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", visible=True)
    ax.tick_params(length=0)
    ax.spines["left"].set_visible(False)


# ---------------------------------------------------------------------------
# 1. Auto-IT convergence: the real orchestrator on the simulated spectrometer
# ---------------------------------------------------------------------------

def figure_auto_it():
    from domain.measurement import MeasurementData
    from services.measurement_orchestrator import (MeasurementOrchestrator, MeasurementOrchestratorCallbacks,
                                                   MeasurementOrchestratorConfig)
    from spectrometers.simulated_spectrometer import SimulatedBench, SimulatedLaserController, SimulatedSpectrometer

    app_config = dict(
        default_start_it={"532": 20.0, "517": 80.0, "Hg_Ar": 20.0, "default": 2.4},
        target_low=60000, target_high=65000, target_mid=62500,
        it_min=0.2, it_max=3000.0, it_step_up=0.3, it_step_down=0.1,
        max_it_adjust_iters=1000, sat_thresh=65400,
        n_sig=50, n_dark=50, n_sig_640=10, n_dark_640=10,
    )
    bench = SimulatedBench()
    spec = SimulatedSpectrometer(bench=bench, time_scale=0.0, seed=1234)
    assert spec.connect() == "OK"
    spec.abort_on_saturation = False
    lasers = SimulatedLaserController(bench=bench)
    data = MeasurementData(npix=spec.npix_active, serial_number=spec.sn)
    log = []
    callbacks = MeasurementOrchestratorCallbacks(
        prepare_devices=lambda: None,
        power_lookup=lambda tag: 0.01,
        auto_it_update=lambda tag, spectrum, it_ms, peak: log.append((tag, float(it_ms), float(peak))),
        countdown=lambda *a: None,
    )
    orch = MeasurementOrchestrator(spec, lasers, data, MeasurementOrchestratorConfig(**app_config), callbacks,
                                   sleep_fn=lambda _s: None)
    tags = ["377", "405", "445", "488", "517", "532", "685", "Hg_Ar"]
    result = orch.run(tags)
    assert not result.errors, result.errors
    traces = {}
    for tag, it_ms, peak in log:
        traces.setdefault(tag, []).append((it_ms, peak))
    frames = {tag: len(v) for tag, v in traces.items()}
    print("auto-IT frames per source:", frames)

    # Emphasis form: every source in the de-emphasis gray, three in colour.
    ordered = sorted(frames, key=frames.get)
    highlight = [ordered[-1], ordered[len(ordered) // 2], ordered[1]]
    colors = dict(zip(highlight, SERIES[:3]))

    fig, (ax_it, ax_peak) = plt.subplots(2, 1, figsize=(8.6, 6.2), sharex=True,
                                         gridspec_kw={"hspace": 0.28})
    for tag in tags:
        trace = np.asarray(traces[tag])
        x = np.arange(1, len(trace) + 1)
        color = colors.get(tag, DEEMPH)
        z = 3 if tag in colors else 1
        ax_it.plot(x, trace[:, 0], color=color, zorder=z)
        ax_peak.plot(x, trace[:, 1], color=color, zorder=z)
        if tag in colors:
            label = "Hg-Ar lamp" if tag == "Hg_Ar" else f"{tag} nm"
            ax_it.plot(x[-1], trace[-1, 0], "o", color=color, markersize=7, markeredgecolor=SURFACE,
                       markeredgewidth=2, zorder=4)
            ax_it.annotate(f"{label}: {len(trace)} frames, {trace[-1, 0]:.2f} ms", (x[-1], trace[-1, 0]),
                           xytext=(8, 0), textcoords="offset points", va="center", color=INK2, fontsize=9)
            ax_peak.plot(x[-1], trace[-1, 1], "o", color=color, markersize=7, markeredgecolor=SURFACE,
                         markeredgewidth=2, zorder=4)
    ax_peak.axhspan(app_config["target_low"], app_config["target_high"], color=BLUE_RAMP[100], alpha=0.6,
                    zorder=0, lw=0)
    ax_peak.axhline(app_config["sat_thresh"], color=STATUS_WARNING, lw=1.2, zorder=0)
    xmax = max(frames.values())
    ax_peak.text(xmax * 1.4, app_config["sat_thresh"] + 900, "saturation threshold 65,400", color=INK2,
                 fontsize=8.5, va="bottom", ha="right")
    ax_peak.text(xmax * 1.4, app_config["target_mid"] + 400, "target band 60,000 to 65,000 counts", color=INK2,
                 fontsize=8.5, va="center", ha="right")
    ax_it.set_title("Auto-IT walks each source into the target band (simulated spectrometer, 8 sources)")
    ax_it.set_ylabel("integration time (ms)")
    ax_peak.set_ylabel("peak counts in frame")
    ax_peak.set_xlabel("single-frame capture number during auto-IT")
    ax_peak.set_ylim(0, 70000)
    ax_peak.set_yticks([0, 20000, 40000, 60000])
    ax_peak.set_yticklabels(["0", "20,000", "40,000", "60,000"])
    ax_it.set_xlim(0, xmax * 1.42)
    for ax in (ax_it, ax_peak):
        tidy(ax)
    fig.text(0.0, -0.01, "Grey traces: the other five sources. Steps are additive (<= 0.3 ms up, <= 0.1 ms down "
             "per frame); a saturated frame backs off by 30 %.", color=MUTED, fontsize=8.5)
    save(fig, "fig-auto-it-convergence.png")
    return frames


# ---------------------------------------------------------------------------
# 2. Analysis pipeline on a real bench CSV (tests/fixtures/meas_4laser.csv)
# ---------------------------------------------------------------------------

def figure_analysis_example():
    import pandas as pd

    from analysis.calculations import compute_characterization

    csv = ROOT / "tests" / "fixtures" / "meas_4laser.csv"
    df = pd.read_csv(csv)
    comp = compute_characterization(df, sn="bench-4-laser")
    print("analysis summary:", str(comp.summary_lines).encode("ascii", "replace").decode())
    metrics = comp.metrics

    fig, axes = plt.subplots(1, 3, figsize=(12.5, 3.9), gridspec_kw={"wspace": 0.32})
    ax_lsf, ax_disp, ax_res = axes

    for i, sample in enumerate(comp.laser_lsfs):
        color = SERIES[i]
        ax_lsf.plot(np.arange(len(sample.values)), sample.values, color=color, lw=1.6)
        # Stagger the labels so neighbouring peaks do not overprint each other.
        ax_lsf.annotate(f"{sample.wavelength_nm:.0f} nm", (sample.pixel_index, 1.0),
                        xytext=(0, 6 + 13 * (i % 2)), textcoords="offset points", ha="center", color=INK2,
                        fontsize=8.5)
    ax_lsf.set_yscale("log")
    ax_lsf.set_ylim(1e-5, 12)
    ax_lsf.set_xlim(0, comp.pixel_count)
    ax_lsf.set_title("Normalized line-spread functions")
    ax_lsf.set_xlabel("pixel")
    ax_lsf.set_ylabel("dark-corrected counts / peak")

    ax_disp.plot(comp.dispersion_curve_pixels, comp.dispersion_curve_wavelengths_nm, color=DEEMPH, lw=2, zorder=1)
    ax_disp.plot(comp.dispersion_fit_pixels, comp.dispersion_fit_wavelengths_nm, "o", color=SERIES[0],
                 markersize=8, markeredgecolor=SURFACE, markeredgewidth=2, zorder=3)
    for px, wl in zip(comp.dispersion_fit_pixels, comp.dispersion_fit_wavelengths_nm):
        ax_disp.annotate(f"{wl:.0f} nm at px {int(px)}", (px, wl), xytext=(9, -3), textcoords="offset points",
                         color=INK2, fontsize=8.5)
    coeffs = metrics.dispersion_coefficients
    ax_disp.set_title("Dispersion fit, degree-2 polynomial")
    ax_disp.set_xlabel("pixel")
    ax_disp.set_ylabel("wavelength (nm)")
    ax_disp.set_xlim(0, comp.pixel_count)
    ax_disp.text(0.03, 0.95, f"lambda(p) = {coeffs[0]:.2e} p^2 + {coeffs[1]:.4f} p + {coeffs[2]:.1f}",
                 transform=ax_disp.transAxes, va="top", color=INK2, fontsize=8.5)

    wl = metrics.spectral_resolution_wavelengths_nm
    fwhm = metrics.spectral_resolution_fwhm_nm
    ax_res.plot(wl, fwhm, color=SERIES[0])
    ax_res.fill_between(wl, 0, fwhm, color=SERIES[0], alpha=0.10, lw=0)
    for s in metrics.slit_parameter_samples:
        w = 2 * abs(s.a2) * np.log(2) ** (1.0 / s.a3)
        ax_res.plot(s.wavelength_nm, w, "o", color=SERIES[0], markersize=8, markeredgecolor=SURFACE,
                    markeredgewidth=2, zorder=3)
    ax_res.set_title("Spectral resolution from the slit-function fits")
    ax_res.set_xlabel("wavelength (nm)")
    ax_res.set_ylabel("FWHM (nm)")
    ax_res.set_ylim(0, max(np.nanmax(fwhm) * 1.3, 0.1))
    ax_res.legend(handles=[Line2D([], [], color=SERIES[0], label="polynomial in a2(lambda), a3(lambda)"),
                           Line2D([], [], marker="o", color=SERIES[0], lw=0, markersize=8,
                                  label="per-line fit")], loc="lower right")

    for ax in axes:
        tidy(ax)
    fig.text(0.0, -0.04, "Computed by analysis.calculations.compute_characterization on tests/fixtures/"
             "meas_4laser.csv (a July 2026 bench run; 377, 405, 445 and 488 nm lasers, 2048 pixels).",
             color=MUTED, fontsize=8.5)
    save(fig, "fig-analysis-example.png")
    return comp


# ---------------------------------------------------------------------------
# 3. Slit-function family used by the analysis
# ---------------------------------------------------------------------------

def figure_slit_family():
    from analysis.calculations import compute_fwhm, slit_func

    x = np.linspace(-2.5, 2.5, 1001)
    a2 = 0.6
    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    anchors = (0.82, 0.62, 0.42, 0.22)   # one label height per curve, on the right flank
    for i, a3 in enumerate((1.0, 2.0, 4.0, 8.0)):
        y = slit_func(x, a2, a3, 0.0)
        color = SERIES[i]
        ax.plot(x, y, color=color)
        fwhm = compute_fwhm(x, y)
        y_anchor = anchors[i]
        x_anchor = a2 * (-np.log(y_anchor)) ** (1.0 / a3)
        ax.annotate(f"a3 = {a3:g}   FWHM {fwhm:.2f} nm", (x_anchor, y_anchor), xytext=(1.45, y_anchor),
                    textcoords="data", va="center", color=INK2, fontsize=8.5,
                    arrowprops=dict(arrowstyle="-", color=AXIS, lw=0.8))
    ax.set_title("Slit-function model exp(-|x / a2|^a3), a2 = 0.6 nm")
    ax.set_xlabel("offset from line centre (nm)")
    ax.set_ylabel("normalized intensity")
    ax.set_ylim(0, 1.15)
    ax.set_xlim(-2.5, 2.5)
    tidy(ax)
    fig.text(0.0, -0.03, "a3 = 2 is a Gaussian; larger exponents flatten the top. FWHM = 2 a2 (ln 2)^(1/a3), "
             "the relation the resolution curve uses.", color=MUTED, fontsize=8.5)
    save(fig, "fig-slit-function-family.png")


# ---------------------------------------------------------------------------
# 4. Startup timeline, v3.3 vs v3.4 (seconds, from README.md / main.py)
# ---------------------------------------------------------------------------

def figure_startup_timeline():
    # Colour follows the phase (ordinal blue ramp), not the row.
    phase_color = {
        "Splash": BLUE_RAMP[250],
        "Imports (numpy, matplotlib, tabs)": BLUE_RAMP[350],
        "Window build": BLUE_RAMP[550],
        "pandas + scipy preload": BLUE_RAMP[150],
    }
    # (row label, [(phase, start, duration)], time the window becomes usable)
    rows = [
        ("v3.3\nmain thread", [("Imports (numpy, matplotlib, tabs)", 0.0, 2.5), ("Splash", 2.5, 2.5),
                               ("Window build", 5.0, 0.5)], 5.5),
        ("v3.4\nmain thread", [("Splash", 0.0, 0.1), ("Imports (numpy, matplotlib, tabs)", 0.1, 1.0),
                               ("Window build", 1.1, 0.4)], 1.5),
        ("v3.4\nworker thread", [("pandas + scipy preload", 3.5, 1.4)], None),
    ]
    fig, ax = plt.subplots(figsize=(9.0, 3.3))
    gap = 0.03
    for yi, (label, phases, usable) in enumerate(reversed(rows)):
        for name, start, dur in phases:
            ax.barh(yi, dur - gap, left=start + gap / 2, height=0.5, color=phase_color[name], lw=0)
            if dur >= 2.0:
                ax.text(start + dur / 2, yi, name, ha="center", va="center", fontsize=8.5, color=INK)
        if usable is not None:
            ax.plot(usable, yi, "o", color=SERIES[0], markersize=8, markeredgecolor=SURFACE, markeredgewidth=2,
                    zorder=4)
            ax.annotate(f"window usable at about {usable:.1f} s", (usable, yi), xytext=(8, 0),
                        textcoords="offset points", va="center", color=INK2, fontsize=8.5)
    ax.text(3.5, 0.42, "pandas + scipy preload, 2 s after the window is up", color=INK2, fontsize=8.5,
            va="bottom")
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in reversed(rows)])
    ax.set_xlim(0, 7)
    ax.set_ylim(-0.6, 2.9)
    ax.set_xlabel("seconds after launch")
    ax.set_title("Startup: what runs when, v3.3 vs v3.4")
    horizontal(ax)
    ax.legend(handles=[Patch(color=c, label=n) for n, c in phase_color.items()], loc="upper right", ncol=2,
              bbox_to_anchor=(1.0, 1.02))
    fig.text(0.0, -0.06, "Approximate timings from README.md and main.py. The v3.4 import step runs behind the "
             "splash, so the screen is never blank; the analysis stack loads later on a daemon thread.",
             color=MUTED, fontsize=8.5)
    save(fig, "fig-startup-timeline.png")


# ---------------------------------------------------------------------------
# 5. v3.3 -> v3.4 timings as a dumbbell on a log axis
# ---------------------------------------------------------------------------

def figure_performance_dumbbell():
    items = [
        ("Launch to usable window", 5.0, 1.5),
        ("Full analysis (compute + 8 PNGs)", 4.7, 1.4),
        ("import app (startup import set)", 2.5, 0.2),
        ("compute_characterization, 4-laser run", 0.29, 0.04),
    ]
    fig, ax = plt.subplots(figsize=(8.6, 3.2))
    for yi, (label, before, after) in enumerate(reversed(items)):
        ax.plot([after, before], [yi, yi], color=DEEMPH, lw=2, zorder=1, solid_capstyle="butt")
        ax.plot(before, yi, "o", color=BLUE_RAMP[250], markersize=9, markeredgecolor=SURFACE, markeredgewidth=2,
                zorder=3)
        ax.plot(after, yi, "o", color=BLUE_RAMP[550], markersize=9, markeredgecolor=SURFACE, markeredgewidth=2,
                zorder=3)
        ax.annotate(f"{before:g} s", (before, yi), xytext=(9, 0), textcoords="offset points", va="center",
                    color=INK2, fontsize=8.5)
        ax.annotate(f"{after:g} s", (after, yi), xytext=(-9, 0), textcoords="offset points", va="center",
                    ha="right", color=INK2, fontsize=8.5)
    ax.set_xscale("log")
    ax.set_xlim(0.02, 12)
    ax.set_xticks([0.03, 0.1, 0.3, 1, 3, 10])
    ax.set_xticklabels(["0.03", "0.1", "0.3", "1", "3", "10"])
    ax.set_yticks(range(len(items)))
    ax.set_yticklabels([i[0] for i in reversed(items)])
    ax.set_xlabel("seconds (log scale)")
    ax.set_title("Time per operation, v3.3 (light) to v3.4 (dark)")
    horizontal(ax)
    ax.legend(handles=[Line2D([], [], marker="o", color=BLUE_RAMP[250], lw=0, markersize=8, label="v3.3"),
                       Line2D([], [], marker="o", color=BLUE_RAMP[550], lw=0, markersize=8, label="v3.4")],
              loc="lower right")
    fig.text(0.0, -0.06, "Values as recorded in README.md (v3.4 comparison table) and the v3.4 design "
             "revision notes; launch time is approximate.", color=MUTED, fontsize=8.5)
    save(fig, "fig-performance-v33-v34.png")


# ---------------------------------------------------------------------------
# 6. Codebase composition and test distribution (measured in this checkout)
# ---------------------------------------------------------------------------

def _count_lines(path: Path) -> int:
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return sum(1 for _ in fh)


def figure_codebase():
    def py_files(folder):
        return [p for p in (ROOT / folder).glob("*.py")]

    layers = {
        "spectrometers/ (10 backends)": py_files("spectrometers"),
        "tabs/ (Tk views, theme, pump)": py_files("tabs"),
        "tests/": py_files("tests"),
        "main.py + app.py": [ROOT / "main.py", ROOT / "app.py", ROOT / "scilab_version.py"],
        "services/": py_files("services"),
        "analysis/": py_files("analysis"),
        "hardware/ + stage/": py_files("hardware") + py_files("stage"),
        "tools/ + packaging/": py_files("tools") + [ROOT / "packaging" / "build.py"],
        "spectrometer_loader.py": [ROOT / "spectrometer_loader.py"],
        "domain/": py_files("domain"),
    }
    loc = {k: sum(_count_lines(p) for p in v) for k, v in layers.items()}

    test_areas = {
        "Vendor backends (6 files)":
            ["test_hama5", "test_hama6", "test_jeti_backend", "test_oceandirect_backend",
             "test_minispec_lifecycle", "test_hamamatsu_diagnostics"],
        "Measurement run + progress": ["test_orchestrator", "test_run_progress"],
        "Analysis (pure + golden)": ["test_analysis_pure", "test_analysis_golden"],
        "GUI smoke (Tk)": ["test_smoke_gui"],
        "UI pump + guard": ["test_ui_pump"],
        "Loader (Auto discovery)": ["test_loader"],
        "Driver helpers (spec_xfus)": ["test_spec_xfus"],
        "Startup import set": ["test_startup_imports"],
    }
    tests = {}
    for area, files in test_areas.items():
        n = 0
        for stem in files:
            src = (ROOT / "tests" / f"{stem}.py").read_text(encoding="utf-8")
            n += len(re.findall(r"^\s*def test_", src, re.M))
        tests[area] = n
    print("LOC:", loc)
    print("tests:", tests, "total", sum(tests.values()))

    fig, (ax_loc, ax_t) = plt.subplots(1, 2, figsize=(12.5, 4.0), gridspec_kw={"wspace": 0.55})
    for ax, series, title, unit in ((ax_loc, loc, "Lines of Python per layer", "lines"),
                                    (ax_t, tests, "Test functions per area", "test functions")):
        names = list(series)[::-1]
        values = [series[n] for n in names]
        ax.barh(names, values, height=0.55, color=SERIES[0], lw=0)
        for yi, v in enumerate(values):
            ax.annotate(f"{v:,}", (v, yi), xytext=(5, 0), textcoords="offset points", va="center", color=INK2,
                        fontsize=8.5)
        ax.set_title(title)
        ax.set_xlabel(unit)
        horizontal(ax)
        ax.set_xlim(0, max(values) * 1.22)
    fig.text(0.0, -0.04, "Counted in this checkout (v3.4.4.0). The bars count test functions (176); pytest "
             "collects 187 cases after parametrisation: 177 run without a display, 10 need Tk.",
             color=MUTED, fontsize=8.5)
    save(fig, "fig-codebase-and-tests.png")
    return loc, tests


if __name__ == "__main__":
    figure_auto_it()
    figure_analysis_example()
    figure_slit_family()
    figure_startup_timeline()
    figure_performance_dumbbell()
    figure_codebase()
    print("done")
