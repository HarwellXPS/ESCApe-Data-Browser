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
2. The **Browser** window shows a tree with columns (detail, points, pass
   energy, etch time) and a **Filter** box; its shape adapts to the file
   (samples show their stage position; depth profiles nest as Sample → region
   → per-level entries with each level's etch time, and selecting a region
   folder takes every level under it). **Right-click** any item for a context
   menu: plot everything under it, export from there down (CSV/VAMAS), or
   create a stacked plot. A second **Display** window opens
   with three panels that update live as you click items:
   * **Spectra** (top-left) — the selected spectrum. Select several at once
     (Ctrl/Shift-click, or click a sample to take all its regions) and they
     tile automatically in a near-square grid, up to 4×4 (16) per page with
     Prev/Next paging beyond that. **Save spectra to PDF** writes the current
     selection (paginated) to a PDF you can keep or print.
   * **Metadata** (right) — acquisition details for the selection: sample,
     date acquired, instrument, source/anode, power, pass energy, etc.
   * **Images** (bottom strip) — the holder camera snapshots as selectable
     thumbnails; click one to open it full size. The image window offers two
     ways to see where each sample sat:
     - **Stage map (beside)** — a schematic X–Y plot in mm next to the photo,
       with the current spectrum selection highlighted; always available.
     - **Overlay positions on photo** — draws the markers directly on the
       photograph. This needs a one-time **Calibrate…** step where you enter
       your camera’s image centre (mm), mm-per-pixel, and any X/Y flip or
       rotation. The calibration is saved (in your home folder) and reused
       automatically; adjust it until the markers land on the right samples.
3. **File → Export spectra…** to pick regions (grouped by sample, with
   All/None) and export them as CSV or VAMAS. VAMAS output is CasaXPS-
   compatible: a kinetic-energy abscissa with two corresponding variables,
   **Intensity** and the interpolated spectrometer **Transmission** function
   (this can be toggled off in the export dialog).
4. **File → Export metadata → CSV / PDF** (or the **Metadata…** button) to save
   the per-sample acquisition metadata — system/instrument, X-ray source and
   anode, source power, pass energy, step size, dwell time, lens mode,
   aperture, charge-neutraliser and ion-gun status, BE range, points and
   quality. CSV gives one row per region; PDF is a formatted report with one
   section per sample.

## Stacked / waterfall plots

Select several spectra — e.g. depth-profile levels 0, 10, 57, 99, 150, 209, or
any individual regions — then click **Stacked plot…** (or right-click →
*Create stacked plot*). The window stacks the traces with an adjustable
vertical offset and lets you normalise them: *None*, *Max = 1*, *Area = 1*, or
*At cursor* — in the last mode you click anywhere on the plot to set an energy,
and every spectrum is scaled to match at that point (a dashed line marks it),
which is ideal for comparing peak-shape changes through a depth profile. Traces
are labelled by level/etch time (or sample/region), the binding-energy axis is
inverted, and **Save PDF…** writes the figure out.

## Depth profiles

Sputter depth profiles are detected automatically. For these files the export
dialog replaces the long per-region list with:

* **region-type checkboxes** (e.g. Survey, O 1s, Mo 3d) — choose which
  regions to include across all levels;
* **level selection** — *All levels*, *First N*, *Every Nth*, or a *level
  range* (so you can, for example, match a CasaXPS export of the first 61
  levels, or thin a 200-level profile to every 10th).

The app reads the per-etch duration from the instrument record, computes the
**sequential etch time** for every level (level 0 = surface at t = 0, then the
cumulative sputter time), and reports the **total etch time**. The etch level
and etch time appear in the metadata CSV/PDF, in the Display metadata panel,
and are written into each VAMAS block as comment lines.

## Notes

* **tkinter** is part of Python but needs an OS package on some Linux systems:
  `sudo apt-get install python3-tk` (Debian/Ubuntu),
  `sudo dnf install python3-tkinter` (Fedora). The launcher will tell you if
  it’s missing.
* The VAMAS exporter writes the spectrometer transmission function as a
  second corresponding variable, interpolated (and extrapolated at the ends)
  from the instrument's calibration onto each spectrum's kinetic-energy grid.
  This matches CasaXPS output exactly — intensities are identical and
  transmission agrees to floating-point precision — so files round-trip
  through CasaXPS. Untick the option in the export dialog for an
  intensity-only file.
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
