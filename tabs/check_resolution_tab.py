"""Check Resolution tab — continuously fits the brightest emission line.

Streams spectra from the connected spectrometer, runs the same modified-
Gaussian fit used by ``CheckSpectrometerService`` on every frame, and
displays the fit overlay plus live numeric readouts so the user can adjust
the spectrometer (focus, integration time, alignment) and watch CEN / width
/ shape react in real time.
"""
from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from services.check_spectrometer_service import _fit_peak, _mgauss

LOGGER = logging.getLogger(__name__)

_HALF_WIN = 40
_IT_MIN = 0.2
_IT_MAX = 3000.0
_FRAME_PAUSE_S = 0.05  # idle between frames


def build(app):
    from .theme import Colors, Fonts, configure_matplotlib_style, make_action_button

    app.check_res_running = threading.Event()
    app._check_res_pending_it = None
    app._check_res_pending_plot = None
    app._check_res_redraw_id = None

    main_frame = ttk.Frame(app.check_res_tab)
    main_frame.pack(fill="both", expand=True)
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(0, weight=1)

    plot_container = ttk.Frame(main_frame)
    plot_container.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

    controls_host = ttk.Frame(main_frame, width=320)
    controls_host.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4)
    controls_host.grid_propagate(False)

    # ---------------- Figure ----------------
    fig = Figure(figsize=(11, 7), dpi=100, constrained_layout=True)
    ax = fig.add_subplot(111)
    configure_matplotlib_style(fig, ax, title="Check Resolution — live fit")
    ax.set_facecolor("white")
    fig.patch.set_facecolor("white")
    ax.grid(True, linestyle="--", linewidth=0.7, color="#cccccc",
            alpha=0.9, zorder=0)
    ax.set_xlabel("PIXEL", fontsize=12, fontweight="bold")
    ax.set_ylabel("NORMALIZED SIGNAL", fontsize=12, fontweight="bold")

    # Series — match the colors used in the static Check Spectrometer plot.
    line_signal,    = ax.plot([], [], lw=0.8, color="#555555", label="SIGNAL", zorder=1)
    line_bg,        = ax.plot([], [], ".", color="black", ms=6,
                              label="BACKGROUND DATA", zorder=2)
    line_fitpts,    = ax.plot([], [], ".", color="blue", ms=4,
                              label="FITTING DATA", zorder=3)
    line_fit_curve, = ax.plot([], [], "-", color="red", lw=1.6,
                              label="FIT", zorder=4)

    legend = ax.legend(loc="upper left", frameon=False, fontsize=9,
                       labelspacing=0.2)
    for text in legend.get_texts():
        text.set_fontweight("bold")

    canvas = FigureCanvasTkAgg(fig, master=plot_container)
    canvas.draw()
    canvas.get_tk_widget().pack(fill="both", expand=True)
    NavigationToolbar2Tk(canvas, plot_container)

    app.check_res_fig = fig
    app.check_res_ax = ax
    app.check_res_canvas = canvas

    # Track user zoom — once the user pans/zooms, stop autoscaling.
    app._check_res_locked = False

    def _on_press(_event):
        pass

    def _on_release(event):
        if event.inaxes is not None:
            app._check_res_locked = True

    canvas.mpl_connect("button_release_event", _on_release)

    # ---------------- Controls ----------------
    spectrum_frame = ttk.LabelFrame(controls_host, text="Acquisition",
                                    padding=8)
    spectrum_frame.pack(fill="x", padx=4, pady=(4, 6))

    ttk.Label(spectrum_frame, text="Integration Time (ms):").pack(anchor="w")
    it_entry = ttk.Entry(spectrum_frame, width=15)
    it_entry.insert(0, "100")
    it_entry.pack(anchor="w", pady=(2, 6))
    app.check_res_it_entry = it_entry

    btn_row = ttk.Frame(spectrum_frame)
    btn_row.pack(fill="x")
    apply_it_btn = ttk.Button(btn_row, text="Apply IT",
                              command=lambda: _apply_it())
    apply_it_btn.pack(side="left", padx=(0, 4))
    reset_zoom_btn = ttk.Button(btn_row, text="Reset Zoom",
                                command=lambda: _reset_zoom())
    reset_zoom_btn.pack(side="left")

    control_frame = ttk.LabelFrame(controls_host, text="Live Fit",
                                   padding=8)
    control_frame.pack(fill="x", padx=4, pady=6)

    start_btn = make_action_button(control_frame, text="Start",
                                   command=lambda: _start(), width=18)
    start_btn.pack(fill="x", pady=(0, 6))
    stop_btn = make_action_button(control_frame, text="Stop",
                                  command=lambda: _stop(), danger=True,
                                  width=18)
    stop_btn.pack(fill="x")
    app.check_res_start_btn = start_btn
    app.check_res_stop_btn = stop_btn

    # Laser controls — share the BooleanVars created by live_view_tab so
    # toggling here also reflects in the Live View tab.
    laser_frame = ttk.LabelFrame(controls_host, text="Laser Controls",
                                 padding=8)
    laser_frame.pack(fill="x", padx=4, pady=6)

    laser_tags = ["377", "405", "445", "488", "517", "532", "640", "685", "Hg_Ar"]
    for tag in laser_tags:
        var = app.laser_vars.get(tag)
        if var is None:
            var = tk.BooleanVar(value=False)
            app.laser_vars[tag] = var
        text = "Hg_Ar" if tag == "Hg_Ar" else f"{tag} nm"
        chk = ttk.Checkbutton(
            laser_frame, text=text, variable=var,
            command=lambda t=tag, v=var: app.toggle_laser(t, v.get()),
        )
        chk.pack(anchor="w", pady=1)

    # Readout
    readout_frame = ttk.LabelFrame(controls_host, text="Fit Readout",
                                   padding=8)
    readout_frame.pack(fill="x", padx=4, pady=6)

    def _row(label_text):
        row = ttk.Frame(readout_frame)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text=label_text, width=14, anchor="w",
                  font=Fonts.BODY_SMALL_BOLD).pack(side="left")
        val = ttk.Label(row, text="—", anchor="w", foreground=Colors.TEXT_PRIMARY,
                        font=Fonts.BODY_SMALL)
        val.pack(side="left", fill="x", expand=True)
        return val

    val_cen   = _row("Center (px):")
    val_width = _row("Width (px):")
    val_shape = _row("Shape n:")
    val_rms   = _row("RMS:")
    val_indm  = _row("Peak pixel:")
    val_peak  = _row("Peak counts:")
    val_fps   = _row("Frame rate:")
    val_status = _row("Status:")
    val_status.config(text="Idle", foreground="gray")

    # ---------------- Helpers ----------------
    def _read_it_entry() -> float:
        try:
            it = float(it_entry.get())
        except Exception:
            raise ValueError("Invalid IT value")
        return max(_IT_MIN, min(_IT_MAX, it))

    def _apply_it():
        if not app.spec:
            messagebox.showwarning("Spectrometer", "Not connected.")
            return
        try:
            it = _read_it_entry()
        except ValueError as exc:
            messagebox.showerror("Apply IT", str(exc))
            return
        # Defer if streaming, apply immediately otherwise.
        if app.check_res_running.is_set():
            app._check_res_pending_it = it
            val_status.config(text=f"IT change queued ({it:.2f} ms)",
                              foreground="blue")
        else:
            try:
                app.spec.set_it(it)
                val_status.config(text=f"IT applied ({it:.2f} ms)",
                                  foreground="green")
            except Exception as exc:
                messagebox.showerror("Apply IT", str(exc))

    def _reset_zoom():
        app._check_res_locked = False

    def _start():
        if not app.spec:
            messagebox.showwarning("Spectrometer", "Not connected.")
            return
        if app.check_res_running.is_set():
            return
        # Make sure other live loops aren't fighting for the spectrometer.
        if getattr(app, "live_running", None) and app.live_running.is_set():
            messagebox.showwarning(
                "Check Resolution",
                "Live View is running. Stop it first.",
            )
            return
        try:
            it = _read_it_entry()
            app.spec.set_it(it)
        except Exception as exc:
            messagebox.showerror("Start", str(exc))
            return

        app.check_res_running.set()
        val_status.config(text="Running…", foreground="green")
        threading.Thread(target=_loop, daemon=True).start()

    def _stop():
        app.check_res_running.clear()
        val_status.config(text="Stopped", foreground="gray")

    # ---------------- Acquisition loop ----------------
    def _blind_correct(spec, signal_mean, signal_std):
        n_blind_r = int(getattr(spec, "npix_blind_right", 0) or 0)
        n_blind_l = int(getattr(spec, "npix_blind_left", 0) or 0)
        sig, unc = signal_mean, signal_std
        if n_blind_r > 0:
            dark = float(np.max(sig[-n_blind_r:]))
            sig = sig[:-n_blind_r] - dark
            unc = unc[:-n_blind_r] if unc.size >= sig.size + n_blind_r else unc
        elif n_blind_l > 0:
            dark = float(np.max(sig[:n_blind_l]))
            sig = sig[n_blind_l:] - dark
            unc = unc[n_blind_l:] if unc.size >= sig.size + n_blind_l else unc
        return sig, unc

    def _loop():
        last_t = time.time()
        ema_dt = None
        while app.check_res_running.is_set():
            try:
                # Apply queued IT change between frames.
                pending = app._check_res_pending_it
                if pending is not None:
                    app._check_res_pending_it = None
                    try:
                        app.spec.set_it(pending)
                    except Exception as exc:
                        LOGGER.warning("Check Resolution set_it failed: %s", exc)

                app.spec.measure(ncy=1)
                app.spec.wait_for_measurement()

                rcm = np.asarray(getattr(app.spec, "rcm", []), dtype=float)
                rcs = np.asarray(getattr(app.spec, "rcs", []), dtype=float)
                if rcm.size == 0 or not np.all(np.isfinite(rcm)):
                    time.sleep(_FRAME_PAUSE_S)
                    continue

                sig, unc = _blind_correct(app.spec, rcm, rcs if rcs.size else
                                          np.zeros_like(rcm))

                n_total = sig.size
                indm = int(np.argmax(sig))
                ind1 = max(0, indm - _HALF_WIN)
                ind2 = min(n_total, indm + _HALF_WIN + 1)
                xi = np.arange(ind1, ind2)
                yi = sig[ind1:ind2]
                uyi = unc[ind1:ind2] if unc.size >= n_total else None

                if xi.size < 6:
                    time.sleep(_FRAME_PAUSE_S)
                    continue

                fit_err, a, rms, resolfit, xxi, yyi = _fit_peak(xi, yi, uyi, indm)
                # Match service: report 2 × fitted half-width
                resolfit_disp = 2.0 * float(abs(a[2]))
                xcen = indm + float(a[0])

                # Build display arrays
                indbg = (xxi[1] + indm).astype(int)
                indbg = indbg[(indbg >= 0) & (indbg < n_total)]
                bg = float(np.mean(sig[indbg])) if indbg.size else 0.0
                peak_above_bg = float(sig[indm] - bg)
                if peak_above_bg <= 0:
                    peak_above_bg = max(float(np.max(sig) - bg), 1.0)
                norm = (sig - bg) / peak_above_bg

                pix = np.arange(n_total)
                x_bg = (xxi[1] + indm)
                y_bg = (yyi[1] - bg) / peak_above_bg
                x_fitpts = (xxi[2] + indm)
                y_fitpts = (yyi[2] - bg) / peak_above_bg
                x_curve = (xxi[3] + indm)
                y_curve = (_mgauss(xxi[3], *a) - bg) / peak_above_bg

                now = time.time()
                dt = now - last_t
                last_t = now
                ema_dt = dt if ema_dt is None else 0.7 * ema_dt + 0.3 * dt
                fps = 1.0 / ema_dt if ema_dt and ema_dt > 0 else 0.0

                snapshot = {
                    "pix": pix, "norm": norm,
                    "x_bg": x_bg, "y_bg": y_bg,
                    "x_fitpts": x_fitpts, "y_fitpts": y_fitpts,
                    "x_curve": x_curve, "y_curve": y_curve,
                    "indm": indm, "peak": float(sig[indm] + bg),
                    "xcen": xcen, "resolfit": resolfit_disp,
                    "shape": float(a[3]), "rms": float(rms),
                    "fit_err": int(fit_err), "fps": fps,
                }
                app._check_res_pending_plot = snapshot
                _schedule_redraw()

            except Exception as exc:
                LOGGER.exception("Check Resolution loop error")
                app.after(0, lambda e=exc: val_status.config(
                    text=f"Error: {e}", foreground="red"))
                break
            time.sleep(_FRAME_PAUSE_S)

        app.after(0, lambda: val_status.config(text="Stopped",
                                               foreground="gray"))

    def _schedule_redraw():
        if app._check_res_redraw_id is not None:
            return

        def _do():
            app._check_res_redraw_id = None
            snap = app._check_res_pending_plot
            if snap is None:
                return

            line_signal.set_data(snap["pix"], snap["norm"])
            line_bg.set_data(snap["x_bg"], snap["y_bg"])
            line_fitpts.set_data(snap["x_fitpts"], snap["y_fitpts"])
            line_fit_curve.set_data(snap["x_curve"], snap["y_curve"])

            if not app._check_res_locked:
                # Default zoom: tight on the windowed pixels.
                if snap["x_fitpts"].size:
                    x_min = float(np.min(snap["x_fitpts"]))
                    x_max = float(np.max(snap["x_fitpts"]))
                    span = max(x_max - x_min, 1.0)
                    pad = max(2.0, span * 0.05)
                    ax.set_xlim(x_min - pad, x_max + pad)
                    y_min = -0.05
                    y_max = 1.15
                    ax.set_ylim(y_min, y_max)

            err = snap["fit_err"]
            err_tag = ""
            if err == 1:
                err_tag = " >maxfun!"
            elif err == 2:
                err_tag = " >maxiter!"
            ax.set_title(
                f"Check Resolution — CEN={snap['xcen']:.2f}, "
                f"w={snap['resolfit']:.2f}, n={snap['shape']:.2f}, "
                f"rms={snap['rms']:.4f}{err_tag}",
                fontsize=11,
            )

            val_cen.config(text=f"{snap['xcen']:.3f}")
            val_width.config(text=f"{snap['resolfit']:.3f}")
            val_shape.config(text=f"{snap['shape']:.3f}")
            val_rms.config(text=f"{snap['rms']:.5f}")
            val_indm.config(text=str(snap["indm"]))
            val_peak.config(text=f"{snap['peak']:.0f}")
            val_fps.config(text=f"{snap['fps']:.1f} Hz")

            if getattr(app, "_is_tab_visible", lambda _w: True)(app.check_res_tab):
                canvas.draw_idle()

        app._check_res_redraw_id = app.after(33, _do)

    # Stop streaming if the tab/window is destroyed.
    def _on_destroy(_event=None):
        app.check_res_running.clear()

    app.check_res_tab.bind("<Destroy>", _on_destroy)
