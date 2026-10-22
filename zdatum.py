#!/usr/bin/env python
"""Check lidar Z values against the vertical datum the LAS header claims.

A LAS header can name a vertical coordinate system (a GeoTIFF key or a WKT
record) and a Z unit. Nothing in the file proves it. A tile can claim NAVD88
metres and hold feet, or claim orthometric heights and hold ellipsoid heights,
and every reader opens it without a word. The Z values are plausible numbers,
so the first sign of trouble is a surface that does not meet its neighbour or a
flood line that moved.

The sharp check needs something the file does not carry: a reference surface
the user trusts and can name the unit of. This tool takes survey control points
(a CSV) or a reference grid (an Esri ASCII raster), samples the ground points
of the tile against it, and asks which of two explanations fits the residuals:
the Z values are in the unit the header names, or they are in the other unit
(metres against feet). It then asks whether what is left is one steady shift,
which is the signature of a different datum, a different geoid model or an
ellipsoid height read as an orthometric height. It refuses to answer when the
reference cannot tell the two units apart, instead of guessing.

The tile is never modified. The only thing this tool can write is the residual
CSV named by --out, and only with --apply.

    python zdatum.py --self-test
    python zdatum.py tile.las --control ctl.csv --ref-unit m
    python zdatum.py tile.las --grid dem.asc --ref-unit ft
    python zdatum.py tile.las --control ctl.csv --ref-unit m --tolerance 0.2
    python zdatum.py tile.las --control ctl.csv --ref-unit m --assume-unit m
    python zdatum.py tile.las --control ctl.csv --ref-unit m --out r.csv --apply

Exit codes: 0 the Z values agree with the reference in the header's unit, 1 at
least one finding (a unit mismatch, a steady offset, scatter, a header that
contradicts itself or its own Z range), 2 a file could not be read or is a
format this tool does not read (LAZ), 3 the header names no vertical unit and
--assume-unit was not given, 4 not decided (too few pairs, no overlap, or a
reference that cannot tell metres from feet), 64 usage error, 70 an unexpected
crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import collections
import contextlib
import csv
import importlib.util
import io
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import traceback

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

FOOT = 0.3048
# The US survey foot is 1200/3937 metre. The international foot is exactly
# 0.3048 metre. NIST and NOAA retired the survey foot on 1 January 2023, but
# files made before then still carry it.
US_SURVEY_FOOT = 1200.0 / 3937.0
UNIT_FACTOR = {"m": 1.0, "ft": FOOT, "usft": US_SURVEY_FOOT}
UNIT_NAME = {"m": "metre", "ft": "international foot",
             "usft": "US survey foot"}
UNIT_CHOICES = ("m", "ft", "usft")

# EPSG unit-of-measure codes that a GeoTIFF unit key may hold.
EPSG_UNIT = {9001: "m", 9002: "ft", 9003: "usft"}

# The vertical coordinate systems this tool recognises by EPSG code, with the
# unit each one is defined in. A code that is not here still works when the
# header also names a unit. Checked against the EPSG registry in PROJ 9.8.
VERTICAL_CRS = {
    5703: ("NAVD88 height", "m"),
    6360: ("NAVD88 height (ftUS)", "usft"),
    8228: ("NAVD88 height (ft)", "ft"),
    5702: ("NGVD29 height (ftUS)", "usft"),
    7968: ("NGVD29 height (m)", "m"),
    5714: ("MSL height", "m"),
    5773: ("EGM96 height", "m"),
    3855: ("EGM2008 height", "m"),
}

# A median residual or a spread over this is a finding, in metres. It is a bias
# test, not an accuracy standard, and it is not taken from one. A tile over a
# canopy compared to a DEM needs a larger value.
DEFAULT_TOLERANCE = 0.30
# How far from a control point a lidar ground point may be, in the horizontal
# units of the tile. The tool does not convert horizontal units.
DEFAULT_RADIUS = 10.0
# Ground points needed around one control point before its median counts.
DEFAULT_MIN_POINTS = 3
# Grid mode compares about this many ground points, evenly spaced in the file.
DEFAULT_MAX_SAMPLES = 20000
# An offset over this many metres gets the ellipsoid-height hint. A heuristic.
DEFAULT_BIG_OFFSET = 5.0
DEFAULT_SAMPLE = 10
GROUND = 2
# Fewer pairs than this decide nothing: a median of three is a coin toss.
MIN_PAIRS = 5
CHUNK = 50000

# Verdicts.
AGREES = "AGREES"
UNIT = "UNIT"
OFFSET = "OFFSET"
UNIT_OFFSET = "UNIT_OFFSET"
SCATTER = "SCATTER"
UNDECIDED = "UNDECIDED"

# Public header block sizes by LAS minor version (ASPRS LAS 1.4 R15).
HEADER_BASE = 227
HEADER_14 = 375
# Smallest point record length that holds the fields each format defines.
RECORD_MIN = {0: 20, 1: 28, 2: 26, 3: 34, 4: 57, 5: 63,
              6: 30, 7: 36, 8: 38, 9: 59, 10: 67}
WKT_BIT = 16
VLR_HEADER = struct.Struct("<H16sHH32s")
EVLR_HEADER = struct.Struct("<H16sHQ32s")
KEY_VERTICAL = 4096
KEY_CITATION = 4097
KEY_UNITS = 4099
TAG_ASCII = 34737
OUT_COLUMNS = ("x", "y", "lidar_z", "reference_z", "points", "residual_m")

Pair = collections.namedtuple("Pair", "x y lidar ref n")
Test = collections.namedtuple("Test", "unit median spread")


class LasError(ValueError):
    """A file that is not a LAS this tool can read. Maps to exit 2."""


class GridError(ValueError):
    """An ASCII grid that is malformed. Maps to exit 2."""


class ColumnError(ValueError):
    """A CSV column the operator named that is not there. Maps to exit 64."""


class Header(object):
    """The fields of the public header block that this tool uses."""

    def __init__(self):
        self.major = 1
        self.minor = 0
        self.size = HEADER_BASE
        self.encoding = 0
        self.point_offset = 0
        self.n_vlr = 0
        self.fmt = 0
        self.rec_len = 0
        self.n_points = 0
        self.scale = (1.0, 1.0, 1.0)
        self.offset = (0.0, 0.0, 0.0)
        self.zmax = 0.0
        self.zmin = 0.0
        self.evlr_start = 0
        self.n_evlr = 0


class Claim(object):
    """What the header says about Z. unit is None when it says nothing."""

    def __init__(self, unit=None, name=None, code=None, source=None,
                 conflict=None):
        self.unit = unit
        self.name = name
        self.code = code
        self.source = source
        self.conflict = conflict


class Diagnosis(object):
    def __init__(self, kind, n, relief=0.0, tests=(), reason="", fit=None):
        self.kind = kind
        self.n = n
        self.relief = relief
        self.tests = list(tests)
        self.reason = reason
        self.fit = fit


class Report(object):
    pass


# ---------------------------------------------------------------- LAS header

def parse_header(raw):
    """Read the public header block from the first bytes of a file."""
    if len(raw) < 4 or raw[0:4] != b"LASF":
        raise LasError("not a LAS file: the first four bytes are %r, not "
                       "b'LASF'" % (bytes(raw[0:4]),))
    if len(raw) < HEADER_BASE:
        raise LasError("truncated: %d bytes is shorter than a LAS header"
                       % len(raw))
    h = Header()
    h.major, h.minor = raw[24], raw[25]
    if h.major != 1 or h.minor > 4:
        raise LasError("LAS version %d.%d is not one this tool reads (1.0 "
                       "to 1.4)" % (h.major, h.minor))
    h.encoding = struct.unpack_from("<H", raw, 6)[0]
    h.size = struct.unpack_from("<H", raw, 94)[0]
    h.point_offset, h.n_vlr = struct.unpack_from("<II", raw, 96)
    h.fmt = raw[104]
    h.rec_len = struct.unpack_from("<H", raw, 105)[0]
    h.n_points = struct.unpack_from("<I", raw, 107)[0]
    h.scale = struct.unpack_from("<3d", raw, 131)
    h.offset = struct.unpack_from("<3d", raw, 155)
    h.zmax, h.zmin = struct.unpack_from("<2d", raw, 211)
    if h.minor >= 4:
        if len(raw) < HEADER_14:
            raise LasError("truncated: a LAS 1.4 header is %d bytes, this "
                           "file has %d" % (HEADER_14, len(raw)))
        h.evlr_start, h.n_evlr, h.n_points = struct.unpack_from(
            "<QIQ", raw, 235)
    if h.fmt & 0x80:
        raise LasError("point format %d has bit 7 set, which marks LAZ "
                       "compression. This tool reads LAS only. Decompress "
                       "the tile first." % h.fmt)
    if h.fmt not in RECORD_MIN:
        raise LasError("point format %d is not defined (LAS defines 0 to 10)"
                       % h.fmt)
    if h.rec_len < RECORD_MIN[h.fmt]:
        raise LasError("point record length %d is shorter than the %d bytes "
                       "format %d needs" % (h.rec_len, RECORD_MIN[h.fmt],
                                            h.fmt))
    if h.scale[2] == 0.0:
        raise LasError("the Z scale factor is 0, so every Z would be the "
                       "offset")
    return h


def read_records(handle, start, count, wide):
    """The (user id, record id, payload) of a run of VLRs or EVLRs."""
    head = EVLR_HEADER if wide else VLR_HEADER
    out = []
    handle.seek(start)
    for _ in range(count):
        block = handle.read(head.size)
        if len(block) < head.size:
            raise LasError("truncated: a variable length record header ends "
                           "the file")
        _, user, record, length, _ = head.unpack(block)
        payload = handle.read(length)
        if len(payload) < length:
            raise LasError("truncated: a variable length record promises %d "
                           "bytes and the file ends first" % length)
        out.append((user.split(b"\0")[0].decode("latin-1"), record, payload))
    return out


def claim_from_geokeys(directory, ascii_params=b""):
    """The vertical claim in a GeoTIFF key directory (LAS record 34735)."""
    n_short = len(directory) // 2
    if n_short < 4:
        return Claim(source="GeoTIFF keys")
    shorts = struct.unpack("<%dH" % n_short, directory[:n_short * 2])
    keys = {}
    for i in range(min(shorts[3], (n_short - 4) // 4)):
        key, where, count, value = shorts[4 + 4 * i:8 + 4 * i]
        keys[key] = (where, count, value)
    code = keys.get(KEY_VERTICAL, (0, 1, None))[2]
    unit_key = keys.get(KEY_UNITS, (0, 1, None))[2]
    citation = None
    if KEY_CITATION in keys and keys[KEY_CITATION][0] == TAG_ASCII:
        _, count, start = keys[KEY_CITATION]
        citation = ascii_params[start:start + count].decode(
            "latin-1").strip("|\0 ") or None
    unit = EPSG_UNIT.get(unit_key)
    known = VERTICAL_CRS.get(code)
    claim = Claim(unit, citation, code, "GeoTIFF keys")
    if known:
        claim.name = known[0]
        if unit is not None and unit != known[1]:
            claim.conflict = (
                "the vertical CRS EPSG:%d is defined in %s, but the vertical "
                "unit key says %s" % (code, UNIT_NAME[known[1]],
                                      UNIT_NAME[unit]))
        claim.unit = known[1]
    return claim


def _bracket(text, start):
    """The text from text[start] (a '[') to its matching ']'."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def claim_from_wkt(text):
    """The vertical claim in a WKT CRS (LAS record 2112), WKT1 or WKT2."""
    found = re.search(r"\b(VERT_CS|VERTCRS|VERTICALCRS)\s*\[", text,
                      re.IGNORECASE)
    if not found:
        return Claim(source="WKT")
    body = _bracket(text, found.end() - 1)
    name = re.match(r'\[\s*"([^"]*)"', body)
    unit_match = re.search(r'\b(?:LENGTH)?UNIT\s*\[\s*"[^"]*"\s*,\s*'
                           r'([-+0-9.eE]+)', body, re.IGNORECASE)
    code_match = re.findall(r'\b(?:AUTHORITY|ID)\s*\[\s*"EPSG"\s*,\s*"?(\d+)',
                            body, re.IGNORECASE)
    unit = None
    if unit_match:
        factor = float(unit_match.group(1))
        for symbol, value in UNIT_FACTOR.items():
            if abs(factor - value) < 1e-9:
                unit = symbol
    return Claim(unit, name.group(1) if name else None,
                 int(code_match[-1]) if code_match else None, "WKT")


def read_claim(handle, h):
    """The vertical claim of a file, from the CRS record its header points to.

    The WKT bit of the global encoding decides: set means the WKT record,
    clear means the GeoTIFF keys. A file that holds only the other kind
    claims nothing, because a reader following the specification would find
    nothing either.
    """
    records = read_records(handle, h.size, h.n_vlr, False)
    if h.minor >= 4 and h.n_evlr:
        records += read_records(handle, h.evlr_start, h.n_evlr, True)
    wanted = 2112 if h.encoding & WKT_BIT else 34735
    payload = {rid: body for user, rid, body in records
               if user == "LASF_Projection"}
    if wanted not in payload:
        return Claim()
    if wanted == 2112:
        return claim_from_wkt(payload[2112].split(b"\0")[0].decode("utf-8",
                                                                  "replace"))
    return claim_from_geokeys(payload[34735], payload.get(TAG_ASCII, b""))


def header_range_issue(h, zmin, zmax):
    """None, or a sentence when the header's Z range is not the points'."""
    if h.zmin == 0.0 and h.zmax == 0.0:
        return None
    slack = abs(h.scale[2]) * 1.5 + 1e-9
    if abs(h.zmin - zmin) > slack or abs(h.zmax - zmax) > slack:
        return ("the header records a Z range of %.4f to %.4f, but the "
                "points span %.4f to %.4f. A tool rescaled the Z values and "
                "left the header behind, or the other way round."
                % (h.zmin, h.zmax, zmin, zmax))
    return None


# ---------------------------------------------------------------- point scan

def point_struct(h):
    """A struct that reads X, Y, Z and the classification byte of a record."""
    where = 15 if h.fmt < 6 else 16
    return struct.Struct("<iii%dxB%dx" % (where - 12, h.rec_len - where - 1))


def scan(handle, h, sink, ground_class=GROUND, any_class=False, stride=1):
    """One pass over every point record.

    Calls sink.add(x, y, z) for each selected point and returns
    (zmin, zmax, n_ground) over all points. A point is selected when its
    class is the ground class, or always with any_class. With stride above 1
    every stride-th point of the file is considered.
    """
    handle.seek(0, 2)
    room = (handle.tell() - h.point_offset) // h.rec_len
    if room < h.n_points:
        raise LasError("truncated: the header counts %d points and the file "
                       "holds %d" % (h.n_points, room))
    layout = point_struct(h)
    mask = 0x1F if h.fmt < 6 else 0xFF
    sx, sy, sz = h.scale
    ox, oy, oz = h.offset
    lo, hi, ground = None, None, 0
    handle.seek(h.point_offset)
    done = 0
    while done < h.n_points:
        take = min(CHUNK, h.n_points - done)
        raw = handle.read(take * h.rec_len)
        recs = list(layout.iter_unpack(raw))
        zs = [r[2] for r in recs]
        lo = min(zs) if lo is None else min(lo, min(zs))
        hi = max(zs) if hi is None else max(hi, max(zs))
        for i in range((-done) % stride, len(recs), stride):
            ix, iy, iz, cls = recs[i]
            if any_class or (cls & mask) == ground_class:
                sink.add(ix * sx + ox, iy * sy + oy, iz * sz + oz)
        ground += sum(1 for r in recs if (r[3] & mask) == ground_class)
        done += take
    if lo is None:
        return 0.0, 0.0, 0
    ends = (lo * sz + oz, hi * sz + oz)
    return min(ends), max(ends), ground


# ----------------------------------------------------------------- reference

def to_number(text):
    """A finite float from a CSV cell, or None."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


class Controls(object):
    """Survey control points, and the lidar ground heights found near each."""

    def __init__(self, points, radius):
        self.points = points
        self.radius = radius
        self.cells = {}
        self.heights = [[] for _ in points]
        for i, (x, y, _) in enumerate(points):
            self.cells.setdefault(self._cell(x, y), []).append(i)

    def _cell(self, x, y):
        return (int(math.floor(x / self.radius)),
                int(math.floor(y / self.radius)))

    def add(self, x, y, z):
        cx, cy = self._cell(x, y)
        r2 = self.radius * self.radius
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for i in self.cells.get((gx, gy), ()):
                    px, py = self.points[i][0], self.points[i][1]
                    if (x - px) ** 2 + (y - py) ** 2 <= r2:
                        self.heights[i].append(z)

    def pairs(self, min_points):
        """(pairs, count of control points with too few ground points)."""
        out, thin = [], 0
        for (x, y, ref), zs in zip(self.points, self.heights):
            if len(zs) >= min_points:
                out.append(Pair(x, y, median(zs), ref, len(zs)))
            else:
                thin += 1
        return out, thin


GRID_KEYS = ("ncols", "nrows", "xllcorner", "yllcorner", "xllcenter",
             "yllcenter", "cellsize", "nodata_value")


class Grid(object):
    """An Esri ASCII raster, sampled bilinearly between cell centres."""

    def __init__(self, ncols, nrows, x0, y_top, cellsize, rows):
        self.ncols, self.nrows = ncols, nrows
        self.x0, self.y_top, self.cellsize = x0, y_top, cellsize
        self.rows = rows
        self.pairs_found = []
        self.outside = 0

    def value_at(self, x, y):
        fx = (x - self.x0) / self.cellsize
        fy = (self.y_top - y) / self.cellsize
        if fx < 0 or fy < 0 or fx > self.ncols - 1 or fy > self.nrows - 1:
            return None
        c0, r0 = int(fx), int(fy)
        c1, r1 = min(c0 + 1, self.ncols - 1), min(r0 + 1, self.nrows - 1)
        tx, ty = fx - c0, fy - r0
        corners = (self.rows[r0][c0], self.rows[r0][c1],
                   self.rows[r1][c0], self.rows[r1][c1])
        if None in corners:
            return None
        top = corners[0] * (1 - tx) + corners[1] * tx
        bottom = corners[2] * (1 - tx) + corners[3] * tx
        return top * (1 - ty) + bottom * ty

    def add(self, x, y, z):
        ref = self.value_at(x, y)
        if ref is None:
            self.outside += 1
        else:
            self.pairs_found.append(Pair(x, y, z, ref, 1))

    def pairs(self, min_points):
        """(pairs, count of sampled ground points with no reference value)."""
        return self.pairs_found, self.outside


def parse_grid(text):
    """Build a Grid from the text of an Esri ASCII raster."""
    tokens = text.split()
    head, i = {}, 0
    while i + 1 < len(tokens) and tokens[i].lower() in GRID_KEYS:
        head[tokens[i].lower()] = tokens[i + 1]
        i += 2
    missing = [k for k in ("ncols", "nrows", "cellsize") if k not in head]
    for axis in ("x", "y"):
        if axis + "llcorner" not in head and axis + "llcenter" not in head:
            missing.append(axis + "llcorner")
    if missing:
        raise GridError("the ASCII grid header lacks: %s"
                         % ", ".join(missing))
    try:
        ncols, nrows = int(head["ncols"]), int(head["nrows"])
        cell = float(head["cellsize"])
        nodata = float(head.get("nodata_value", "-9999"))
        xll = float(head.get("xllcenter", head.get("xllcorner")))
        yll = float(head.get("yllcenter", head.get("yllcorner")))
    except ValueError:
        raise GridError("the ASCII grid header holds a value that is not a "
                         "number")
    if ncols < 1 or nrows < 1 or not cell > 0:
        raise GridError("the ASCII grid needs ncols and nrows of 1 or more "
                         "and a cellsize above 0")
    data = tokens[i:]
    if len(data) != ncols * nrows:
        raise GridError("the ASCII grid holds %d cell values, and ncols "
                         "times nrows is %d" % (len(data), ncols * nrows))
    values = []
    for token in data:
        try:
            number = float(token)
        except ValueError:
            raise GridError("the ASCII grid holds %r where a cell value "
                            "belongs" % token)
        values.append(None if not math.isfinite(number) or number == nodata
                      else number)
    rows = [values[r * ncols:(r + 1) * ncols] for r in range(nrows)]
    # A corner origin puts the first cell centre half a cell in.
    shift = 0.0 if "xllcenter" in head else cell / 2.0
    shift_y = 0.0 if "yllcenter" in head else cell / 2.0
    return Grid(ncols, nrows, xll + shift, yll + shift_y + (nrows - 1) * cell,
                cell, rows)


def read_text(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        return handle.read()


def read_controls(path, xf, yf, zf):
    """(control points, count of rows skipped) from a CSV."""
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        absent = [f for f in (xf, yf, zf) if f not in header]
        if absent:
            raise ColumnError(
                "%s: columns not in the CSV: %s. It has: %s"
                % (path, ", ".join(absent),
                   ", ".join(header) if header else "(no columns at all)"))
        points, skipped = [], 0
        for row in reader:
            x, y, z = (to_number(row.get(f)) for f in (xf, yf, zf))
            if x is None or y is None or z is None:
                skipped += 1
            else:
                points.append((x, y, z))
    return points, skipped


# ------------------------------------------------------------------ decision

def median(values):
    if not values:
        raise ValueError("the median of nothing")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def residual_stats(values):
    """Median and spread (median absolute deviation) of a list."""
    m = median(values)
    return m, median([abs(v - m) for v in values])


def other_unit(unit):
    """The unit a mislabelled tile is most likely really in."""
    return "m" if unit in ("ft", "usft") else "ft"


def diagnose(pairs, claimed, ref_unit, tolerance=DEFAULT_TOLERANCE):
    """Decide what the residuals say. Pure: no I/O.

    Each pair holds the lidar Z as stored and the reference Z in its own unit.
    Two explanations are tried: the stored Z is in the claimed unit, or it is
    in the other unit. For each, the residual (lidar minus reference, in
    metres) has a median, which is the bias, and a spread. The bias counts only
    when the spread is small, because a bias of scattered numbers is noise.
    """
    if len(pairs) < MIN_PAIRS:
        return Diagnosis(UNDECIDED, len(pairs), reason=(
            "only %d pair(s) were measured and at least %d are needed. A "
            "median of fewer says nothing." % (len(pairs), MIN_PAIRS)))
    refs = [p.ref * UNIT_FACTOR[ref_unit] for p in pairs]
    relief = max(refs) - min(refs)
    tests = []
    for unit in (claimed, other_unit(claimed)):
        d = [p.lidar * UNIT_FACTOR[unit] - r for p, r in zip(pairs, refs)]
        tests.append(Test(unit, *residual_stats(d)))
    steady = [t for t in tests if t.spread <= tolerance]
    fits = [t for t in steady if abs(t.median) <= tolerance]
    if len(fits) == 2 or (not fits and len(steady) == 2):
        return Diagnosis(UNDECIDED, len(pairs), relief, tests, (
            "the reference spans only %.3f m, too little to tell %s from %s. "
            "Both explanations fit the residuals equally well. Use control "
            "that spans more relief." % (
                relief, UNIT_NAME[tests[0].unit], UNIT_NAME[tests[1].unit])))
    if fits:
        chosen = fits[0]
        kind = AGREES if chosen is tests[0] else UNIT
    elif steady:
        chosen = steady[0]
        kind = OFFSET if chosen is tests[0] else UNIT_OFFSET
    else:
        return Diagnosis(SCATTER, len(pairs), relief, tests)
    return Diagnosis(kind, len(pairs), relief, tests, fit=chosen)


def residuals_under(pairs, ref_unit, unit):
    """(pair, residual in metres) for each pair if the lidar Z is in unit."""
    return [(p, p.lidar * UNIT_FACTOR[unit] - p.ref * UNIT_FACTOR[ref_unit])
            for p in pairs]


# -------------------------------------------------------------------- report

def signed(value):
    return "%+.4f m" % value


def gate(report):
    """Exit status for a finished report."""
    d = report.diagnosis
    if report.findings or d.kind in (UNIT, OFFSET, UNIT_OFFSET, SCATTER):
        return 1
    return 4 if d.kind == UNDECIDED else 0


def describe(report, sample=DEFAULT_SAMPLE):
    """The lines a person reads. Pure."""
    h, c, d = report.header, report.claim, report.diagnosis
    out = ["%s: LAS %d.%d, point format %d, %d point(s), %d ground"
           % (report.tile, h.major, h.minor, h.fmt, h.n_points,
              report.n_ground)]
    if c.name or c.code or c.unit:
        out.append("header claims: %s%s, unit %s (from the %s)"
                   % (c.name or "unnamed vertical CRS",
                      " (EPSG:%d)" % c.code if c.code else "",
                      UNIT_NAME[c.unit] if c.unit else "not stated",
                      c.source))
    else:
        out.append("header claims: no vertical CRS")
    if report.assumed:
        out.append("unit: %s, ASSUMED with --assume-unit. The header did not "
                   "say." % UNIT_NAME[report.unit])
    out.append("reference: %s, %s, declared %s"
               % (report.reference, report.reference_note,
                  UNIT_NAME[report.ref_unit]))
    out.append("pairs: %d compared, %d skipped for lack of a reference value "
               "or of enough ground points. Tolerance %.3f m."
               % (d.n, report.thin, report.tolerance))
    for line in report.notes:
        out.append(line)
    out.append("")
    if d.tests:
        out.append("relief of the reference: %.3f m" % d.relief)
        for t in d.tests:
            label = "stored Z is in the header's unit" if (
                t is d.tests[0]) else "stored Z is in the other unit"
            out.append("  if %-32s (%s): median %s, spread %.4f m"
                       % (label, UNIT_NAME[t.unit], signed(t.median),
                          t.spread))
    for finding in report.findings:
        out.append("FINDING   %s" % finding)
    if d.tests and d.kind != AGREES and report.worst and sample > 0:
        out.append("")
        out.append("pairs furthest from the reference, if the header is "
                   "right:")
        for p, r in report.worst[:sample]:
            out.append("  x %.3f y %.3f  lidar %.4f  reference %.4f  "
                       "%s  (%d point(s))"
                       % (p.x, p.y, p.lidar, p.ref, signed(r), p.n))
    out.append("")
    out.append(verdict_line(report))
    return out


def verdict_line(report):
    d, tol = report.diagnosis, report.tolerance
    c_unit = UNIT_NAME[report.unit]
    named = "assumed" if report.assumed else "the header names"
    fit = d.fit
    if d.kind == AGREES:
        text = ("AGREES. The Z values match the reference to within %.3f m "
                "in the %s (%s)." % (tol, c_unit, named))
    elif d.kind == UNIT:
        text = ("UNIT. The unit is %s (%s), but the Z values fit the "
                "reference as %s (median %s, spread %.4f m). Do not use the "
                "Z values until the header or the data is fixed."
                % (c_unit, named, UNIT_NAME[fit.unit], signed(fit.median),
                   fit.spread))
    elif d.kind in (OFFSET, UNIT_OFFSET):
        text = ("%s. The Z values sit a steady %s from the reference (spread "
                "%.4f m)" % (d.kind, signed(fit.median), fit.spread))
        if d.kind == UNIT_OFFSET:
            text += (", and only if they are read as %s, not the %s (%s)"
                     % (UNIT_NAME[fit.unit], c_unit, named))
        text += (". A shift this steady is what a different vertical datum, "
                 "a different geoid model, or ellipsoid heights read as "
                 "orthometric heights look like. This tool cannot tell "
                 "them apart.")
        if abs(fit.median) > report.big_offset:
            text += (" It is over %.1f m, larger than a datum realisation "
                     "usually moves a point, so test the ellipsoid-height "
                     "explanation first." % report.big_offset)
    elif d.kind == SCATTER:
        text = ("SCATTER. Neither unit leaves the residuals within %.3f m "
                "of one shift (spreads %s). The reference may not cover this "
                "tile, may be in another horizontal CRS, or may sit on "
                "different ground than the lidar."
                % (tol, " and ".join("%.4f m" % t.spread for t in d.tests)))
    else:
        text = "NOT DECIDED. %s" % d.reason
    if report.findings and d.kind in (AGREES, UNDECIDED):
        text += " The header findings above still stand."
    return "VERDICT: " + text


def residual_rows(report):
    unit = report.unit
    return [{"x": p.x, "y": p.y, "lidar_z": p.lidar, "reference_z": p.ref,
             "points": p.n, "residual_m": r}
            for p, r in residuals_under(report.pairs, report.ref_unit, unit)]


def build_report(tile, header, claim, unit, assumed, ref_name, ref_note,
                 ref_unit, pairs, thin, n_ground, range_issue, tolerance,
                 big_offset):
    """Join the header facts and the diagnosis into one Report."""
    r = Report()
    r.tile, r.header, r.claim, r.unit, r.assumed = (tile, header, claim, unit,
                                                     assumed)
    r.reference, r.reference_note, r.ref_unit = ref_name, ref_note, ref_unit
    r.pairs, r.thin, r.n_ground = pairs, thin, n_ground
    r.tolerance, r.big_offset = tolerance, big_offset
    r.notes = []
    r.findings = [f for f in (claim.conflict, range_issue) if f]
    r.diagnosis = diagnose(pairs, unit, ref_unit, tolerance)
    r.worst = sorted(residuals_under(pairs, ref_unit, unit),
                     key=lambda item: -abs(item[1]))
    return r


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="zdatum.py",
        description="Check lidar Z values against the vertical datum the LAS "
                    "header claims, using a reference surface you supply.",
        epilog="The tile is never modified. Nothing is written without "
               "--apply.",
        allow_abbrev=False,
    )
    ap.add_argument("tile", nargs="?", metavar="TILE",
                    help="the LAS file to check (LAZ is refused)")
    ap.add_argument("--control", help="reference: a CSV of survey control "
                                      "points with X, Y and Z columns")
    ap.add_argument("--grid", help="reference: an Esri ASCII raster (.asc)")
    ap.add_argument("--ref-unit", dest="ref_unit", choices=UNIT_CHOICES,
                    help="the Z unit of the reference. Required, never "
                         "guessed.")
    ap.add_argument("--assume-unit", dest="assume_unit",
                    choices=UNIT_CHOICES,
                    help="the tile's Z unit, used only when the header names "
                         "none")
    ap.add_argument("--x-field", dest="x_field", default="X",
                    help="control CSV X column (default X)")
    ap.add_argument("--y-field", dest="y_field", default="Y",
                    help="control CSV Y column (default Y)")
    ap.add_argument("--z-field", dest="z_field", default="Z",
                    help="control CSV Z column (default Z)")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                    help="metres a median residual or its spread may reach "
                         "(default %.2f)" % DEFAULT_TOLERANCE)
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS,
                    help="how far from a control point a ground point may be, "
                         "in the tile's horizontal units (default %.0f)"
                         % DEFAULT_RADIUS)
    ap.add_argument("--min-points", dest="min_points", type=int,
                    default=DEFAULT_MIN_POINTS,
                    help="ground points needed near a control point "
                         "(default %d)" % DEFAULT_MIN_POINTS)
    ap.add_argument("--max-samples", dest="max_samples", type=int,
                    default=DEFAULT_MAX_SAMPLES,
                    help="grid mode: about this many ground points are "
                         "compared (default %d)" % DEFAULT_MAX_SAMPLES)
    ap.add_argument("--ground-class", dest="ground_class", type=int,
                    default=GROUND,
                    help="the classification code that means bare earth "
                         "(default %d)" % GROUND)
    ap.add_argument("--any-class", dest="any_class", action="store_true",
                    help="compare points of every class. Vegetation and "
                         "buildings then bias the result high.")
    ap.add_argument("--big-offset", dest="big_offset", type=float,
                    default=DEFAULT_BIG_OFFSET,
                    help="metres of steady offset above which the "
                         "ellipsoid-height hint is printed (default %.0f)"
                         % DEFAULT_BIG_OFFSET)
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="how many worst pairs to list (default %d)"
                         % DEFAULT_SAMPLE)
    ap.add_argument("--out", help="path for the residual CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
        os.path.realpath(b))


def _usage_problem(args):
    """The first usage error in args, or None."""
    if not args.tile:
        return ("a LAS tile is required. Use --self-test to verify the tool "
                "without one.")
    if bool(args.control) == bool(args.grid):
        return "name exactly one reference: --control or --grid."
    if not args.ref_unit:
        return ("--ref-unit is required. The unit of the reference is never "
                "guessed.")
    if not args.tolerance > 0:
        return "--tolerance must be above 0."
    if args.radius <= 0:
        return "--radius must be above 0."
    if args.min_points < 1 or args.max_samples < 1 or args.sample < 0:
        return ("--min-points and --max-samples must be 1 or more, and "
                "--sample cannot be negative.")
    if args.apply and not args.out:
        return "--apply needs --out."
    if args.out:
        for path in (args.tile, args.control, args.grid):
            if path and _same_file(args.out, path):
                return ("--out %s is also an input. Name a new file."
                        % args.out)
    return None


def measure(args, handle, h):
    """Scan the tile against the reference. Returns the sink it filled."""
    if args.control:
        points, skipped = read_controls(args.control, args.x_field,
                                        args.y_field, args.z_field)
        sink = Controls(points, args.radius)
        sink.note = "%d control point(s) read, %d row(s) skipped" % (
            len(points), skipped)
        stride = 1
    else:
        sink = parse_grid(read_text(args.grid))
        sink.note = "%d x %d grid, cell %g" % (sink.ncols, sink.nrows,
                                              sink.cellsize)
        stride = max(1, h.n_points // args.max_samples)
    sink.stats = scan(handle, h, sink, args.ground_class, args.any_class,
                      stride)
    return sink


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a finding'."""
    _tolerate_odd_characters()
    try:
        return _run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the data." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        return 70


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file
        # could not be read. A mistyped --ap is a usage error, so it gets 64.
        return 64 if exc.code == 2 else exc.code

    if args.self_test:
        return self_test()

    problem = _usage_problem(args)
    if problem:
        print("error: %s" % problem, file=sys.stderr)
        return 64

    try:
        with open(args.tile, "rb") as handle:
            h = parse_header(handle.read(HEADER_14))
            claim = read_claim(handle, h)
            unit = claim.unit or args.assume_unit
            if unit is None:
                print("%s: LAS %d.%d, point format %d, %d point(s)"
                      % (args.tile, h.major, h.minor, h.fmt, h.n_points))
                print("VERDICT: REFUSED. The header names no vertical unit, "
                      "so there is no claim to check. Name the unit with "
                      "--assume-unit if you know it. The datum itself "
                      "cannot be assumed.")
                return 3
            sink = measure(args, handle, h)
    except (LasError, GridError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except ColumnError as exc:
        # A column name the operator got wrong is a usage error, and a file
        # that could not be read is not. Only the exit code tells them apart.
        print("error: %s" % exc, file=sys.stderr)
        return 64
    except (IOError, OSError, csv.Error) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    zmin, zmax, n_ground = sink.stats
    pairs, thin = sink.pairs(args.min_points)
    report = build_report(
        args.tile, h, claim, unit, claim.unit is None,
        args.control or args.grid, sink.note, args.ref_unit, pairs, thin,
        n_ground, header_range_issue(h, zmin, zmax), args.tolerance,
        args.big_offset)
    if claim.unit and args.assume_unit:
        report.notes.append("--assume-unit ignored: the header names the "
                            "unit.")
    if not args.any_class and n_ground == 0:
        report.notes.append("no point has class %d, so nothing was compared. "
                            "Use --any-class to compare every point."
                            % args.ground_class)
    for line in describe(report, args.sample):
        print(line)

    if args.out:
        if args.apply:
            try:
                with open(args.out, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(OUT_COLUMNS))
                    writer.writeheader()
                    for row in residual_rows(report):
                        writer.writerow(row)
            except (IOError, OSError, csv.Error) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.out)
        else:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
    return gate(report)


# SELF-TEST-BEGIN
def self_test():
    return 0
# SELF-TEST-END


if __name__ == "__main__":
    sys.exit(main())
