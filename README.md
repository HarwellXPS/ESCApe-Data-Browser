# ESCApe Explorer

A browser, viewer and exporter for Kratos ESCApe `.experiment` files. It shows
the experiment hierarchy (samples → spectral regions → conditions), plots each
spectrum on a calibrated binding-energy axis, displays the holder camera
image(s), and exports selected regions to **CSV** or **VAMAS (ISO 14976)**.

## Files

| File | Purpose |
|------|---------|
| `escape_explorer.py` | the application |
| `launch.py` | one-step launcher (creates a venv, installs deps, starts the app) |
| `requirements.txt` | the Python packages it needs (matplotlib, Pillow, reportlab) |
| `run.bat` | double-click launcher for Windows |
| `run.sh` | launcher for macOS / Linux |

Keep all of these in the same folder.

## Quick start

**Windows**

1. Install Python 3.8+ from https://www.python.org/downloads/ and tick
   *“Add python.exe to PATH”* during setup.
2. Double-click **`run.bat`**.

**macOS / Linux**

```bash
chmod +x run.sh        # first time only
./run.sh
```

On the first launch it builds an isolated environment in `./.venv` and installs
matplotlib and Pillow (an internet connection is needed once). Later launches
start immediately. Nothing is installed into your system Python.

## Manual launch (optional)

```bash
python launch.py            # Windows
python3 launch.py           # macOS / Linux
```

Flags:

| Flag | Effect |
|------|--------|
| `--setup-only` | build the environment but don’t start the app |
| `--reinstall`  | rebuild the environment from scratch |
| `--check`      | report environment status and exit |

## Using the app

1. **File → Open .experiment…** and choose a file.
2. The **Browser** window shows the tree. A second **Display** window opens
   with three panels that update live as you click items:
   * **Spectra** (top-left) — the selected spectrum. Select several at once
     (Ctrl/Shift-click, or click a sample to take all its regions) and they
     tile automatically in a near-square grid, up to 4×4 (16) per page with
     Prev/Next paging beyond that. **Save spectra to PDF** writes the current
     selection (paginated) to a PDF you can keep or print.
   * **Metadata** (right) — acquisition details for the selection: sample,
     date acquired, instrument, source/anode, power, pass energy, etc.
   * **Images** (bottom strip) — the holder camera snapshots as selectable
     thumbnails; click one to open it full size.
3. **File → Export spectra…** to pick regions (grouped by sample, with
   All/None) and export them as CSV or VAMAS.
4. **File → Export metadata → CSV / PDF** (or the **Metadata…** button) to save
   the per-sample acquisition metadata — system/instrument, X-ray source and
   anode, source power, pass energy, step size, dwell time, lens mode,
   aperture, charge-neutraliser and ion-gun status, BE range, points and
   quality. CSV gives one row per region; PDF is a formatted report with one
   section per sample.

## Notes

* **tkinter** is part of Python but needs an OS package on some Linux systems:
  `sudo apt-get install python3-tk` (Debian/Ubuntu),
  `sudo dnf install python3-tkinter` (Fedora). The launcher will tell you if
  it’s missing.
* Binding energy is computed as *photon energy − kinetic energy* and is **not
  charge-corrected**, so peaks may be shifted by a few eV on charging samples.
* Acquisition metadata (pass energy, dwell, step, source, etc.) is read from
  the file's binary structure. The common fields are reliable, but treat them
  as best-effort reverse engineering and cross-check anything critical against
  ESCApe. Fields that aren't present in a file (e.g. ion-gun settings when no
  ion gun was used) are reported as such rather than guessed.
* If a `.experiment` file was transferred as text rather than binary it can be
  silently corrupted; the app detects this and refuses to export noise rather
  than producing meaningless numbers.
