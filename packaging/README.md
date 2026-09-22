# Packaging SciLab for Windows

Produces two artifacts:

| Artifact | Path | What it is |
|---|---|---|
| Frozen app | `dist/SciLab/` | One-folder PyInstaller build, ~183 MB, runs without Python installed |
| Installer | `dist/installer/SciLab-Setup-<version>.exe` | ~55 MB Inno Setup installer |

## One-time setup

The build interpreter needs both the runtime dependencies *and* PyInstaller.
None of the system Pythons on a typical bench has the full set, so keep an
isolated environment — it also stops a stray dependency upgrade from changing
what ships:

```sh
py -3.12 -m venv .venv-build
.venv-build/Scripts/python.exe -m pip install -r requirements.txt pyinstaller
```

Python 3.12 is the right base: it is the only version that carries
**pythonnet** (needed by the Hama6 / C13016 backend) alongside matplotlib,
pyusb and Tk. A 3.13 build silently loses Hama6.

[Inno Setup 6](https://jrsoftware.org/isdl.php) must be installed for the
installer step (`winget install JRSoftware.InnoSetup`). Without it the build
still produces the frozen app and says the installer was skipped.

## Building

```sh
.venv-build/Scripts/python.exe packaging/build.py            # app + installer
.venv-build/Scripts/python.exe packaging/build.py --app-only # skip the installer
.venv-build/Scripts/python.exe packaging/build.py --clean    # discard build/ and dist/ first
```

The build fails loudly if the frozen bundle cannot import its own hardware
backends — see *Self-test* below.

## Releasing a new version

Edit **`scilab_version.py`** and rebuild. Nothing else carries a version
number:

```python
VERSION_TUPLE = (3, 4, 0, 6)
```

It flows into the title strip, the splash, the executable's Windows version
resource (Properties > Details), the installer filename, and the
Apps & features entry.

`APP_ID` in that file is the installer's identity and **must not change**
between releases of the same product — Windows uses it to recognise an
installed copy and upgrade it in place instead of stacking duplicate entries
in Apps & features.

## Self-test

The app can check its own installation without opening a window:

```
"C:\Program Files\SciLab\SciLab.exe" --self-test
```

It verifies every scientific dependency, all eight spectrometer backends, the
bundled vendor DLLs, the writable data directory and the bundled assets, then
prints a report and writes it to `%LOCALAPPDATA%\SciLab\self-test.log`. Exit
code is non-zero on failure.

Three places use it: the build (before wrapping the bundle), the installer
(right after copying files, so a machine-specific problem surfaces then rather
than on first use), and a Start-menu shortcut for support. Ask a site to run
that shortcut and send the log.

A frozen bundle that lost a backend looks perfectly healthy until an operator
tries to connect an instrument and gets "No spectrometer detected" — which is
why this exists.

## Installing

Per-machine (default, needs admin — right for a shared instrument PC):

```
SciLab-Setup-3.4.0.6.exe
```

Unattended, per-user, no elevation:

```
SciLab-Setup-3.4.0.6.exe /VERYSILENT /CURRENTUSER /NORESTART
```

Useful switches: `/DIR=<path>`, `/LOG=<file>`, `/SUPPRESSMSGBOXES`,
`/ALLUSERS`, `/NOICONS`.

> **Testing installers from Git Bash:** prefix the command with
> `MSYS_NO_PATHCONV=1`. Without it MSYS rewrites every `/SWITCH` into a path
> like `C:/Program Files/Git/VERYSILENT`, Setup sees no switches at all, and
> an intended per-user silent install runs the wizard machine-wide instead.
> The Inno log echoes the command line it received — check there first when a
> switch appears to be ignored.

## Where the installed app keeps its data

| | Location |
|---|---|
| Settings | `%LOCALAPPDATA%\SciLab\spectro_gui_settings.json` |
| Results | `%LOCALAPPDATA%\SciLab\results\` |
| Self-test log | `%LOCALAPPDATA%\SciLab\self-test.log` |

`Program Files` is read-only for a normal operator, so the installed build
writes per-user application data instead; settings are written atomically
(temp file + `os.replace`) so an interrupted write cannot reset a bench's COM
ports. Running from a source checkout still writes next to the code, so a
developer's files stay where they always were.

**Uninstalling deliberately leaves that folder alone** — it holds measurement
results and analysis output. Remove it by hand to reset a machine completely.

A fresh install therefore starts from built-in defaults, not from another
machine's configuration. To seed a bench, copy a known-good
`spectro_gui_settings.json` into `%LOCALAPPDATA%\SciLab\` after installing.

## Notes on the build choices

- **One folder, not one file.** A one-file build unpacks the entire bundle to
  a temp directory on every launch (seconds, with this scientific stack) and
  hands the vendor DLLs a different path each run. A folder also lets support
  swap a single vendor DLL in place.
- **No UPX.** Compressing scientific DLLs reliably trips antivirus heuristics.
- **Hidden imports are load-bearing.** Backends are imported through
  `importlib.import_module()`, so PyInstaller cannot see them; they are listed
  explicitly in `SciLab.spec` and covered by the self-test.
- **The icon is generated**, not hand-drawn: `build.py` crops the mark from
  `sciglob_logoRGB.png` and resamples it once per size (16-256 px) so the thin
  node lines survive at 16 px. The old `sciglob_symbol.ico` was 32 px only.
- **x64 only** — `ArchitecturesAllowed=x64compatible`. The bundled vendor DLLs
  are 64-bit, so a 32-bit install would produce an app that cannot open any
  instrument.

## Files here

| File | Purpose |
|---|---|
| `build.py` | Orchestrates the build; generates the version resource, icon and wizard images |
| `SciLab.spec` | PyInstaller configuration (data files, hidden imports) |
| `installer.iss` | Inno Setup script; takes all identity values from `build.py` |
| `scilab.ico` | Multi-resolution app icon (committed) |
| `scilab_256.png` | 256 px PNG of the mark, for docs |
| `file_version_info.txt` | Generated version resource (not committed) |
| `wizard_banner.bmp`, `wizard_small.bmp` | Generated installer artwork (not committed) |
