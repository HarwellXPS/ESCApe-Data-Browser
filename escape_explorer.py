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
import json
import math
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
    tf_ke: Optional[list] = None       # transmission-function kinetic energies
    tf_values: Optional[list] = None   # transmission-function values
    etch_level: Optional[int] = None   # depth-profile level (0 = surface)
    etch_time: Optional[float] = None  # cumulative etch time (s) at this level
    pos_x: Optional[float] = None      # stage analysis position X (mm)
    pos_y: Optional[float] = None      # stage analysis position Y (mm)

    @property
    def n_points(self) -> int:
        return len(self.counts) if self.counts else 0

    @property
    def kinetic_energy(self):
        """Kinetic-energy axis (eV), or None if not decoded."""
        if self.photon_energy is None or not self.energy:
            return None
        return [self.photon_energy - be for be in self.energy]

    def transmission(self):
        """Per-point transmission function, linearly interpolated from the
        instrument's calibration pairs onto this spectrum's KE axis.
        Returns None if no transmission function is available."""
        if not self.tf_ke or not self.tf_values:
            return None
        ke = self.kinetic_energy
        if ke is None:
            return None
        xs, ys = self.tf_ke, self.tf_values
        out = []
        for x in ke:
            if x <= xs[0]:
                # linear extrapolation using the first segment
                if len(xs) > 1 and xs[1] != xs[0]:
                    f = (x - xs[0]) / (xs[1] - xs[0])
                    out.append(ys[0] + f * (ys[1] - ys[0]))
                else:
                    out.append(ys[0])
            elif x >= xs[-1]:
                # linear extrapolation using the last segment
                if len(xs) > 1 and xs[-1] != xs[-2]:
                    f = (x - xs[-2]) / (xs[-1] - xs[-2])
                    out.append(ys[-2] + f * (ys[-1] - ys[-2]))
                else:
                    out.append(ys[-1])
            else:
                lo = 0
                for i in range(len(xs) - 1):
                    if xs[i] <= x <= xs[i + 1]:
                        lo = i
                        break
                x0, x1 = xs[lo], xs[lo + 1]
                y0, y1 = ys[lo], ys[lo + 1]
                f = (x - x0) / (x1 - x0) if x1 != x0 else 0.0
                out.append(y0 + f * (y1 - y0))
        return out


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
    cols: tuple = ()          # extra column values for the browser tree


class EscapeParser:
    UTF8_REPL = b"\xef\xbf\xbd"
    SPECTRUM_MARKER = b"DataTypes.EscaSpectrum"
    RESULT_MARKER = b"ProcessData.ProcessResult"
    IMAGE_MARKER = b"DataTypes.HolderContentSnapshotData"
    SAMPLE_MARKER = b"ProcessData.SampleAnalysis"
    SETTINGS_MARKER = b"NICPU.Acquisition.Spectrum.SpectroscopySettings"
    LOCATION_MARKER = b"SampleHandling.InstrumentAnalysisLocation"
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
        self.depth_profile = {"is_profile": False, "n_levels": 0,
                              "regions_per_level": 0, "etch_per_level": 0.0,
                              "total_etch_time": 0.0, "cumulative": [],
                              "etch_source": ""}
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
        self._parse_positions()
        self._parse_depth_profile()
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
        host_like = re.compile(r"^[0-9A-Z]+(-[0-9A-Z]+)+$")
        ignore = {"Analysis", "Spectroscopy", "Slot", "Hybrid", "Tilt"}
        for k, off in enumerate(self._find_all(self.SAMPLE_MARKER)):
            near = self._strings_between(off, off + 220)
            name = None
            for _, s in near:
                if ("." in s or "\\" in s or s in ignore or len(s) > 16
                        or host_like.match(s) or not any(c.isalnum() for c in s)):
                    continue
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
            # Each pair is (kinetic energy, transmission), 2 x float64.
            tf_ke, tf_val = [], []
            for k in range(npairs):
                tf_ke.append(d(vend + 8 + k * 16))
                tf_val.append(d(vend + 8 + 8 + k * 16))
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
            reg.tf_ke = tf_ke
            reg.tf_values = tf_val
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
    # -- depth profile --------------------------------------------------
    GRAPH_MARKER = b"DataTypes.NonUniformGraphData"

    def _decode_duration_graph(self, off):
        """Decode (etch number, duration) pairs from a NonUniformGraphData
        block. Returns the list of per-etch durations (best effort)."""
        e = off + len(self.GRAPH_MARKER)
        base = self.raw.find(struct.pack("<d", 1.0), e, e + 220)
        if base < 0:
            return []
        d = lambda o: struct.unpack_from("<d", self.raw, o)[0]
        result = []
        for stride in (17, 16):
            ys, k = [], 0
            while True:
                ox = base + k * stride
                if ox + 16 > len(self.raw):
                    break
                try:
                    x = d(ox); y = d(ox + 8)
                except struct.error:
                    break
                if x != x or abs(x - (k + 1)) > 0.01:
                    break
                ys.append(y)
                k += 1
            if len(ys) >= 2:
                result = ys
                break
        return result

    def _parse_depth_profile(self):
        raw = self.raw
        is_profile = (b"DepthProfileData" in raw or b"Depth Profile" in raw
                      or b"Mb6EtchSettings" in raw)
        if not is_profile or not self.regions:
            return

        names = [r.name for r in self.regions]
        rpl = next((i for i in range(1, len(names))
                    if names[i] == names[0]), len(names))
        if rpl < 1 or len(names) % rpl != 0:
            rpl = 1
        n_levels = len(self.regions) // rpl

        durs = []
        for off in self._find_all(self.GRAPH_MARKER):
            labels = [s for _, s in self._strings_between(off, off + 60)]
            if any("Duration" in s for s in labels):
                durs += self._decode_duration_graph(off)

        n_etches = max(0, n_levels - 1)
        per_etch, source = [], ""
        if durs:
            import statistics
            med = statistics.median(durs)
            constant = all(abs(x - med) <= 0.01 * med + 1e-9 for x in durs)
            if constant:
                per_etch = [med] * n_etches
                source = f"constant {med:g} s/etch (from instrument record)"
            else:
                per_etch = list(durs)
                if len(per_etch) < n_etches:
                    per_etch += [per_etch[-1]] * (n_etches - len(per_etch))
                per_etch = per_etch[:n_etches]
                source = "per-etch durations (from instrument record)"
        else:
            per_etch = [0.0] * n_etches
            source = "etch time not recorded in file"

        cumulative = [0.0]
        for dd in per_etch:
            cumulative.append(cumulative[-1] + dd)

        for idx, r in enumerate(self.regions):
            lvl = idx // rpl
            r.etch_level = lvl
            r.etch_time = cumulative[lvl] if lvl < len(cumulative) else None

        self.depth_profile = {
            "is_profile": True,
            "n_levels": n_levels,
            "regions_per_level": rpl,
            "etch_per_level": (per_etch[0] if per_etch else 0.0),
            "total_etch_time": cumulative[-1] if cumulative else 0.0,
            "cumulative": cumulative,
            "etch_source": source,
            "n_etches": n_etches,
        }

    # -- analysis positions --------------------------------------------
    def _parse_positions(self):
        """Extract stage analysis positions (mm) from InstrumentAnalysisLocation
        blocks. Each stores two consecutive float64 (metres). The first block
        for each sample gives that sample's analysis position (later blocks are
        auto-Z / alignment points)."""
        d = lambda o: struct.unpack_from("<d", self.raw, o)[0]
        rep = {}
        loc = []
        if self.corruption["corrupted"]:
            self._locations, self._sample_pos = [], {}
            return
        for off in self._find_all(self.LOCATION_MARKER):
            e = off + len(self.LOCATION_MARKER)
            xy = None
            for k in range(40, 150):
                try:
                    x = d(e + k); y = d(e + k + 8)
                except struct.error:
                    break
                if (x == x and y == y and abs(x) < 0.06 and abs(y) < 0.06
                        and (abs(x) + abs(y)) > 1e-5):
                    xy = (x * 1000.0, y * 1000.0)
                    break
            if xy is None:
                continue
            owner = self._sample_for(off)
            loc.append((off, owner, xy[0], xy[1]))
            rep.setdefault(owner, xy)      # first block per sample wins
        self._locations = loc
        self._sample_pos = rep
        for r in self.regions:
            if r.sample in rep:
                r.pos_x, r.pos_y = rep[r.sample]

    def analysis_positions(self):
        """Distinct analysis positions as (label, x_mm, y_mm) per sample."""
        return [(s, xy[0], xy[1])
                for s, xy in getattr(self, "_sample_pos", {}).items()]

    def sample_positions(self):
        """One representative position per sample: {sample: (x, y)}."""
        return dict(getattr(self, "_sample_pos", {}))

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
    @staticmethod
    def _be_str(r):
        if r.decodable and r.energy:
            return f"{r.energy[0]:.0f}-{r.energy[-1]:.0f} eV"
        return "no data"

    @staticmethod
    def _pe_str(r):
        return f"{r.pass_energy:g}" if r.pass_energy else ""

    def _build_tree(self):
        base = os.path.basename(self.path) if self.path else "Experiment"
        root = TreeNode(f"Experiment: {base}", "experiment")
        is_profile = self.depth_profile.get("is_profile")
        pos = self.sample_positions()

        order, groups = [], {}
        for r in self.regions:
            if r.sample not in groups:
                groups[r.sample] = []
                order.append(r.sample)
            groups[r.sample].append(r)
        if not order:
            order = [s for _, s in self.samples] or ["Sample"]
            groups = {s: [] for s in order}

        for sample_name in order:
            pstr = ""
            if sample_name in pos:
                pstr = f"({pos[sample_name][0]:.1f}, {pos[sample_name][1]:.1f} mm)"
            sample_node = TreeNode(f"Sample: {sample_name}", "sample",
                                   cols=(pstr, "", "", ""))

            if is_profile:
                # Sample -> Region type -> per-level leaves
                byname, rorder = {}, []
                for r in groups[sample_name]:
                    if r.name not in byname:
                        byname[r.name] = []
                        rorder.append(r.name)
                    byname[r.name].append(r)
                for rname in rorder:
                    rl = byname[rname]
                    folder = TreeNode(rname, "regionfolder",
                                      cols=(f"{len(rl)} levels", "",
                                            self._pe_str(rl[0]), ""))
                    for r in rl:
                        et = (f"{r.etch_time:g} s" if r.etch_time is not None
                              else "")
                        folder.children.append(TreeNode(
                            f"Level {r.etch_level}", "EscaSpectrum", r.offset,
                            region=r,
                            cols=(self._be_str(r), str(r.n_points),
                                  self._pe_str(r), et)))
                    sample_node.children.append(folder)
            else:
                for r in groups[sample_name]:
                    tag = "" if r.decodable else "  [no data]"
                    sample_node.children.append(TreeNode(
                        f"{r.name}{tag}", "EscaSpectrum", r.offset, region=r,
                        cols=(self._be_str(r), str(r.n_points),
                              self._pe_str(r), "")))
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
        if r.pos_x is not None:
            md["Position X (mm)"] = f"{r.pos_x:.3f}"
            md["Position Y (mm)"] = f"{r.pos_y:.3f}"
        if r.etch_level is not None:
            md["Etch level"] = str(r.etch_level)
            md["Etch time (s)"] = (f"{r.etch_time:g}"
                                   if r.etch_time is not None else "")
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


def export_vamas(regions, path, institution="Not specified",
                 instrument="", operator="", experiment_id="",
                 sample_id="Sample", include_transmission=True):
    """Export selected regions as a VAMAS (ISO 14976) file.

    Sequential block layout: experiment mode NORM, scan mode REGULAR,
    technique XPS, kinetic-energy abscissa. When the spectrometer transmission
    function is available it is written as a second corresponding variable
    ("Transmission"), interleaved with intensity, matching CasaXPS exports.
    Only regions with decodable data are written.
    """
    usable = [r for r in regions if r.decodable and r.counts]
    if not usable:
        raise ValueError("None of the selected regions contain decodable data.")

    L = []
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
    a(str(len(usable)))  # number of spectral regions (NORM)
    a("0")            # number of experimental variables
    a("0")            # parameter inclusion/exclusion list entries
    a("0")            # manually entered items in block
    a("0")            # future-upgrade experiment entries
    a("0")            # future-upgrade block entries
    a(str(len(usable)))  # number of blocks

    now = datetime.datetime.now()
    SENT = "1E+37"    # VAMAS "not specified" sentinel
    for r in usable:
        hv = r.photon_energy if r.photon_energy else 1486.69
        # Kinetic-energy abscissa (matches CasaXPS and the transmission axis).
        ke = r.kinetic_energy or [hv - be for be in r.energy]
        ke0 = ke[0]
        dke = (ke[1] - ke[0]) if len(ke) > 1 else 1.0
        counts = r.counts
        trans = r.transmission() if include_transmission else None
        n_cv = 2 if trans else 1
        anode = (r.anode.split()[0] if r.anode else "Al")  # element only
        power = ""
        if r.conditions.get("X-ray Power"):
            power = r.conditions["X-ray Power"].replace("W", "").strip()
        dwell = f"{r.dwell:.6g}" if r.dwell else SENT

        a(r.name)                 # block identifier
        a(r.sample or sample_id)  # sample identifier
        a(str(now.year)); a(str(now.month)); a(str(now.day))
        a(str(now.hour)); a(str(now.minute)); a(str(now.second))
        a("0")                    # hours in advance of GMT
        # block comment: include etch info for depth profiles
        comment = []
        if r.etch_level is not None:
            comment.append(f"Etch level : {r.etch_level}")
        if r.etch_time is not None:
            comment.append(f"Etch time (s) : {r.etch_time:g}")
        a(str(len(comment)))      # lines in block comment
        for c in comment:
            a(c)
        a("XPS")                  # technique
        a(anode)                  # analysis source label
        a(f"{hv:.6g}")            # source characteristic energy
        a(power or SENT)          # source strength (W)
        a(SENT)                   # beam width x
        a(SENT)                   # beam width y
        a(SENT)                   # source polar angle of incidence
        a(SENT)                   # source azimuth
        a("FAT")                  # analyser mode
        a(f"{r.pass_energy:g}" if r.pass_energy else SENT)  # pass energy
        a(SENT)                   # magnification of transfer lens
        a("-4.5")                 # analyser work function
        a(SENT)                   # target bias
        a(SENT)                   # analysis width x
        a(SENT)                   # analysis width y
        a(SENT)                   # take-off polar angle
        a(SENT)                   # take-off azimuth
        a(r.name)                 # species label
        a("")                     # transition / charge state label
        a("-1")                   # charge of detected particle
        # (scan mode REGULAR)
        a("Kinetic energy")       # abscissa label
        a("eV")                   # abscissa units
        a(f"{ke0:.6g}")           # abscissa start
        a(f"{dke:.6g}")           # abscissa increment
        a(str(n_cv))              # number of corresponding variables
        a("Intensity"); a("d")    # corresponding var 1: label, units
        if trans:
            a("Transmission"); a("d")   # corresponding var 2
        a("pulse counting")       # signal mode
        a(dwell)                  # signal collection time (s)
        a("1")                    # number of scans
        a("0")                    # signal time correction
        a(SENT)                   # sample normal polar angle of tilt
        a(SENT)                   # sample normal tilt azimuth
        a(SENT)                   # sample rotation angle
        a("0")                    # additional numerical parameters

        def fc(v):                # count: integer when whole, else 8 sig figs
            return str(int(round(v))) if abs(v - round(v)) < 1e-6 else f"{v:.8g}"

        def ft(v):                # transmission: high precision
            return f"{v:.12g}"

        a(str(len(counts) * n_cv))             # number of ordinate values
        a(fc(min(counts)))                     # var 1 min
        a(fc(max(counts)))                     # var 1 max
        if trans:
            a(ft(min(trans)))                  # var 2 min
            a(ft(max(trans)))                  # var 2 max
        # ordinate values, interleaved per point
        if trans:
            for c, t in zip(counts, trans):
                a(fc(c))
                a(ft(t))
        else:
            for c in counts:
                a(fc(c))

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
CALIB_PATH = os.path.join(os.path.expanduser("~"), ".escape_explorer_calib.json")


def load_calibration():
    try:
        with open(CALIB_PATH) as fh:
            return json.load(fh)
    except Exception:
        return None


def save_calibration(c):
    try:
        with open(CALIB_PATH, "w") as fh:
            json.dump(c, fh, indent=2)
        return True
    except Exception:
        return False


def stage_to_pixel(x_mm, y_mm, img_w, img_h, c):
    """Map a stage coordinate (mm) to an image pixel using a calibration:
    {centre_x_mm, centre_y_mm, mm_per_px, flip_x, flip_y, rotation_deg}."""
    dx = (x_mm - c["centre_x_mm"]) / c["mm_per_px"]
    dy = (y_mm - c["centre_y_mm"]) / c["mm_per_px"]
    if c.get("flip_x"):
        dx = -dx
    if c.get("flip_y"):
        dy = -dy
    th = math.radians(c.get("rotation_deg", 0.0))
    rx = dx * math.cos(th) - dy * math.sin(th)
    ry = dx * math.sin(th) + dy * math.cos(th)
    return img_w / 2.0 + rx, img_h / 2.0 + ry


class CalibrationDialog(tk.Toplevel):
    """Enter the one-time camera-to-stage calibration."""

    def __init__(self, master, on_save, current=None):
        super().__init__(master)
        self.on_save = on_save
        self.title("Camera calibration")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        c = current or {"centre_x_mm": 0.0, "centre_y_mm": 0.0,
                        "mm_per_px": 0.02, "flip_x": False, "flip_y": False,
                        "rotation_deg": 0.0}

        intro = ("Map stage coordinates (mm) onto the holder photo. These are "
                 "fixed for your camera setup — enter them once.\n\n"
                 "• Image centre X/Y: the stage position (mm) at the centre of "
                 "the photo.\n"
                 "• mm per pixel: image width in mm ÷ pixel width.\n"
                 "• Flip X/Y, rotation: correct the photo's orientation so "
                 "markers land on the right samples.")
        ttk.Label(self, text=intro, wraplength=380, justify="left",
                  padding=12).grid(row=0, column=0, columnspan=2, sticky="w")

        self.vars = {}
        rows = [("Image centre X (mm)", "centre_x_mm"),
                ("Image centre Y (mm)", "centre_y_mm"),
                ("mm per pixel", "mm_per_px"),
                ("Rotation (degrees)", "rotation_deg")]
        r = 1
        for label, key in rows:
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="e",
                                             padx=(12, 6), pady=3)
            v = tk.StringVar(value=str(c.get(key, 0.0)))
            ttk.Entry(self, textvariable=v, width=14).grid(
                row=r, column=1, sticky="w", padx=(0, 12))
            self.vars[key] = v
            r += 1
        self.flip_x = tk.BooleanVar(value=c.get("flip_x", False))
        self.flip_y = tk.BooleanVar(value=c.get("flip_y", False))
        ttk.Checkbutton(self, text="Flip X", variable=self.flip_x).grid(
            row=r, column=0, sticky="w", padx=12)
        ttk.Checkbutton(self, text="Flip Y", variable=self.flip_y).grid(
            row=r, column=1, sticky="w")
        r += 1
        btns = ttk.Frame(self)
        btns.grid(row=r, column=0, columnspan=2, pady=12)
        ttk.Button(btns, text="Save", command=self._save).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")

    def _save(self):
        try:
            calib = {
                "centre_x_mm": float(self.vars["centre_x_mm"].get()),
                "centre_y_mm": float(self.vars["centre_y_mm"].get()),
                "mm_per_px": float(self.vars["mm_per_px"].get()),
                "rotation_deg": float(self.vars["rotation_deg"].get()),
                "flip_x": self.flip_x.get(),
                "flip_y": self.flip_y.get(),
            }
            if calib["mm_per_px"] == 0:
                raise ValueError("mm per pixel cannot be zero.")
        except ValueError as exc:
            messagebox.showerror("Invalid calibration", str(exc))
            return
        save_calibration(calib)
        self.on_save(calib)
        self.destroy()


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
        self.calib = load_calibration()   # camera-to-stage calibration

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
        positions = self.app.parser.sample_positions()
        self._cur_blob = blob

        bar = ttk.Frame(viewer)
        bar.pack(side="top", fill="x", padx=6, pady=4)
        show_map = tk.BooleanVar(value=False)
        overlay = tk.BooleanVar(value=False)
        body = ttk.Frame(viewer)
        body.pack(side="top", fill="both", expand=True)
        photo_frame = ttk.Frame(body)
        photo_frame.pack(side="left", fill="both", expand=True)
        map_frame = ttk.Frame(body)

        def redraw_photo():
            for w in photo_frame.winfo_children():
                w.destroy()
            if overlay.get() and self.calib and HAVE_MPL and positions:
                self._render_photo_overlay(photo_frame, blob, positions)
            else:
                self._render_plain_photo(photo_frame, blob)

        def toggle_map():
            if show_map.get() and HAVE_MPL and positions:
                map_frame.pack(side="left", fill="both", expand=True)
                self._render_stage_map(map_frame)
            else:
                map_frame.pack_forget()

        def toggle_overlay():
            if overlay.get() and not self.calib:
                overlay.set(False)
                if messagebox.askyesno(
                        "Calibration needed",
                        "Overlaying markers on the photo needs a one-time "
                        "camera calibration. Set it now?"):
                    open_calib()
                return
            redraw_photo()

        def open_calib():
            def saved(c):
                self.calib = c
                overlay.set(True)
                redraw_photo()
            CalibrationDialog(viewer, saved, current=self.calib)

        ttk.Button(bar, text="Calibrate…", command=open_calib).pack(side="left")
        ov_cb = ttk.Checkbutton(bar, variable=overlay, command=toggle_overlay,
                                text="Overlay positions on photo")
        ov_cb.pack(side="left", padx=8)
        map_cb = ttk.Checkbutton(bar, variable=show_map, command=toggle_map,
                                 text="Stage map (beside)")
        map_cb.pack(side="left")
        if not positions:
            for w in (ov_cb, map_cb):
                w.configure(state="disabled")
            ttk.Label(bar, text="  (no positions recorded)").pack(side="left")
        elif not HAVE_MPL:
            for w in (ov_cb, map_cb):
                w.configure(state="disabled")

        redraw_photo()

    def _render_plain_photo(self, parent, blob):
        if not HAVE_PIL:
            ttk.Label(parent, padding=20,
                      text="Pillow is not installed.\n\n  pip install pillow"
                      ).pack()
            return
        jpeg = self.app.parser.extract_jpeg(blob)
        if jpeg is None:
            ttk.Label(parent, padding=20,
                      text=f"Image cannot be displayed.\n\n{blob.note}").pack()
            return
        try:
            import io
            img = Image.open(io.BytesIO(jpeg))
            img.thumbnail((760, 580))
            self._view_photo = ImageTk.PhotoImage(img)
            ttk.Label(parent, image=self._view_photo).pack()
        except Exception as exc:
            ttk.Label(parent, padding=20, text=f"Could not render:\n{exc}").pack()

    def _render_photo_overlay(self, parent, blob, positions):
        """Show the photo with analysis markers placed via the calibration."""
        jpeg = self.app.parser.extract_jpeg(blob)
        if jpeg is None or not HAVE_PIL:
            self._render_plain_photo(parent, blob)
            return
        import io
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        w, h = img.size
        sel_samples = {r.sample for r in self.regions}
        fig = Figure(figsize=(7.2, 5.6), dpi=100)
        ax = fig.add_subplot(111)
        ax.imshow(img, extent=[0, w, h, 0])   # top-left origin
        for sample, (x_mm, y_mm) in positions.items():
            px, py = stage_to_pixel(x_mm, y_mm, w, h, self.calib)
            hot = sample in sel_samples
            ax.scatter([px], [py], s=160 if hot else 90,
                       facecolors="none",
                       edgecolors="#ff2d2d" if hot else "#19e0ff",
                       linewidths=2.2 if hot else 1.6, zorder=3)
            ax.annotate(sample, (px, py), textcoords="offset points",
                        xytext=(7, -7), fontsize=8,
                        color="#ff2d2d" if hot else "#19e0ff",
                        fontweight=("bold" if hot else "normal"))
        ax.set_xlim(0, w); ax.set_ylim(h, 0)
        ax.set_axis_off()
        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=parent)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()
        ttk.Label(parent, font=("", 8), foreground="#555", wraplength=560,
                  justify="left",
                  text="Markers placed from your saved calibration. If they're "
                       "off, use Calibrate… to adjust centre, scale, flip or "
                       "rotation.").pack(side="bottom", fill="x", padx=4)

    def _render_stage_map(self, parent):
        for w in parent.winfo_children():
            w.destroy()
        positions = self.app.parser.sample_positions()
        sel_samples = {r.sample for r in self.regions}
        fig = Figure(figsize=(4.6, 4.4), dpi=100)
        ax = fig.add_subplot(111)
        for sample, (x, y) in positions.items():
            hot = sample in sel_samples
            ax.scatter([x], [y], s=120 if hot else 70,
                       c="#d33" if hot else "#3a6ea5",
                       edgecolors="black", zorder=3)
            ax.annotate(sample, (x, y), textcoords="offset points",
                        xytext=(6, 5), fontsize=8,
                        fontweight=("bold" if hot else "normal"))
        ax.set_xlabel("Stage X (mm)")
        ax.set_ylabel("Stage Y (mm)")
        ax.set_title("Analysis positions on holder")
        ax.grid(True, ls=":", alpha=0.5)
        ax.set_aspect("equal", adjustable="datalim")
        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=parent)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()
        ttk.Label(parent, font=("", 8), foreground="#555", wraplength=300,
                  justify="left",
                  text="Schematic stage coordinates (mm). Highlighted points "
                       "match the current spectrum selection. Not overlaid on "
                       "the photo: the file has no camera calibration.").pack(
            side="bottom", fill="x", padx=4, pady=(0, 4))

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
            order = ["Sample", "Date acquired", "Etch level", "Etch time (s)",
                     "Instrument", "Acquisition computer", "X-ray source",
                     "Anode", "Photon energy (eV)", "Source power (W)",
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
        self.geometry("470x640")
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

        self.profile = app.parser.depth_profile
        self.vars = []           # per-region checkboxes (non-profile)
        self.type_vars = {}      # per-region-type checkboxes (profile)

        if self.profile.get("is_profile"):
            self._build_profile_selectors(frame)
        else:
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
                    ttk.Checkbutton(
                        frame, variable=v, state=state,
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
        self.incl_tf = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            fmt_frame, variable=self.incl_tf,
            text="Include spectrometer transmission function (VAMAS, "
                 "CasaXPS-compatible)").pack(anchor="w", padx=24, pady=(0, 4))

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(btns, text="Export", command=self.do_export).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)

        if app.parser.corruption["corrupted"]:
            ttk.Label(self, foreground="#a00", wraplength=430, justify="left",
                      text="This file's numeric data is corrupted, so no "
                           "regions can be exported. See the loader warning."
                      ).pack(padx=12, pady=(0, 10))

    def _build_profile_selectors(self, frame):
        dp = self.profile
        total_min = dp["total_etch_time"] / 60.0
        ttk.Label(frame, justify="left", font=("", 9),
                  text=(f"Depth profile: {dp['n_levels']} levels, "
                        f"{dp['regions_per_level']} regions/level\n"
                        f"Etch: {dp['etch_source']}\n"
                        f"Total etch time: {dp['total_etch_time']:g} s "
                        f"({total_min:g} min)")).pack(anchor="w", pady=(4, 8))

        ttk.Label(frame, text="Regions to include:",
                  font=("", 9, "bold")).pack(anchor="w")
        seen = []
        for r in self.app.parser.regions:
            if r.name not in seen:
                seen.append(r.name)
        for name in seen:
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(frame, variable=v, text=f"   {name}"
                            ).pack(anchor="w")
            self.type_vars[name] = v

        ttk.Label(frame, text="Levels to include:", font=("", 9, "bold")
                  ).pack(anchor="w", pady=(10, 1))
        self.level_mode = tk.StringVar(value="all")
        nlev = dp["n_levels"]
        for val, txt in [("all", f"All {nlev} levels"),
                         ("first", "First N levels"),
                         ("every", "Every Nth level"),
                         ("range", "Level range")]:
            ttk.Radiobutton(frame, text=txt, value=val,
                            variable=self.level_mode).pack(anchor="w")
        spin = ttk.Frame(frame)
        spin.pack(anchor="w", pady=4)
        ttk.Label(spin, text="N / step:").pack(side="left")
        self.n_spin = tk.IntVar(value=min(61, nlev))
        ttk.Spinbox(spin, from_=1, to=nlev, width=6,
                    textvariable=self.n_spin).pack(side="left", padx=4)
        ttk.Label(spin, text="range:").pack(side="left", padx=(10, 2))
        self.range_from = tk.IntVar(value=0)
        self.range_to = tk.IntVar(value=nlev - 1)
        ttk.Spinbox(spin, from_=0, to=nlev - 1, width=5,
                    textvariable=self.range_from).pack(side="left")
        ttk.Label(spin, text="–").pack(side="left")
        ttk.Spinbox(spin, from_=0, to=nlev - 1, width=5,
                    textvariable=self.range_to).pack(side="left")

    def _selected_regions(self):
        if not self.profile.get("is_profile"):
            return [r for v, r in self.vars if v.get()]
        types = {n for n, v in self.type_vars.items() if v.get()}
        mode = self.level_mode.get()
        n = max(1, self.n_spin.get())
        lo, hi = self.range_from.get(), self.range_to.get()

        def level_ok(lvl):
            if lvl is None:
                return True
            if mode == "all":
                return True
            if mode == "first":
                return lvl < n
            if mode == "every":
                return lvl % n == 0
            if mode == "range":
                return lo <= lvl <= hi
            return True

        return [r for r in self.app.parser.regions
                if r.decodable and r.name in types and level_ok(r.etch_level)]

    def _set_all(self, value):
        for v, r in self.vars:
            if r.decodable:
                v.set(value)
        for v in self.type_vars.values():
            v.set(value)

    def do_export(self):
        chosen = self._selected_regions()
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
            inst = self.app.parser.instrument
            try:
                n = export_vamas(
                    chosen, path,
                    instrument=inst.get("Instrument", ""),
                    operator=inst.get("Acquisition computer", ""),
                    experiment_id=os.path.basename(self.app.parser.path or ""),
                    include_transmission=self.incl_tf.get())
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
        self.root.geometry("620x640")
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

        # filter row
        fb = ttk.Frame(self.root)
        fb.pack(side="top", fill="x", padx=4, pady=(0, 4))
        ttk.Label(fb, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        ent = ttk.Entry(fb, textvariable=self.filter_var)
        ent.pack(side="left", fill="x", expand=True, padx=4)
        ent.bind("<KeyRelease>", lambda e: self._populate_tree())
        ttk.Button(fb, text="Clear", width=6,
                   command=lambda: (self.filter_var.set(""),
                                    self._populate_tree())).pack(side="left")

        cols = ("detail", "pts", "pe", "etch")
        self.tree = ttk.Treeview(self.root, columns=cols,
                                 show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Experiment / Sample / Region")
        self.tree.heading("detail", text="Detail")
        self.tree.heading("pts", text="Points")
        self.tree.heading("pe", text="Pass E (eV)")
        self.tree.heading("etch", text="Etch time")
        self.tree.column("#0", width=260, stretch=True)
        self.tree.column("detail", width=120, anchor="w")
        self.tree.column("pts", width=60, anchor="e")
        self.tree.column("pe", width=70, anchor="e")
        self.tree.column("etch", width=80, anchor="e")
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
        self._populate_tree(reset_display=True)
        s = self.parser.summary
        self.status.config(
            text=f"{os.path.basename(path)} — {s['n_regions']} regions, "
                 f"{s['n_images']} image(s), {s['n_decodable']} decodable")
        if self.parser.corruption["corrupted"]:
            messagebox.showwarning("File data corrupted",
                                   self.parser.corruption["message"])

    def _populate_tree(self, reset_display=False):
        self.tree.delete(*self.tree.get_children())
        self.node_map.clear()
        n_samples = len(self.parser.tree.children) if self.parser.tree else 0
        flt = getattr(self, "filter_var", None)
        flt = flt.get().strip().lower() if flt else ""

        def matches(node):
            if not flt:
                return True
            hay = (node.label + " " + " ".join(str(c) for c in node.cols)).lower()
            if flt in hay:
                return True
            return any(matches(c) for c in node.children)

        def add(parent, node, depth=0):
            if not matches(node):
                return
            opened = (depth == 0) or bool(flt) or (depth == 1 and n_samples <= 3)
            cols = tuple(node.cols) if node.cols else ("", "", "", "")
            iid = self.tree.insert(parent, "end", text=node.label, open=opened,
                                   values=cols)
            self.node_map[iid] = node
            for c in node.children:
                add(iid, c, depth + 1)

        if self.parser.tree:
            add("", self.parser.tree)

        if reset_display:
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
