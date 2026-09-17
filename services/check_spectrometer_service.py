"""Check Spectrometer service.

Implements the routine described in ``CheckSpectrometer_Specification.md``:
auto-expose, take a 3 s lamp measurement, dark-correct using blind pixels,
window the brightest line, fit a modified (super-)Gaussian, and produce a
diagnostic plot plus data dump.

Model used (Section 2 of the spec):
    f(x) = B + A * exp( -0.5 * |(x - xcen) / w|^n )

with ``n`` the shape exponent (n=2 pure Gaussian, n>2 flatter top).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

LOGGER = logging.getLogger(__name__)

# Spec defaults (Section 1.2)
_HALF_WIN = 40              # dpix
_AUTO_IT_TARGET_FRAC = 0.60 # 60% of full-scale
_AUTO_IT_BAND = (0.50, 0.70)
_AUTO_IT_MAX_ITERS = 12
_INITIAL_RESOL = 5.0        # initial width guess (pixels)
_MEASURE_IT_MS = 3000.0     # final acquisition integration time

# Fraction of the window used to estimate background (each side).
_BG_FRACTION = 0.10


# ---------------------------------------------------------------------------
# Modified Gaussian model
# ---------------------------------------------------------------------------

def _mgauss(x: np.ndarray, c: float, amp: float, w: float, n: float,
            bg: float) -> np.ndarray:
    """B + A * exp(-0.5 * |(x - c)/w|^n)."""
    w = max(abs(w), 1e-6)
    n = max(abs(n), 0.1)
    return bg + amp * np.exp(-0.5 * np.abs((x - c) / w) ** n)


def _fit_peak(
    xi: np.ndarray,
    yi: np.ndarray,
    uyi: Optional[np.ndarray],
    indm: int,
) -> Tuple[int, np.ndarray, float, float, List[np.ndarray], List[np.ndarray]]:
    """Fit the modified Gaussian to the windowed peak.

    Returns
    -------
    err       : 0 converged, 1 max_fev, 2 max_iter
    a         : [c_offset_from_indm, A, w, n, B]
    rms       : RMS residual of the fit
    resolfit  : fitted width w (pixels)
    xxi/yyi   : 4-element list of classification arrays (relative to indm),
                see Section 3.5 of the spec.
    """
    try:
        from scipy.optimize import curve_fit  # type: ignore
    except ImportError as exc:
        raise RuntimeError("scipy is required for Check Spectrometer fit") from exc

    x_rel = xi.astype(float) - float(indm)
    y = yi.astype(float)
    n_pts = len(x_rel)

    n_bg_side = max(2, int(round(n_pts * _BG_FRACTION)))
    n_bg_side = min(n_bg_side, n_pts // 3)
    bg_mask = np.zeros(n_pts, dtype=bool)
    bg_mask[:n_bg_side] = True
    bg_mask[-n_bg_side:] = True

    excl_mask = np.zeros(n_pts, dtype=bool)
    fit_mask = ~bg_mask & ~excl_mask

    bg0 = float(np.mean(y[bg_mask])) if np.any(bg_mask) else float(np.min(y))
    amp0 = float(np.max(y) - bg0)
    if amp0 <= 0:
        amp0 = max(float(np.max(y)), 1.0)

    p0 = [0.0, amp0, _INITIAL_RESOL, 2.0, bg0]
    half_span = float(x_rel.max() - x_rel.min()) / 2.0
    bounds_lo = [-half_span, 0.0, 0.5, 0.5, -abs(bg0) - amp0]
    bounds_hi = [+half_span, amp0 * 5.0 + 1.0, max(half_span, 10.0), 12.0,
                 abs(bg0) + amp0]

    sigma = None
    if uyi is not None:
        s = np.asarray(uyi, dtype=float)
        if np.all(np.isfinite(s)) and np.all(s > 0):
            sigma = s

    err = 0
    try:
        popt, _ = curve_fit(
            _mgauss, x_rel, y, p0=p0, sigma=sigma, absolute_sigma=False,
            bounds=(bounds_lo, bounds_hi), maxfev=5000,
        )
    except RuntimeError:
        err = 1
        popt = np.array(p0, dtype=float)
    except Exception:
        err = 2
        popt = np.array(p0, dtype=float)

    a = np.asarray(popt, dtype=float)
    y_fit = _mgauss(x_rel, *a)
    rms = float(np.sqrt(np.mean((y - y_fit) ** 2)))
    # Reported width is 2 × the fitted half-width parameter `w`.
    resolfit = 2.0 * float(abs(a[2]))

    xxi = [
        x_rel[excl_mask],
        x_rel[bg_mask],
        x_rel[fit_mask],
        np.linspace(x_rel.min(), x_rel.max(), 200),
    ]
    yyi = [
        y[excl_mask],
        y[bg_mask],
        y[fit_mask],
        _mgauss(xxi[3], *a),
    ]
    return err, a, rms, resolfit, xxi, yyi


# ---------------------------------------------------------------------------
# Public result
# ---------------------------------------------------------------------------

@dataclass
class CheckSpectrometerResult:
    xcen: float
    resolfit: float
    shape_exponent: float
    rms: float
    fit_err: int
    auto_it_ms: float
    plot_path: str
    csv_path: str
    ddf_path: str = ""
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class CheckSpectrometerService:
    """Run the Check Spectrometer routine on a connected spectrometer."""

    def __init__(self, output_dir: Path, instrument_name: str = "",
                 location: str = ""):
        self.output_dir = Path(output_dir)
        self.instrument_name = instrument_name
        self.location = location

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, spec) -> CheckSpectrometerResult:
        """Execute the full routine. ``spec`` must expose:
        ``rcm``, ``rcs``, ``npix_active``, ``eff_saturation_limit``,
        ``npix_blind_left``/``npix_blind_right`` (optional), and
        ``set_it(ms)`` / ``measure(ncy)`` methods.
        """
        warnings: List[str] = []
        sat_limit = float(getattr(spec, "eff_saturation_limit", 65535))

        # Step 1 — auto-expose to ~60% of full-scale (no dark subtraction)
        auto_it_ms = self._auto_it(spec, sat_limit, warnings)

        # Step 2 — final acquisition: 3 s, single repetition
        spec.set_it(_MEASURE_IT_MS)
        result = spec.measure(ncy=1)
        if result != "OK":
            LOGGER.warning("Check spectrometer measure returned: %s", result)

        signal_mean = np.asarray(spec.rcm, dtype=float)
        signal_std = np.asarray(spec.rcs, dtype=float)
        if signal_mean.size == 0:
            raise RuntimeError("Spectrometer returned an empty spectrum")

        if np.any(signal_mean >= sat_limit):
            warnings.append("WARNING: SATURATION — one or more pixels at full scale.")
            LOGGER.warning("Saturation detected during check spectrometer")

        # Step 3 — dark correction via blind pixels
        signal, uncert, dark_level = self._apply_blind_correction(
            spec, signal_mean, signal_std,
        )

        # Step 4 — window around brightest pixel
        n_total = len(signal)
        indm = int(np.argmax(signal))
        ind1 = max(0, indm - _HALF_WIN)
        ind2 = min(n_total, indm + _HALF_WIN + 1)
        xi = np.arange(ind1, ind2)
        yi = signal[ind1:ind2]
        uyi = uncert[ind1:ind2] if uncert.size >= n_total else None

        # Step 5 — fit modified Gaussian
        fit_err, a, rms, resolfit, xxi, yyi = _fit_peak(xi, yi, uyi, indm)
        xcen = indm + float(a[0])

        if fit_err == 1:
            warnings.append("NOTE: fit hit max function evaluations.")
        elif fit_err == 2:
            warnings.append("NOTE: fit hit max iterations.")

        # Step 6 — display normalization (peak-after-bg = 1.0)
        indbg = (xxi[1] + indm).astype(int)
        indbg = indbg[(indbg >= 0) & (indbg < n_total)]
        bg = float(np.mean(signal[indbg])) if indbg.size else 0.0
        peak_above_bg = float(signal[indm] - bg)
        if peak_above_bg <= 0:
            peak_above_bg = max(float(np.max(signal) - bg), 1.0)
        norm = (signal - bg) / peak_above_bg

        # Build plot + persist
        now_local = datetime.now()
        ts_human = now_local.strftime("%Y-%m-%d %H:%M:%S")
        ts_compact = now_local.strftime("%Y%m%dT%H%M%S")
        sn = str(getattr(spec, "sn", "Unknown")).strip() or "Unknown"

        plot_path, ddf_path = self._build_plot_and_dump(
            signal=signal, norm=norm, indm=indm, xxi=xxi, yyi=yyi,
            a=a, rms=rms, resolfit=resolfit, xcen=xcen, fit_err=fit_err,
            sn=sn, ts_human=ts_human, ts_compact=ts_compact,
            bg=bg, peak_above_bg=peak_above_bg,
        )

        csv_path = self._save_csv(
            signal=signal, indm=indm, ind1=ind1, ind2=ind2,
            a=a, rms=rms, resolfit=resolfit, xcen=xcen,
            auto_it_ms=auto_it_ms, dark_level=dark_level,
            sn=sn, ts_compact=ts_compact,
        )

        return CheckSpectrometerResult(
            xcen=xcen,
            resolfit=resolfit,
            shape_exponent=float(a[3]),
            rms=rms,
            fit_err=fit_err,
            auto_it_ms=auto_it_ms,
            plot_path=str(plot_path),
            csv_path=str(csv_path),
            ddf_path=str(ddf_path),
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Step 1 — auto-IT to ~60% saturation, no dark correction
    # ------------------------------------------------------------------

    def _auto_it(self, spec, sat_limit: float, warnings: List[str]) -> float:
        target = _AUTO_IT_TARGET_FRAC * sat_limit
        band_lo = _AUTO_IT_BAND[0] * sat_limit
        band_hi = _AUTO_IT_BAND[1] * sat_limit

        n_blind_r = int(getattr(spec, "npix_blind_right", 0) or 0)
        n_blind_l = int(getattr(spec, "npix_blind_left", 0) or 0)

        current_it = float(getattr(spec, "it_ms", 10.0)) or 10.0

        for _ in range(_AUTO_IT_MAX_ITERS):
            spec.set_it(current_it)
            res = spec.measure(ncy=1)
            if res != "OK":
                break

            rcm = np.asarray(spec.rcm, dtype=float)
            if rcm.size == 0:
                break
            active = rcm
            if n_blind_r > 0:
                active = active[:-n_blind_r]
            if n_blind_l > 0 and active.size > n_blind_l:
                active = active[n_blind_l:]
            peak = float(np.max(active)) if active.size else 0.0

            if peak <= 0:
                current_it = min(current_it * 2.0, _MEASURE_IT_MS)
                continue

            if band_lo <= peak <= band_hi:
                break

            new_it = current_it * (target / peak)
            new_it = max(0.2, min(new_it, _MEASURE_IT_MS))
            if abs(new_it - current_it) / max(current_it, 1e-6) < 0.02:
                current_it = new_it
                break
            current_it = new_it
        else:
            warnings.append("NOTE: auto-IT did not settle within iteration limit.")

        spec.set_it(current_it)
        LOGGER.info("Check spectrometer auto-IT settled at %.2f ms", current_it)
        return current_it

    # ------------------------------------------------------------------
    # Step 3 — blind-pixel dark correction (Section 3.3)
    # ------------------------------------------------------------------

    def _apply_blind_correction(
        self, spec, signal_mean: np.ndarray, signal_std: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray, float]:
        n_blind_r = int(getattr(spec, "npix_blind_right", 0) or 0)
        n_blind_l = int(getattr(spec, "npix_blind_left", 0) or 0)

        dark_level = 0.0
        sig, unc = signal_mean, signal_std

        if n_blind_r > 0:
            dark_level = float(np.max(sig[-n_blind_r:]))
            sig = sig[:-n_blind_r] - dark_level
            unc = unc[:-n_blind_r] if unc.size >= sig.size + n_blind_r else unc
            LOGGER.info("Dark correction from %d right blind pixels: %.2f",
                        n_blind_r, dark_level)
        elif n_blind_l > 0:
            dark_level = float(np.max(sig[:n_blind_l]))
            sig = sig[n_blind_l:] - dark_level
            unc = unc[n_blind_l:] if unc.size >= sig.size + n_blind_l else unc
            LOGGER.info("Dark correction from %d left blind pixels: %.2f",
                        n_blind_l, dark_level)

        return sig, unc, dark_level

    # ------------------------------------------------------------------
    # Plot (Section 4) + DDF data dump
    # ------------------------------------------------------------------

    def _build_plot_and_dump(
        self,
        signal: np.ndarray,
        norm: np.ndarray,
        indm: int,
        xxi: List[np.ndarray],
        yyi: List[np.ndarray],
        a: np.ndarray,
        rms: float,
        resolfit: float,
        xcen: float,
        fit_err: int,
        sn: str,
        ts_human: str,
        ts_compact: str,
        bg: float,
        peak_above_bg: float,
    ) -> Tuple[Path, Path]:
        from matplotlib.figure import Figure

        n_total = len(signal)
        pix = np.arange(n_total)

        def _norm_y(y: np.ndarray) -> np.ndarray:
            return (np.asarray(y, dtype=float) - bg) / peak_above_bg

        x0_abs = xxi[0] + indm  # excluded
        y0_norm = _norm_y(yyi[0])
        x1_abs = xxi[1] + indm  # background
        y1_norm = _norm_y(yyi[1])
        x2_abs = xxi[2] + indm  # used in fit
        y2_norm = _norm_y(yyi[2])
        x3_abs = xxi[3] + indm  # dense fit curve
        y3_norm = _norm_y(yyi[3])

        # Section 3.6 legend label
        label_fit = (
            f"CEN={xcen:.2f}, w={resolfit:.2f}, n={float(a[3]):.2f}, "
            f"rms={rms:.4f}"
        )
        if fit_err == 1:
            label_fit += ", >maxfun!"
        elif fit_err == 2:
            label_fit += ", >maxiter!"

        # Section 4.1 colors
        C_FIT, C_SIG, C_NOTUSED, C_FITDATA, C_BG = (
            "red", "#555555", "lightblue", "blue", "black",
        )

        fig = Figure(figsize=(14, 7))
        ax = fig.add_subplot(111)
        ax.set_facecolor("white")
        fig.patch.set_facecolor("white")
        ax.grid(True, linestyle="--", linewidth=0.7, color="#cccccc",
                alpha=0.9, zorder=0)

        # Series 2 — full normalized spectrum
        ax.plot(pix, norm, color=C_SIG, lw=0.6, label="SIGNAL", zorder=1)

        # Series 5 — background dots
        if x1_abs.size:
            ax.plot(x1_abs, y1_norm, ".", color=C_BG, ms=6,
                    label="BACKGROUND DATA", zorder=2)

        # Series 3 — not-used dots (light blue, large)
        if x0_abs.size:
            ax.plot(x0_abs, y0_norm, ".", color=C_NOTUSED, ms=12,
                    label="DATA NOT USED FOR FITTING", zorder=3)

        # Series 4 — fitting dots (blue, small)
        if x2_abs.size:
            ax.plot(x2_abs, y2_norm, ".", color=C_FITDATA, ms=4,
                    label="FITTING DATA", zorder=4)

        # Series 1 — fit curve
        ax.plot(x3_abs, y3_norm, "-", color=C_FIT, lw=1.6,
                label=label_fit, zorder=5)

        ax.set_xlabel("PIXEL", fontsize=13, fontweight="bold")
        ax.set_ylabel("NORMALIZED SIGNAL", fontsize=13, fontweight="bold")

        loc = self.location or ""
        instr = self.instrument_name or sn
        title_l1 = (
            f"{instr} at {loc}, routine CheckSpectrometer, {ts_human}"
            if loc else
            f"{instr}, routine CheckSpectrometer, {ts_human}"
        )
        ax.set_title(f"{title_l1}\nFitting strongest line", fontsize=11)

        # Section 4.6 legend — order: fit, signal, not-used, fitting, background
        legend_specs = [
            (label_fit, C_FIT),
            ("SIGNAL", C_SIG),
            ("DATA NOT USED FOR FITTING", C_NOTUSED),
            ("FITTING DATA", C_FITDATA),
            ("BACKGROUND DATA", C_BG),
        ]
        handles_by_label = {h.get_label(): h for h in ax.get_lines()}
        ord_h, ord_l, ord_c = [], [], []
        for lbl, color in legend_specs:
            h = handles_by_label.get(lbl)
            if h is not None:
                ord_h.append(h); ord_l.append(lbl); ord_c.append(color)
        leg = ax.legend(
            ord_h, ord_l,
            loc="upper left",
            frameon=False, fontsize=9, labelspacing=0.2,
        )
        for text, color in zip(leg.get_texts(), ord_c):
            text.set_color(color)
            text.set_fontweight("bold")

        # Two-pass axis limits (Section 4.5)
        all_x_parts = [x3_abs, pix]
        all_y_parts = [y3_norm, norm]
        for xs, ys in ((x0_abs, y0_norm), (x1_abs, y1_norm),
                       (x2_abs, y2_norm)):
            if xs.size:
                all_x_parts.append(xs); all_y_parts.append(ys)
        all_x = np.concatenate(all_x_parts)
        all_y = np.concatenate(all_y_parts)

        def _limits(arr: np.ndarray, margin: float) -> Tuple[float, float]:
            lo = float(np.nanmin(arr)); hi = float(np.nanmax(arr))
            span = hi - lo if hi > lo else max(abs(hi), 1.0)
            return lo - margin * span, hi + margin * span

        # Pass 1 — wide "home" view
        ax.set_xlim(*_limits(all_x, 0.01))
        ax.set_ylim(*_limits(all_y, 0.05))

        # Pass 2 — narrow default view from data-dot series only
        narrow_x_parts, narrow_y_parts = [], []
        for xs, ys in ((x0_abs, y0_norm), (x1_abs, y1_norm),
                       (x2_abs, y2_norm)):
            if xs.size:
                narrow_x_parts.append(xs); narrow_y_parts.append(ys)
        if narrow_x_parts:
            nx = np.concatenate(narrow_x_parts)
            ny = np.concatenate(narrow_y_parts)
            ax.set_xlim(*_limits(nx, 0.04))
            ax.set_ylim(*_limits(ny, 0.08))

        fig.tight_layout()

        # Section 4.7 saving
        self.output_dir.mkdir(parents=True, exist_ok=True)
        loc_tag = (loc or "site").replace(" ", "")
        base_name = f"{sn}_{loc_tag}_{ts_compact}_CheckSpectrometer"

        plot_path = self.output_dir / f"{base_name}.jpg"
        fig.savefig(plot_path, dpi=150, bbox_inches="tight",
                    format="jpeg", pil_kwargs={"quality": 92})
        LOGGER.info("Check spectrometer plot saved: %s", plot_path)

        ddf_path = self.output_dir / f"{base_name}.ddf"
        self._save_ddf(
            ddf_path, sn=sn, ts_human=ts_human, indm=indm,
            xcen=xcen, resolfit=resolfit, a=a, rms=rms, fit_err=fit_err,
            pix=pix, norm=norm,
            x_curve=x3_abs, y_curve=y3_norm,
            x_fitpts=x2_abs, y_fitpts=y2_norm,
            x_bgpts=x1_abs, y_bgpts=y1_norm,
            x_excluded=x0_abs, y_excluded=y0_norm,
        )
        return plot_path, ddf_path

    # ------------------------------------------------------------------
    # DDF — native data dump so the plot can be re-rendered later
    # ------------------------------------------------------------------

    def _save_ddf(self, path: Path, **kw) -> None:
        a = kw["a"]
        lines: List[str] = [
            "# CheckSpectrometer data dump",
            f"# instrument_sn: {kw['sn']}",
            f"# timestamp: {kw['ts_human']}",
            f"# indm: {kw['indm']}",
            f"# xcen: {kw['xcen']:.6f}",
            f"# resolfit: {kw['resolfit']:.6f}",
            f"# shape_n: {float(a[3]):.6f}",
            f"# amplitude: {float(a[1]):.6f}",
            f"# background: {float(a[4]):.6f}",
            f"# rms: {kw['rms']:.6f}",
            f"# fit_err: {kw['fit_err']}",
            "",
        ]

        def _block(name: str, x: np.ndarray, y: np.ndarray) -> None:
            lines.append(f"[{name}] n={len(x)}")
            for xv, yv in zip(np.asarray(x).ravel(), np.asarray(y).ravel()):
                lines.append(f"{float(xv):.6f}\t{float(yv):.6f}")
            lines.append("")

        _block("SIGNAL", kw["pix"], kw["norm"])
        _block("FIT_CURVE", kw["x_curve"], kw["y_curve"])
        _block("FIT_POINTS", kw["x_fitpts"], kw["y_fitpts"])
        _block("BACKGROUND_POINTS", kw["x_bgpts"], kw["y_bgpts"])
        _block("EXCLUDED_POINTS", kw["x_excluded"], kw["y_excluded"])

        path.write_text("\n".join(lines), encoding="utf-8")
        LOGGER.info("Check spectrometer DDF saved: %s", path)

    # ------------------------------------------------------------------
    # CSV (preserved for backward compatibility with the GUI)
    # ------------------------------------------------------------------

    def _save_csv(
        self, signal: np.ndarray, indm: int, ind1: int, ind2: int,
        a: np.ndarray, rms: float, resolfit: float, xcen: float,
        auto_it_ms: float, dark_level: float, sn: str, ts_compact: str,
    ) -> Path:
        try:
            import pandas as pd  # type: ignore
        except ImportError as exc:
            raise RuntimeError("pandas is required to save Check Spectrometer CSV") from exc

        npix = len(signal)
        summary = {
            "SN": sn,
            "Timestamp": ts_compact,
            "AutoIT_ms": round(auto_it_ms, 4),
            "MeasureIT_ms": _MEASURE_IT_MS,
            "DarkLevel_counts": round(dark_level, 4),
            "xcen_pixel": round(xcen, 3),
            "resolfit_pixels": round(resolfit, 3),
            "shape_exponent_n": round(float(a[3]), 4),
            "fit_rms": round(rms, 4),
            "center_offset_a0": round(float(a[0]), 3),
            "amplitude_a1": round(float(a[1]), 3),
            "width_a2": round(float(a[2]), 3),
            "background_a4": round(float(a[4]), 3),
            "peak_pixel_indm": indm,
            "window_lo": ind1,
            "window_hi": ind2 - 1,
        }
        pixel_cols = {f"Pixel_{i}": round(float(signal[i]), 4) for i in range(npix)}
        row = {**summary, **pixel_cols}

        self.output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = self.output_dir / f"CheckSpectrometer_{sn}_{ts_compact}.csv"
        pd.DataFrame([row]).to_csv(csv_path, index=False)
        LOGGER.info("Check spectrometer CSV saved: %s", csv_path)
        return csv_path
