#!/usr/bin/env python3
"""
ESCApe Explorer
===============
A GUI browser, viewer and exporter for Kratos ESCApe ``.experiment`` files.

Features
--------
* **Browser window** (main): a tree view of the experiment hierarchy
  (sample -> spectral regions / images) with acquisition conditions.
* **Display window** (separate, live): updates in real time as you select a
  node in the browser — plots spectra, shows the camera image, or lists the
  full metadata for the selected item.
* **Export**: pick exactly which regions/images to export, in **CSV** or
  **VAMAS (ISO 14976)** format, via a checkbox dialog.
* **Corruption detection**: the loader detects files damaged by a UTF-8
  text round-trip (which silently destroys the binary spectra and images)
  and tells you clearly rather than exporting noise.

Dependencies
------------
* ``tkinter``  (standard library)
* ``matplotlib``  (optional — for spectrum plots)        pip install matplotlib
* ``Pillow``      (optional — for camera images)         pip install pillow

The app runs without matplotlib/Pillow; those panes simply show a notice.

Note on this format
-------------------
``.experiment`` is an undocumented proprietary container. The experiment
*structure* and *acquisition settings* are parsed reliably; the numeric
spectrum decoder is best-effort reverse engineering and should be validated
against a known-good ESCApe or VAMAS export.
"""

from __future__ import annotations

import os
import re
import csv
import struct
import datetime
from dataclasses import dataclass, field
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Optional dependencies ----------------------------------------------------
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg, NavigationToolbar2Tk
    )
    HAVE_MPL = True
except Exception:
    HAVE_MPL = False

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


# ==========================================================================
#  PARSER
# ==========================================================================
@dataclass
class Region:
    name: str
    index: int
    offset: int
    technique: str = "XPS"
    conditions: dict = field(default_factory=dict)
    energy: Optional[list] = None
    counts: Optional[list] = None
    energy_label: str = "Binding Energy"
    energy_units: str = "eV"
    count_label: str = "Intensity"
    count_units: str = "counts"
    decodable: bool = False
    note: str = ""
    sample: str = ""
    # acquisition metadata
    photon_energy: Optional[float] = None
    pass_energy: Optional[float] = None
    dwell: Optional[float] = None
    step: Optional[float] = None
    lens_mode: str = ""
    aperture: str = ""
    anode: str = ""

    @property
    def n_points(self) -> int:
        return len(self.counts) if self.counts else 0


@dataclass
class ImageBlob:
    name: str
    offset: int
    data: bytes
    is_jpeg_intact: bool
    note: str = ""


@dataclass
class TreeNode:
    label: str
    type_name: str = ""
    offset: int = 0
    children: list = field(default_factory=list)
    region: Optional[Region] = None
    image: Optional[ImageBlob] = None


class EscapeParser:
    UTF8_REPL = b"\xef\xbf\xbd"
    SPECTRUM_MARKER = b"DataTypes.EscaSpectrum"
    RESULT_MARKER = b"ProcessData.ProcessResult"
    IMAGE_MARKER = b"DataTypes.HolderContentSnapshotData"
    SAMPLE_MARKER = b"ProcessData.SampleAnalysis"
    SETTINGS_MARKER = b"NICPU.Acquisition.Spectrum.SpectroscopySettings"
    PASS_ENERGIES = (2, 5, 10, 20, 40, 80, 160, 224, 280)

    def __init__(self):
        self.path = None
        self.raw = b""
        self.strings: list = []
        self.regions: list = []
        self.images: list = []
        self.samples: list = []        # list[(offset, name)]
        self.tree: Optional[TreeNode] = None
        self.instrument = {}           # system-wide metadata
        self.corruption = {"corrupted": False, "message": ""}
        self.summary = {}

    # -- public ---------------------------------------------------------
    def load(self, path: str):
        with open(path, "rb") as fh:
            self.raw = fh.read()
        self.path = path
        self._check_corruption()
        self._scan_strings()
        self._parse_samples()
        self._parse_instrument()
        self._parse_regions()
        self._parse_images()
        self._build_tree()
        self._build_summary()
        return self

    # -- corruption -----------------------------------------------------
    def _check_corruption(self):
        raw, n = self.raw, len(self.raw)
        repl = raw.count(self.UTF8_REPL)
        frac = (3 * repl) / n if n else 0.0
        no_ff = b"\xff" not in raw
        no_nul = b"\x00" not in raw
        corrupted = repl > 50 and (no_ff or no_nul)
        msg = ""
        if corrupted:
            msg = (
                "This file has been damaged by a UTF-8 text round-trip: "
                f"{repl:,} replacement characters cover about {frac * 100:.0f}% "
                "of the file. The numeric spectra and the camera image cannot "
                "be recovered from it — the original bytes are irreversibly "
                "lost. The experiment structure and acquisition settings are "
                "still readable.\n\nTo recover the data, re-export the "
                ".experiment file from ESCApe and transfer it in binary mode "
                "(for example, put it in a .zip first)."
            )
        self.corruption = {"corrupted": corrupted, "repl_count": repl,
                           "repl_fraction": frac, "message": msg}

    # -- strings --------------------------------------------------------
    def _scan_strings(self):
        raw, i, out, n = self.raw, 0, [], len(self.raw)
        while i < n - 1:
            ln = raw[i]
            if 4 <= ln <= 120:
                chunk = raw[i + 1: i + 1 + ln]
                if len(chunk) == ln and all(32 <= b < 127 for b in chunk):
                    out.append((i, chunk.decode("ascii")))
                    i += 1 + ln
                    continue
            i += 1
        self.strings = out

    def _strings_between(self, lo, hi):
        return [(o, s) for (o, s) in self.strings if lo <= o < hi]

    def _find_all(self, marker):
        offs, start = [], 0
        while True:
            p = self.raw.find(marker, start)
            if p == -1:
                break
            offs.append(p)
            start = p + 1
        return offs

    # -- regions --------------------------------------------------------
    def _parse_regions(self):
        spec = self._find_all(self.SPECTRUM_MARKER)
        res = self._find_all(self.RESULT_MARKER)
        regions = []
        for idx, off in enumerate(spec):
            nxt = min([o for o in spec + res if o > off] + [len(self.raw)])
            regions.append(self._build_region(idx, off, nxt, res))
        self.regions = regions

    def _parse_samples(self):
        """Find each SampleAnalysis block and its sample identifier."""
        samples = []
        for k, off in enumerate(self._find_all(self.SAMPLE_MARKER)):
            near = self._strings_between(off, off + 220)
            name = None
            # Prefer an identifier-looking token (e.g. PR001, EXPO 18-9)
            pat = re.compile(r"^\d*:?\s?[A-Z]{2,}[\w\- ]*\d+$")
            for _, s in near:
                if pat.match(s) and "." not in s and "\\" not in s and len(s) <= 16:
                    name = s
                    break
            samples.append((off, name or f"Sample {k + 1}"))
        self.samples = samples

    def _sample_for(self, offset: int) -> str:
        owners = [(o, n) for (o, n) in self.samples if o < offset]
        return owners[-1][1] if owners else (self.samples[0][1]
                                             if self.samples else "Sample")

    def _date_for(self, offset: int) -> str:
        dates = getattr(self, "dates", [])
        if not dates:
            return ""
        return min(dates, key=lambda od: abs(od[0] - offset))[1]

    # -- instrument-level metadata --------------------------------------
    @staticmethod
    def _anode_from_hv(hv):
        if hv is None:
            return ""
        table = [(1486.6, "Al K-alpha (monochromated)"),
                 (1253.6, "Mg K-alpha"),
                 (2984.3, "Ag L-alpha"),
                 (1740.0, "Si K-alpha")]
        for e, n in table:
            if abs(hv - e) < 3:
                return n
        return f"{hv:.1f} eV source"

    def _parse_instrument(self):
        raw = self.raw
        instrument = next((m.group().decode() for m in
                           [re.search(rb"MI-[A-Z0-9\-]+", raw)] if m), "")
        host = next((s for _, s in self.strings
                     if re.match(r"^[A-Za-z0-9\-]+$", s) and "-" in s
                     and not s.startswith("MI-")
                     and len(s) <= 14), "")
        neutraliser = b"AxisChargeNeutraliser" in raw
        ion_gun = any(t in raw for t in
                      (b"Sputter", b"IonGun", b"Ion Gun", b"Minibeam",
                       b"MiniBeam", b"GasCluster", b"Etch"))
        # acquisition dates (dd/mm/yyyy) with their byte offsets
        self.dates = [(m.start(), m.group().decode())
                      for m in re.finditer(rb"[0-3]?\d/[01]?\d/20\d\d", raw)]
        # source / lens / aperture: read from the first settings block
        source = lens = aperture = ""
        ss = self._find_all(self.SETTINGS_MARKER)
        if ss:
            tokens = self._settings_tokens(ss[0])
            aperture = tokens[0] if len(tokens) > 0 else ""
            lens = tokens[1] if len(tokens) > 1 else ""
            source = next((s for _, s in self._strings_between(
                ss[0], ss[0] + 120) if "monochrom" in s.lower()
                or "achromat" in s.lower()), "")
        self.instrument = {
            "Instrument": instrument or "(unknown)",
            "Acquisition computer": host or "(unknown)",
            "X-ray source": source or "(unknown)",
            "Lens mode": lens,
            "Aperture": aperture,
            "Charge neutraliser": "Yes" if neutraliser else "No",
            "Ion gun / sputtering": "Used" if ion_gun else "Not used",
        }

    def _settings_tokens(self, ss_off, span=60):
        """Short strings right after a SpectroscopySettings marker."""
        e = ss_off + len(self.SETTINGS_MARKER)
        out, j = [], e
        while j < e + span:
            n = self.raw[j]
            if 3 <= n <= 24:
                c = self.raw[j + 1: j + 1 + n]
                if len(c) == n and all(32 <= b < 127 for b in c):
                    out.append(c.decode())
                    j += 1 + n
                    continue
            j += 1
        return out

    def _pass_energy_for(self, es_off):
        ss = [o for o in self._find_all(self.SETTINGS_MARKER) if o < es_off]
        if not ss:
            return None
        e = ss[-1] + len(self.SETTINGS_MARKER)
        for k in range(0, 56):
            try:
                v = struct.unpack_from("<d", self.raw, e + k)[0]
            except struct.error:
                break
            if v in self.PASS_ENERGIES:
                return v
        return None

    def _settings_for(self, es_off):
        ss = [o for o in self._find_all(self.SETTINGS_MARKER) if o < es_off]
        if not ss:
            return "", ""
        toks = self._settings_tokens(ss[-1])
        ap = toks[0] if len(toks) > 0 else ""
        lens = toks[1] if len(toks) > 1 else ""
        return ap, lens

    def _build_region(self, idx, off, end, result_offsets):
        starts = [o for o in result_offsets if o < off]
        block_start = starts[-1] if starts else max(0, off - 400)
        hs = self._strings_between(block_start, off)
        reg = Region(name=self._guess_region_name(hs), index=idx, offset=off,
                     conditions=self._extract_conditions(hs))
        reg.sample = self._sample_for(off)
        reg.pass_energy = self._pass_energy_for(off)
        reg.aperture, reg.lens_mode = self._settings_for(off)
        self._decode_spectrum(reg, off, end)
        if reg.pass_energy is not None:
            reg.conditions.setdefault("Pass energy", f"{reg.pass_energy:g} eV")
        if reg.dwell is not None:
            reg.conditions.setdefault("Dwell time", f"{reg.dwell:.3g} s")
        return reg

    @staticmethod
    def _guess_region_name(hs):
        cands = [s for _, s in hs]
        pat = re.compile(r"^[A-Z][a-z]?\s?\d[spdf]\d?$|^[A-Z][a-z]? [A-Z]{2,3}$")
        for s in cands:
            if pat.match(s):
                return s
        for s in cands:
            if s.lower() in ("wide", "survey"):
                return s
        ignore = {"Spectroscopy", "Analysis"}
        for s in reversed(cands):
            if s not in ignore and "." not in s and "\\" not in s and len(s) <= 12:
                return s
        return "Region"

    @staticmethod
    def _extract_conditions(hs):
        cond, texts = {}, [s for _, s in hs]
        for i, s in enumerate(texts):
            if s == "X-ray Power" and i + 1 < len(texts):
                cond["X-ray Power"] = texts[i + 1]
            if s == "Quality" and i + 1 < len(texts):
                cond["Quality"] = texts[i + 1]
        return cond

    def _decode_spectrum(self, reg, off, end):
        """Populate reg.energy / reg.counts in place.

        Uses the real EscaSpectrum layout (photon energy + kinetic-energy
        range, then an int32 point count followed by N float64 ordinates).
        Falls back to a heuristic float scan if the structure isn't found.
        """
        if self.corruption["corrupted"]:
            reg.decodable = False
            reg.note = ("Numeric data not available — the binary payload is "
                        "corrupted (UTF-8 round-trip damage).")
            return

        if self._decode_structured(reg, off, end):
            return

        # Fallback: heuristic scan (last resort; energy axis is just an index)
        payload = self.raw[off + len(self.SPECTRUM_MARKER): end]
        for fmt, size in (("<d", 8), ("<f", 4)):
            arr = self._scan_float_array(payload, fmt, size)
            if arr is not None:
                reg.energy = list(range(len(arr)))
                reg.counts = arr
                reg.energy_label, reg.energy_units = "Point", "index"
                reg.decodable = True
                reg.note = ("Structured header not found; spectrum recovered "
                            "heuristically with an index axis (no energy "
                            "calibration). Verify against a known-good export.")
                return
        reg.decodable = False
        reg.note = "Spectrum payload present but could not be decoded."

    def _decode_structured(self, reg, off, end):
        """Decode one EscaSpectrum block using the known layout. -> bool."""
        try:
            d = lambda o: struct.unpack_from("<d", self.raw, o)[0]
            i32 = lambda o: struct.unpack_from("<i", self.raw, o)[0]

            # Energy-axis doubles follow the "Uninitialized" tag.
            u = self.raw.find(b"Uninitialized", off, end)
            if u == -1:
                return False
            ue = u + len(b"Uninitialized")
            hv = d(ue)            # photon energy (e.g. 1486.69 eV, Al Ka)
            ke_a = d(ue + 8)      # kinetic-energy start
            ke_b = d(ue + 16)     # kinetic-energy end
            dwell = d(ue + 24)    # dwell time per step (seconds)
            if not (50.0 < hv < 6000.0 and 0.0 <= ke_a < hv + 50
                    and 0.0 <= ke_b < hv + 50):
                return False

            # Transmission function, then the point count + ordinates.
            tf = self.raw.find(self.TF_MARKER, off, end)
            if tf == -1:
                return False
            vend = tf + len(self.TF_MARKER)
            npairs = i32(vend + 4)
            if not (0 <= npairs < 100000):
                return False
            tf_end = vend + 8 + npairs * 16
            n = i32(tf_end)
            if not (1 < n < 5_000_000):
                return False
            cstart = tf_end + 4
            avail = (end - cstart) // 8
            n = min(n, avail)
            if n < 2:
                return False
            counts = list(struct.unpack_from(f"<{n}d", self.raw, cstart))

            # Binding-energy axis: BE = photon energy - kinetic energy.
            ke = [ke_a + (ke_b - ke_a) * j / (n - 1) for j in range(n)]
            energy = [hv - k for k in ke]

            reg.energy = energy
            reg.counts = counts
            reg.energy_label, reg.energy_units = "Binding Energy", "eV"
            reg.count_label, reg.count_units = "Intensity", "counts"
            reg.decodable = True
            step = (ke_b - ke_a) / (n - 1)
            reg.photon_energy = hv
            reg.anode = self._anode_from_hv(hv)
            reg.dwell = dwell if (dwell == dwell and 0 < dwell < 1e4) else None
            reg.step = abs(step)
            reg.conditions.setdefault("Photon energy", f"{hv:.2f} eV")
            reg.conditions.setdefault("Anode", reg.anode)
            reg.conditions.setdefault(
                "BE range", f"{energy[0]:.1f} - {energy[-1]:.1f} eV")
            reg.conditions.setdefault("Step", f"{abs(step):.3f} eV")
            reg.conditions.setdefault("Points", str(n))
            reg.note = ("Decoded from the EscaSpectrum structure. Binding "
                        "energy = photon energy − kinetic energy; not "
                        "charge-corrected.")
            return True
        except (struct.error, IndexError, ZeroDivisionError):
            return False

    TF_MARKER = b"TransFunc.Core.VisionTf"

    @staticmethod
    def _scan_float_array(payload, fmt, size):
        n, best = len(payload), []
        for phase in range(size):
            cur = []
            for i in range(phase, n - size, size):
                try:
                    v = struct.unpack(fmt, payload[i: i + size])[0]
                except struct.error:
                    v = float("nan")
                if (v == v) and (0.0 <= v < 1e9):
                    cur.append(v)
                else:
                    if len(cur) > len(best):
                        best = cur
                    cur = []
            if len(cur) > len(best):
                best = cur
        if len(best) >= 64 and len(set(round(x, 3) for x in best)) > 10:
            return best
        return None

    # -- images ---------------------------------------------------------
    def _parse_images(self):
        imgs = []
        for off in self._find_all(self.IMAGE_MARKER):
            blob = self.raw[off:]
            intact = (b"\xff\xd8" in blob) and not self.corruption["corrupted"]
            note = "" if intact else ("Camera image present but not recoverable "
                                      "from this file (JPEG markers destroyed).")
            imgs.append(ImageBlob("Holder snapshot", off, blob, intact, note))
        self.images = imgs

    def extract_jpeg(self, blob):
        if not blob.is_jpeg_intact:
            return None
        d = blob.data
        s = d.find(b"\xff\xd8")
        if s == -1:
            return None
        e = d.find(b"\xff\xd9", s)
        return d[s:(e + 2) if e != -1 else len(d)]

    # -- tree -----------------------------------------------------------
    def _build_tree(self):
        base = os.path.basename(self.path) if self.path else "Experiment"
        root = TreeNode(f"Experiment: {base}", "SampleAnalysis")

        # Group regions by their owning sample, preserving order.
        order = []
        groups = {}
        for r in self.regions:
            if r.sample not in groups:
                groups[r.sample] = []
                order.append(r.sample)
            groups[r.sample].append(r)

        if not order:                       # no regions at all
            order = [s for _, s in self.samples] or ["Sample"]
            groups = {s: [] for s in order}

        for sample_name in order:
            sample_node = TreeNode(f"Sample: {sample_name}", "SampleAnalysis")
            for r in groups.get(sample_name, []):
                cond = (f"  ({r.conditions['X-ray Power']})"
                        if r.conditions.get("X-ray Power") else "")
                tag = "" if r.decodable else "  [no data]"
                sample_node.children.append(
                    TreeNode(f"{r.name}{cond}{tag}", "EscaSpectrum",
                             r.offset, region=r))
            root.children.append(sample_node)

        if self.images:
            imgs = TreeNode(f"Images ({len(self.images)})",
                            "HolderSnapshotFolder")
            for n, im in enumerate(self.images, 1):
                lbl = im.name if len(self.images) == 1 else f"{im.name} {n}"
                imgs.children.append(
                    TreeNode(lbl, "HolderContentSnapshot", im.offset, image=im))
            root.children.append(imgs)

        self.tree = root

    def _build_summary(self):
        self.summary = {
            "file": self.path, "size": len(self.raw),
            "n_regions": len(self.regions),
            "region_names": [r.name for r in self.regions],
            "n_images": len(self.images),
            "n_decodable": sum(1 for r in self.regions if r.decodable),
            "corrupted": self.corruption["corrupted"],
        }

    # -- metadata -------------------------------------------------------
    def region_metadata(self, r: "Region") -> dict:
        """Full, ordered acquisition metadata for one region."""
        def fmt(v, unit="", nd=None):
            if v is None:
                return ""
            if nd is not None:
                return f"{v:.{nd}f}{unit}"
            return f"{v}{unit}"

        be0 = r.energy[0] if r.decodable and r.energy else None
        be1 = r.energy[-1] if r.decodable and r.energy else None
        md = {}
        md["Sample"] = r.sample
        md["Region"] = r.name
        md["Technique"] = r.technique
        md["Date acquired"] = self._date_for(r.offset)
        md["Instrument"] = self.instrument.get("Instrument", "")
        md["Acquisition computer"] = self.instrument.get("Acquisition computer", "")
        md["X-ray source"] = self.instrument.get("X-ray source", "")
        md["Anode"] = r.anode or self.instrument.get("X-ray source", "")
        md["Photon energy (eV)"] = fmt(r.photon_energy, "", 2)
        md["Source power (W)"] = (r.conditions.get("X-ray Power", "")
                                  .replace("W", "").strip())
        md["Pass energy (eV)"] = fmt(r.pass_energy, "", 0) if r.pass_energy else ""
        md["Lens mode"] = r.lens_mode or self.instrument.get("Lens mode", "")
        md["Aperture"] = r.aperture or self.instrument.get("Aperture", "")
        md["BE start (eV)"] = fmt(be0, "", 2)
        md["BE end (eV)"] = fmt(be1, "", 2)
        md["Step (eV)"] = fmt(r.step, "", 3)
        md["Dwell (s)"] = fmt(r.dwell, "", 3)
        md["Points"] = str(r.n_points) if r.n_points else ""
        md["Quality"] = r.conditions.get("Quality", "")
        md["Charge neutraliser"] = self.instrument.get("Charge neutraliser", "")
        md["Ion gun / sputtering"] = self.instrument.get("Ion gun / sputtering", "")
        return md

    def metadata_rows(self):
        """One metadata dict per region, in file order."""
        return [self.region_metadata(r) for r in self.regions]

    def samples_metadata(self):
        """Grouped: {sample_name: [region_metadata, ...]} preserving order."""
        groups, order = {}, []
        for r in self.regions:
            groups.setdefault(r.sample, []).append(self.region_metadata(r))
            if r.sample not in order:
                order.append(r.sample)
        return [(s, groups[s]) for s in order]


# ==========================================================================
#  EXPORTERS
# ==========================================================================
def export_csv(regions, path):
    """Export selected regions to a single CSV (wide format)."""
    usable = [r for r in regions if r.decodable and r.counts]
    if not usable:
        raise ValueError("None of the selected regions contain decodable data.")
    cols = []
    maxlen = 0
    for r in usable:
        pre = f"{r.sample} " if r.sample else ""
        cols.append((f"{pre}{r.name} {r.energy_label} ({r.energy_units})", r.energy))
        cols.append((f"{pre}{r.name} {r.count_label} ({r.count_units})", r.counts))
        maxlen = max(maxlen, len(r.counts))
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([c[0] for c in cols])
        for i in range(maxlen):
            row = [(c[1][i] if i < len(c[1]) else "") for c in cols]
            w.writerow(row)
    return len(usable)


def export_vamas(regions, path, institution="Kratos", instrument="AXIS",
                 operator="", experiment_id="", sample_id="Sample"):
    """Export selected regions as a VAMAS (ISO 14976) file.

    Implements the standard sequential block layout: experiment mode NORM,
    scan mode REGULAR, technique XPS, one corresponding (ordinate) variable.
    Only regions with decodable data are written.
    """
    usable = [r for r in regions if r.decodable and r.counts]
    if not usable:
        raise ValueError("None of the selected regions contain decodable data.")

    L = []  # one value per line
    a = L.append

    # ---- experiment header ----
    a("VAMAS Surface Chemical Analysis Standard Data Transfer Format 1988 May 4")
    a(institution)
    a(instrument)
    a(operator)
    a(experiment_id)
    a("0")            # number of lines in comment
    a("NORM")         # experiment mode
    a("REGULAR")      # scan mode
    # (experiment mode NORM) number of spectral regions:
    a(str(len(usable)))
    a("0")            # number of experimental variables
    a("0")            # number of entries in parameter inclusion/exclusion list
    a("0")            # number of manually entered items in block
    a("0")            # number of future-upgrade experiment entries
    a("0")            # number of future-upgrade block entries
    a(str(len(usable)))  # number of blocks

    now = datetime.datetime.now()
    for r in usable:
        e0 = r.energy[0]
        de = (r.energy[1] - r.energy[0]) if len(r.energy) > 1 else 1.0
        ords = r.counts
        blk_sample = r.sample or sample_id
        # Use the region's decoded photon energy if available.
        hv = "1486.6"
        if r.conditions.get("Photon energy"):
            hv = r.conditions["Photon energy"].split()[0]
        a(r.name)                 # block identifier
        a(blk_sample)             # sample identifier
        a(str(now.year)); a(str(now.month)); a(str(now.day))
        a(str(now.hour)); a(str(now.minute)); a(str(now.second))
        a("0")                    # hours in advance of GMT
        a("0")                    # number of lines in block comment
        a("XPS")                  # technique
        a("")                     # analysis source label
        a(hv)                     # source characteristic energy
        a("0")                    # source strength
        a("0")                    # beam width x
        a("0")                    # beam width y
        a("0")                    # source polar angle of incidence
        a("0")                    # source azimuth
        a("FAT")                  # analyser mode
        a("0")                    # pass energy / retard ratio
        a("0")                    # magnification of transfer lens
        a("0")                    # work function / acceptance energy
        a("0")                    # target bias
        a("0")                    # analysis width x
        a("0")                    # analysis width y
        a("0")                    # take-off polar angle
        a("0")                    # take-off azimuth
        a(r.name.split()[0] if r.name else "")  # species label
        a("")                     # transition / charge state label
        a("-1")                   # charge of detected particle
        # (scan mode REGULAR):
        a(r.energy_label.lower())   # abscissa label
        a(r.energy_units)           # abscissa units
        a(f"{e0:.6g}")              # abscissa start
        a(f"{de:.6g}")              # abscissa increment
        a("1")                      # number of corresponding variables
        a(r.count_label.lower())    # corresponding variable label
        a(r.count_units)            # corresponding variable units
        a("pulse counting")         # signal mode
        a("1")                      # signal collection time
        a("1")                      # number of scans
        a("0")                      # signal time correction
        a("0")                      # sample normal polar angle of tilt
        a("0")                      # sample normal tilt azimuth
        a("0")                      # sample rotation angle
        a("0")                      # number of additional numerical parameters
        a(str(len(ords)))           # number of ordinate values
        a(f"{min(ords):.6g}")       # minimum ordinate value (var 1)
        a(f"{max(ords):.6g}")       # maximum ordinate value (var 1)
        for v in ords:
            a(f"{v:.6g}")

    a("end of experiment")
    with open(path, "w", newline="\r\n") as fh:
        fh.write("\n".join(L) + "\n")
    return len(usable)


def export_metadata_csv(parser, path):
    """Write one row of acquisition metadata per region."""
    rows = parser.metadata_rows()
    if not rows:
        raise ValueError("No regions found to export metadata for.")
    fields = list(rows[0].keys())
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return len(rows)


# Fields that are constant for a sample -> shown once in the PDF header block.
_SAMPLE_LEVEL = [
    "Date acquired", "Instrument", "Acquisition computer", "X-ray source",
    "Anode", "Source power (W)", "Lens mode", "Aperture",
    "Charge neutraliser", "Ion gun / sputtering",
]
# Per-region columns for the PDF table.
_REGION_COLS = [
    ("Region", "Region"), ("PE (eV)", "Pass energy (eV)"),
    ("BE start", "BE start (eV)"), ("BE end", "BE end (eV)"),
    ("Step (eV)", "Step (eV)"), ("Dwell (s)", "Dwell (s)"),
    ("Points", "Points"), ("Quality", "Quality"),
    ("hv (eV)", "Photon energy (eV)"),
]


def export_metadata_pdf(parser, path):
    """Write a formatted, per-sample metadata report as PDF.

    Uses reportlab if available (nicer tables); otherwise falls back to a
    matplotlib-rendered PDF so the feature works with the base dependencies.
    """
    samples = parser.samples_metadata()
    if not samples:
        raise ValueError("No regions found to export metadata for.")
    try:
        return _metadata_pdf_reportlab(parser, samples, path)
    except ImportError:
        return _metadata_pdf_matplotlib(parser, samples, path)


def _metadata_pdf_reportlab(parser, samples, path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak)

    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8,
                           leading=10)
    doc = SimpleDocTemplate(path, pagesize=landscape(A4),
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=12 * mm,
                            title="ESCApe acquisition metadata")
    story = []
    fname = os.path.basename(parser.path or "experiment")
    story.append(Paragraph("ESCApe Acquisition Metadata", h1))
    story.append(Paragraph(
        f"File: {fname} &nbsp;&nbsp; Samples: {len(samples)} &nbsp;&nbsp; "
        f"Regions: {parser.summary['n_regions']} &nbsp;&nbsp; "
        f"Generated: {datetime.datetime.now():%Y-%m-%d %H:%M}", small))
    if parser.corruption["corrupted"]:
        story.append(Paragraph(
            "<font color='red'>Warning: this file's binary data is corrupted; "
            "numeric values may be unavailable.</font>", small))
    story.append(Spacer(1, 6 * mm))

    hdr_style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b0b0b0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f2f5f8")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ])

    for si, (sample, rows) in enumerate(samples):
        if si > 0:
            story.append(PageBreak())
        story.append(Paragraph(f"Sample: {sample}", h2))
        # sample-level block (use first region's values)
        base = rows[0]
        info = [[k, base.get(k, "")] for k in _SAMPLE_LEVEL]
        info_tbl = Table(info, colWidths=[55 * mm, 110 * mm])
        info_tbl.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#2c3e50")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ]))
        story.append(info_tbl)
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(f"Regions ({len(rows)})", small))

        table = [[c[0] for c in _REGION_COLS]]
        for row in rows:
            table.append([row.get(c[1], "") for c in _REGION_COLS])
        t = Table(table, repeatRows=1)
        t.setStyle(hdr_style)
        story.append(t)

    doc.build(story)
    return len(samples)


def _metadata_pdf_matplotlib(parser, samples, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    fname = os.path.basename(parser.path or "experiment")
    with PdfPages(path) as pdf:
        for sample, rows in samples:
            fig = plt.figure(figsize=(11.7, 8.3))  # A4 landscape
            fig.suptitle(f"ESCApe metadata — {fname}\nSample: {sample}",
                         fontsize=12, x=0.02, ha="left")
            ax = fig.add_axes([0.02, 0.02, 0.96, 0.84])
            ax.axis("off")
            base = rows[0]
            lines = [f"{k}: {base.get(k, '')}" for k in _SAMPLE_LEVEL]
            ax.text(0, 1.0, "\n".join(lines), va="top", fontsize=8,
                    family="monospace")
            col_labels = [c[0] for c in _REGION_COLS]
            cells = [[row.get(c[1], "") for c in _REGION_COLS] for row in rows]
            tbl = ax.table(cellText=cells, colLabels=col_labels,
                           loc="lower center", cellLoc="center")
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(7)
            tbl.scale(1, 1.2)
            pdf.savefig(fig)
            plt.close(fig)
    return len(samples)


# ==========================================================================
#  GUI
# ==========================================================================
class DisplayWindow(tk.Toplevel):
    """Composite display: spectra grid (top-left), metadata (right),
    selectable image filmstrip (bottom). Renders 1..16 spectra per page in a
    near-square grid (max 4x4) and can save the selection to PDF."""

    MAX_PER_PAGE = 16

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("ESCApe Display")
        self.geometry("1040x720")
        self.protocol("WM_DELETE_WINDOW", self.hide)

        self.regions = []          # currently selected regions
        self.page = 0
        self._thumb_imgs = []      # keep refs so Tk doesn't GC them
        self._view_photo = None

        # --- toolbar ---------------------------------------------------
        tb = ttk.Frame(self)
        tb.pack(side="top", fill="x", padx=6, pady=4)
        self.save_btn = ttk.Button(tb, text="Save spectra to PDF",
                                   command=self.save_pdf, state="disabled")
        self.save_btn.pack(side="left")
        self.prev_btn = ttk.Button(tb, text="◀ Prev", width=8,
                                   command=self.prev_page, state="disabled")
        self.prev_btn.pack(side="left", padx=(12, 2))
        self.page_lbl = ttk.Label(tb, text="")
        self.page_lbl.pack(side="left", padx=2)
        self.next_btn = ttk.Button(tb, text="Next ▶", width=8,
                                   command=self.next_page, state="disabled")
        self.next_btn.pack(side="left", padx=2)
        self.count_lbl = ttk.Label(tb, text="No spectra selected")
        self.count_lbl.pack(side="right")

        # --- bottom image filmstrip (packed first so it keeps its height) -
        self.strip_wrap = ttk.LabelFrame(self, text="Images")
        self.strip_wrap.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        self.strip_canvas = tk.Canvas(self.strip_wrap, height=128,
                                      highlightthickness=0)
        sx = ttk.Scrollbar(self.strip_wrap, orient="horizontal",
                           command=self.strip_canvas.xview)
        self.strip_canvas.configure(xscrollcommand=sx.set)
        sx.pack(side="bottom", fill="x")
        self.strip_canvas.pack(side="top", fill="x")
        self.strip_inner = ttk.Frame(self.strip_canvas)
        self.strip_canvas.create_window((0, 0), window=self.strip_inner,
                                        anchor="nw")
        self.strip_inner.bind(
            "<Configure>",
            lambda e: self.strip_canvas.configure(
                scrollregion=self.strip_canvas.bbox("all")))

        # --- main split: spectra (left) | metadata (right) -------------
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(side="top", fill="both", expand=True, padx=6, pady=4)

        self.plot_frame = ttk.Frame(pane)
        pane.add(self.plot_frame, weight=4)
        self.canvas = None
        self.fig = None
        if HAVE_MPL:
            self.fig = Figure(figsize=(6.5, 4.5), dpi=100)
            self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
            self.canvas.get_tk_widget().pack(expand=True, fill="both")
            NavigationToolbar2Tk(self.canvas, self.plot_frame)
        else:
            ttk.Label(self.plot_frame, justify="left", padding=20,
                      text="matplotlib is not installed.\n\n"
                           "    pip install matplotlib").pack()

        meta_frame = ttk.LabelFrame(pane, text="Metadata")
        pane.add(meta_frame, weight=1)
        self.meta = tk.Text(meta_frame, wrap="word", width=30,
                            font=("TkDefaultFont", 9))
        ms = ttk.Scrollbar(meta_frame, command=self.meta.yview)
        self.meta.configure(yscrollcommand=ms.set, state="disabled")
        ms.pack(side="right", fill="y")
        self.meta.pack(side="left", expand=True, fill="both")

    # -- window helpers -------------------------------------------------
    def hide(self):
        self.withdraw()

    # -- image filmstrip ------------------------------------------------
    def set_images(self, images):
        for w in self.strip_inner.winfo_children():
            w.destroy()
        self._thumb_imgs = []
        if not images:
            ttk.Label(self.strip_inner, text="  (no images in this file)",
                      padding=8).pack(side="left")
            return
        for n, blob in enumerate(images, 1):
            cell = ttk.Frame(self.strip_inner)
            cell.pack(side="left", padx=4, pady=4)
            thumb = self._make_thumb(blob)
            if thumb is not None:
                btn = ttk.Button(cell, image=thumb,
                                 command=lambda b=blob: self.open_image(b))
                btn.image = thumb
                self._thumb_imgs.append(thumb)
            else:
                btn = ttk.Button(cell, text="[image\nunavailable]", width=12,
                                 command=lambda b=blob: self.open_image(b))
            btn.pack()
            label = blob.name if len(images) == 1 else f"{blob.name} {n}"
            ttk.Label(cell, text=label, font=("TkDefaultFont", 8)).pack()

    def _make_thumb(self, blob, size=(150, 100)):
        if not HAVE_PIL:
            return None
        jpeg = self.app.parser.extract_jpeg(blob)
        if jpeg is None:
            return None
        try:
            import io
            img = Image.open(io.BytesIO(jpeg))
            img.thumbnail(size)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def open_image(self, blob):
        viewer = tk.Toplevel(self)
        viewer.title(blob.name)
        if not HAVE_PIL:
            ttk.Label(viewer, padding=20,
                      text="Pillow is not installed.\n\n  pip install pillow"
                      ).pack()
            return
        jpeg = self.app.parser.extract_jpeg(blob)
        if jpeg is None:
            ttk.Label(viewer, padding=20,
                      text=f"Image cannot be displayed.\n\n{blob.note}").pack()
            return
        try:
            import io
            img = Image.open(io.BytesIO(jpeg))
            img.thumbnail((1000, 760))
            self._view_photo = ImageTk.PhotoImage(img)
            ttk.Label(viewer, image=self._view_photo).pack()
        except Exception as exc:
            ttk.Label(viewer, padding=20, text=f"Could not render:\n{exc}").pack()

    # -- selection entry points ----------------------------------------
    def show_node(self, node: TreeNode):
        """Single-node entry (kept for compatibility)."""
        if node is None:
            return
        if node.image is not None:
            self.deiconify()
            self.open_image(node.image)
            return
        self.show_regions(self._regions_under(node))

    def show_regions(self, regions):
        self.deiconify()
        self.regions = [r for r in regions if r is not None]
        self.page = 0
        self._render()
        self._update_metadata()

    @staticmethod
    def _regions_under(node: TreeNode):
        out = []
        if node.region is not None:
            out.append(node.region)
        for c in node.children:
            out += DisplayWindow._regions_under(c)
        return out

    # -- grid layout ----------------------------------------------------
    @staticmethod
    def _grid_dims(n):
        import math
        cols = min(4, max(1, math.ceil(math.sqrt(n))))
        rows = min(4, math.ceil(n / cols))
        return rows, cols

    def _pages(self):
        n = len(self.regions)
        if n == 0:
            return 0
        return (n + self.MAX_PER_PAGE - 1) // self.MAX_PER_PAGE

    def _render(self):
        if not HAVE_MPL:
            return
        self.fig.clear()
        n = len(self.regions)
        npages = self._pages()
        self.count_lbl.config(
            text=("No spectra selected" if n == 0
                  else f"{n} spectrum selected" if n == 1
                  else f"{n} spectra selected"))
        self.save_btn.config(state=("normal" if n else "disabled"))
        for b in (self.prev_btn, self.next_btn):
            b.config(state=("normal" if npages > 1 else "disabled"))
        self.page_lbl.config(text=(f"Page {self.page + 1}/{npages}"
                                   if npages > 1 else ""))
        if n == 0:
            self.canvas.draw()
            return

        start = self.page * self.MAX_PER_PAGE
        chunk = self.regions[start:start + self.MAX_PER_PAGE]
        rows, cols = self._grid_dims(len(chunk))
        for i, r in enumerate(chunk):
            ax = self.fig.add_subplot(rows, cols, i + 1)
            self._plot_into(ax, r, compact=(len(chunk) > 1))
        self.fig.tight_layout()
        self.canvas.draw()

    @staticmethod
    def _plot_into(ax, r, compact=False):
        if r.decodable and r.counts:
            ax.plot(r.energy, r.counts, lw=0.8)
            title = f"{r.sample} — {r.name}" if r.sample else r.name
            ax.set_title(title, fontsize=(8 if compact else 11))
            if not compact:
                ax.set_xlabel(f"{r.energy_label} ({r.energy_units})")
                ax.set_ylabel(f"{r.count_label} ({r.count_units})")
            else:
                ax.tick_params(labelsize=6)
            if r.energy_label.lower().startswith("binding"):
                ax.invert_xaxis()
        else:
            ax.text(0.5, 0.5, f"{r.name}\n(no data)", ha="center",
                    va="center", transform=ax.transAxes,
                    fontsize=(8 if compact else 10))
            ax.set_axis_off()

    def prev_page(self):
        if self.page > 0:
            self.page -= 1
            self._render()

    def next_page(self):
        if self.page < self._pages() - 1:
            self.page += 1
            self._render()

    # -- metadata panel -------------------------------------------------
    def _update_metadata(self):
        self.meta.config(state="normal")
        self.meta.delete("1.0", "end")
        if not self.regions:
            self.meta.insert("end", "Select one or more spectra in the "
                                    "browser to see acquisition metadata.")
            self.meta.config(state="disabled")
            return
        parser = self.app.parser
        samples = sorted({r.sample for r in self.regions})
        if len(self.regions) == 1 or len(samples) == 1:
            base = parser.region_metadata(self.regions[0])
            order = ["Sample", "Date acquired", "Instrument",
                     "Acquisition computer", "X-ray source", "Anode",
                     "Photon energy (eV)", "Source power (W)",
                     "Charge neutraliser", "Ion gun / sputtering"]
            for k in order:
                if base.get(k):
                    self.meta.insert("end", f"{k}:\n  {base[k]}\n\n")
            if len(self.regions) == 1:
                r = self.regions[0]
                self.meta.insert("end", "— Region —\n")
                for k in ["Region", "Pass energy (eV)", "BE start (eV)",
                          "BE end (eV)", "Step (eV)", "Dwell (s)", "Points",
                          "Quality"]:
                    if base.get(k):
                        self.meta.insert("end", f"{k}: {base[k]}\n")
                if r.note:
                    self.meta.insert("end", f"\n{r.note}\n")
            else:
                self.meta.insert("end", f"— {len(self.regions)} regions —\n")
                for r in self.regions:
                    pe = (f"  PE {r.pass_energy:g} eV" if r.pass_energy else "")
                    self.meta.insert("end", f"• {r.name}{pe}\n")
        else:
            self.meta.insert("end", f"{len(self.regions)} spectra across "
                                    f"{len(samples)} samples:\n\n")
            for s in samples:
                rs = [r for r in self.regions if r.sample == s]
                self.meta.insert("end", f"{s} ({len(rs)}):\n")
                for r in rs:
                    self.meta.insert("end", f"  • {r.name}\n")
                self.meta.insert("end", "\n")
        self.meta.config(state="disabled")

    # -- save / print to PDF -------------------------------------------
    def save_pdf(self):
        if not self.regions:
            return
        if not HAVE_MPL:
            messagebox.showinfo("PDF", "matplotlib is required to make a PDF.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
            initialfile="spectra.pdf")
        if not path:
            return
        try:
            from matplotlib.backends.backend_pdf import PdfPages
            from matplotlib.figure import Figure as MplFigure
            with PdfPages(path) as pdf:
                per = self.MAX_PER_PAGE
                for p in range((len(self.regions) + per - 1) // per):
                    chunk = self.regions[p * per:(p + 1) * per]
                    rows, cols = self._grid_dims(len(chunk))
                    fig = MplFigure(figsize=(11.7, 8.3), dpi=150)
                    for i, r in enumerate(chunk):
                        ax = fig.add_subplot(rows, cols, i + 1)
                        self._plot_into(ax, r, compact=(len(chunk) > 1))
                    fig.tight_layout()
                    pdf.savefig(fig)
        except Exception as exc:
            messagebox.showerror("PDF failed", str(exc))
            return
        messagebox.showinfo("Saved",
                            f"Wrote {len(self.regions)} spectrum(a) to:\n{path}")


class ExportDialog(tk.Toplevel):
    """Checkbox selection of regions + format choice."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Export data")
        self.geometry("460x560")
        self.transient(master)
        self.grab_set()

        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(top, text="Select regions to export:",
                  font=("", 10, "bold")).pack(side="left")
        ttk.Button(top, text="None", width=6,
                   command=lambda: self._set_all(False)).pack(side="right")
        ttk.Button(top, text="All", width=6,
                   command=lambda: self._set_all(True)).pack(side="right", padx=4)

        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        frame = ttk.Frame(canvas)
        sb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True, padx=12)
        canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        # mouse-wheel scrolling for long lists
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        # Group regions by sample
        self.vars = []
        groups, order = {}, []
        for r in app.parser.regions:
            groups.setdefault(r.sample, []).append(r)
            if r.sample not in order:
                order.append(r.sample)

        for sample in order:
            ttk.Label(frame, text=sample or "Sample",
                      font=("", 9, "bold")).pack(anchor="w", pady=(8, 1))
            for r in groups[sample]:
                v = tk.BooleanVar(value=r.decodable)
                state = "normal" if r.decodable else "disabled"
                suffix = "" if r.decodable else "   (no data)"
                ttk.Checkbutton(frame, variable=v, state=state,
                                text=f"   {r.name}  [{r.n_points} pts]{suffix}"
                                ).pack(anchor="w", pady=0)
                self.vars.append((v, r))

        # format
        fmt_frame = ttk.LabelFrame(self, text="Format")
        fmt_frame.pack(fill="x", padx=12, pady=10)
        self.fmt = tk.StringVar(value="csv")
        ttk.Radiobutton(fmt_frame, text="CSV (.csv)", value="csv",
                        variable=self.fmt).pack(anchor="w", padx=8, pady=2)
        ttk.Radiobutton(fmt_frame, text="VAMAS / ISO 14976 (.vms)", value="vamas",
                        variable=self.fmt).pack(anchor="w", padx=8, pady=2)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(btns, text="Export", command=self.do_export).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)

        if app.parser.corruption["corrupted"]:
            ttk.Label(self, foreground="#a00", wraplength=430, justify="left",
                      text="This file's numeric data is corrupted, so no "
                           "regions can be exported. See the loader warning."
                      ).pack(padx=12, pady=(0, 10))

    def _set_all(self, value):
        for v, r in self.vars:
            if r.decodable:
                v.set(value)

    def do_export(self):
        chosen = [r for v, r in self.vars if v.get()]
        if not chosen:
            messagebox.showwarning("Nothing selected",
                                   "Select at least one region to export.")
            return
        fmt = self.fmt.get()
        if fmt == "csv":
            path = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV", "*.csv")])
            if not path:
                return
            try:
                n = export_csv(chosen, path)
            except Exception as exc:
                messagebox.showerror("Export failed", str(exc))
                return
        else:
            path = filedialog.asksaveasfilename(
                defaultextension=".vms",
                filetypes=[("VAMAS", "*.vms"), ("VAMAS", "*.vamas")])
            if not path:
                return
            sample = next((s for _, s in self.app.parser.strings
                           if s == "EXPO 18-9"), "Sample")
            try:
                n = export_vamas(chosen, path, sample_id=sample,
                                 experiment_id=os.path.basename(
                                     self.app.parser.path or ""))
            except Exception as exc:
                messagebox.showerror("Export failed", str(exc))
                return
        messagebox.showinfo("Exported",
                            f"Wrote {n} region(s) to:\n{path}")
        self.destroy()


class BrowserApp:
    """Main browser window."""

    def __init__(self, root):
        self.root = root
        self.root.title("ESCApe Explorer — Browser")
        self.root.geometry("440x600")
        self.parser = EscapeParser()
        self.node_map = {}          # treeview item id -> TreeNode
        self.display = None

        self._build_menu()
        self._build_body()

    def _build_menu(self):
        bar = tk.Menu(self.root)
        filem = tk.Menu(bar, tearoff=0)
        filem.add_command(label="Open .experiment…", command=self.open_file)
        filem.add_command(label="Export spectra…", command=self.open_export)
        filem.add_separator()
        filem.add_command(label="Export metadata → CSV…",
                          command=self.export_meta_csv)
        filem.add_command(label="Export metadata → PDF…",
                          command=self.export_meta_pdf)
        filem.add_separator()
        filem.add_command(label="Quit", command=self.root.quit)
        bar.add_cascade(label="File", menu=filem)
        viewm = tk.Menu(bar, tearoff=0)
        viewm.add_command(label="Show display window",
                          command=self.show_display)
        bar.add_cascade(label="View", menu=viewm)
        self.root.config(menu=bar)

    def _build_body(self):
        tb = ttk.Frame(self.root)
        tb.pack(side="top", fill="x", padx=4, pady=4)
        ttk.Button(tb, text="Open", command=self.open_file).pack(side="left")
        ttk.Button(tb, text="Export spectra…",
                   command=self.open_export).pack(side="left", padx=4)
        ttk.Button(tb, text="Metadata…",
                   command=self.open_metadata).pack(side="left")
        ttk.Button(tb, text="Display window",
                   command=self.show_display).pack(side="left", padx=4)

        self.tree = ttk.Treeview(self.root, show="tree", selectmode="extended")
        sb = ttk.Scrollbar(self.root, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="top", expand=True, fill="both")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        self.status = ttk.Label(self.root, anchor="w", relief="sunken",
                                text="Open a .experiment file to begin.")
        self.status.pack(side="bottom", fill="x")

    # -- actions --------------------------------------------------------
    def open_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Kratos Experiment", "*.experiment"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            self.parser = EscapeParser().load(path)
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))
            return
        self._populate_tree()
        s = self.parser.summary
        self.status.config(
            text=f"{os.path.basename(path)} — {s['n_regions']} regions, "
                 f"{s['n_images']} image(s), {s['n_decodable']} decodable")
        if self.parser.corruption["corrupted"]:
            messagebox.showwarning("File data corrupted",
                                   self.parser.corruption["message"])

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.node_map.clear()
        n_samples = len(self.parser.tree.children) if self.parser.tree else 0

        def add(parent, node, depth=0):
            # Keep the tree readable for big experiments: expand the root, and
            # expand sample folders only when there are just a few of them.
            opened = (depth == 0) or (depth == 1 and n_samples <= 3)
            iid = self.tree.insert(parent, "end", text=node.label, open=opened)
            self.node_map[iid] = node
            for c in node.children:
                add(iid, c, depth + 1)

        if self.parser.tree:
            add("", self.parser.tree)
        self.show_display()
        if self.display and self.display.winfo_exists():
            self.display.set_images(self.parser.images)
            self.display.show_regions([])

    def on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        self.show_display()
        # Gather regions from every selected node (expanding folders/samples).
        regions, seen = [], set()
        only_image = None
        for iid in sel:
            node = self.node_map.get(iid)
            if node is None:
                continue
            if node.image is not None and len(sel) == 1:
                only_image = node.image
            for r in DisplayWindow._regions_under(node):
                if id(r) not in seen:
                    seen.add(id(r))
                    regions.append(r)
        if regions:
            self.display.show_regions(regions)
        elif only_image is not None:
            self.display.open_image(only_image)

    def show_display(self):
        if self.display is None or not self.display.winfo_exists():
            self.display = DisplayWindow(self.root, self)
            self.display.set_images(self.parser.images)
        else:
            self.display.deiconify()

    def open_export(self):
        if not self.parser.regions:
            messagebox.showinfo("Nothing to export",
                                "Open a .experiment file first.")
            return
        ExportDialog(self.root, self)

    def open_metadata(self):
        if not self.parser.regions:
            messagebox.showinfo("No metadata",
                                "Open a .experiment file first.")
            return
        choice = MetadataDialog(self.root)
        self.root.wait_window(choice)
        if choice.result == "csv":
            self.export_meta_csv()
        elif choice.result == "pdf":
            self.export_meta_pdf()

    def export_meta_csv(self):
        if not self.parser.regions:
            messagebox.showinfo("No metadata", "Open a .experiment file first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile="metadata.csv")
        if not path:
            return
        try:
            n = export_metadata_csv(self.parser, path)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        messagebox.showinfo("Exported", f"Wrote metadata for {n} region(s) "
                                        f"to:\n{path}")

    def export_meta_pdf(self):
        if not self.parser.regions:
            messagebox.showinfo("No metadata", "Open a .experiment file first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
            initialfile="metadata.pdf")
        if not path:
            return
        try:
            n = export_metadata_pdf(self.parser, path)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        messagebox.showinfo("Exported",
                            f"Wrote a metadata report for {n} sample(s) "
                            f"to:\n{path}")


class MetadataDialog(tk.Toplevel):
    """Tiny chooser: export metadata as CSV or PDF."""

    def __init__(self, master):
        super().__init__(master)
        self.result = None
        self.title("Export metadata")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        ttk.Label(self, text="Export per-sample acquisition metadata as:",
                  padding=12).pack()
        btns = ttk.Frame(self)
        btns.pack(padx=12, pady=(0, 12))
        ttk.Button(btns, text="CSV", width=12,
                   command=lambda: self._pick("csv")).pack(side="left", padx=6)
        ttk.Button(btns, text="Formatted PDF", width=14,
                   command=lambda: self._pick("pdf")).pack(side="left", padx=6)

    def _pick(self, value):
        self.result = value
        self.destroy()


def main():
    root = tk.Tk()
    BrowserApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
