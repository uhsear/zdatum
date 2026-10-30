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
least one finding (a unit mismatch, a steady offset, scatter, a tile that is
part right and part wrong, a header that contradicts itself or its own Z
range; a header finding makes it 1 whatever the verdict), 2 a file could not be read or is a
format this tool does not read (LAZ), 3 the header names no vertical unit and
--assume-unit was not given, 4 not decided (too few pairs, no overlap, or a
reference that cannot tell metres from feet), 64 usage error, 70 an unexpected
crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import base64
import collections
import contextlib
import csv
import hashlib
import io
import math
import os
import re
import runpy
import shutil
import struct
import sys
import tempfile
import traceback
import zlib

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

FOOT = 0.3048
# The US survey foot is 1200/3937 metre. The international foot is exactly
# 0.3048 metre. NIST and NOAA NGS deprecated the survey foot as of 31 December
# 2022, but files made before then still carry it.
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
# Share of the pairs that must lie within the tolerance of the fitted shift. A
# median and a spread both ignore a minority, so without this a tile that is
# half wrong still passes.
MIN_SHARE = 0.9
CHUNK = 50000
# A CRS record (GeoTIFF keys or WKT) larger than this is not a CRS. A real one
# is a few kilobytes. The cap stops a forged length from allocating memory.
MAX_CRS_BYTES = 1 << 20

# Verdicts.
AGREES = "AGREES"
UNIT = "UNIT"
OFFSET = "OFFSET"
UNIT_OFFSET = "UNIT_OFFSET"
SCATTER = "SCATTER"
MIXED = "MIXED"
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
Test = collections.namedtuple("Test", "unit median spread within")


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
    if h.size < HEADER_BASE:
        raise LasError("the header says it is %d bytes, shorter than the %d "
                       "bytes every LAS header has" % (h.size, HEADER_BASE))
    if not all(math.isfinite(v) for v in h.scale + h.offset):
        raise LasError("a scale factor or offset in the header is not a "
                       "finite number")
    if h.scale[2] == 0.0:
        raise LasError("the Z scale factor is 0, so every Z would be the "
                       "offset")
    return h


def read_records(handle, start, count, wide):
    """The (user id, record id, payload) of a run of VLRs or EVLRs.

    The count and every length come from the file. A forged count cannot loop
    long, because each pass consumes bytes and the first header past the end
    of the file raises. A length is checked against the bytes that remain
    before it is read. Only LASF_Projection payloads are read, up to
    MAX_CRS_BYTES. Any other payload is stepped over and returned as b"".
    """
    head = EVLR_HEADER if wide else VLR_HEADER
    handle.seek(0, 2)
    size = handle.tell()
    out = []
    handle.seek(start)
    for _ in range(count):
        block = handle.read(head.size)
        if len(block) < head.size:
            raise LasError("truncated: a variable length record header ends "
                           "the file")
        _, user, record, length, _ = head.unpack(block)
        user = user.split(b"\0")[0].decode("latin-1")
        if length > size - handle.tell():
            raise LasError("truncated: a variable length record promises %d "
                           "bytes and the file ends first" % length)
        if user != "LASF_Projection":
            handle.seek(length, 1)
            out.append((user, record, b""))
            continue
        if length > MAX_CRS_BYTES:
            raise LasError("a coordinate system record claims %d bytes, more "
                           "than the %d this tool will read"
                           % (length, MAX_CRS_BYTES))
        out.append((user, record, handle.read(length)))
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
    factor = to_number(unit_match.group(1)) if unit_match else None
    if factor is not None:
        for symbol, value in UNIT_FACTOR.items():
            if abs(factor - value) < 1e-9:
                unit = symbol
    return Claim(unit, name.group(1) if name else None,
                 int(code_match[-1]) if code_match else None, "WKT")


def _claim_of(rid, body, ascii_params):
    if rid == 2112:
        return claim_from_wkt(body.split(b"\0")[0].decode("utf-8", "replace"))
    return claim_from_geokeys(body, ascii_params)


def read_claim(handle, h):
    """The vertical claim of a file, from the CRS record its header points to.

    The WKT bit of the global encoding decides: set means the WKT record,
    clear means the GeoTIFF keys. Point formats 6 to 10 always mean WKT, and
    LAS 1.4 calls a clear bit there an error. A file that holds only the other
    kind of record claims nothing, because a reader following the
    specification would find nothing either. The specification also calls a
    second record of one kind an error, so a duplicate, or a pair of records
    that name different units, is recorded as a conflict on the claim.
    """
    records = read_records(handle, h.size, h.n_vlr, False)
    if h.minor >= 4 and h.n_evlr:
        records += read_records(handle, h.evlr_start, h.n_evlr, True)
    found = {}
    for user, rid, body in records:
        if user == "LASF_Projection":
            found.setdefault(rid, []).append(body)
    bit = bool(h.encoding & WKT_BIT)
    wkt = bit or h.fmt >= 6
    wanted = 2112 if wkt else 34735
    other = 34735 if wkt else 2112
    texts = found.get(TAG_ASCII, [b""])[0]
    notes = []
    if h.fmt >= 6 and not bit:
        notes.append("the WKT bit of the global encoding is clear for point "
                     "format %d, which the LAS 1.4 specification calls an "
                     "error. The WKT record was read." % h.fmt)
    if wanted not in found:
        claim = Claim()
        claim.conflict = " ".join(notes) or None
        return claim
    claims = [_claim_of(wanted, b, texts) for b in found[wanted]]
    claim = claims[0]
    if len(claims) > 1:
        notes.append("the file holds %d %s records, and the specification "
                     "calls more than one an error. The first was read."
                     % (len(claims), "WKT" if wkt else "GeoTIFF key"))
        if len(set(c.unit for c in claims)) > 1:
            notes.append("They name different units.")
    if other in found:
        rival = _claim_of(other, found[other][0], texts)
        if rival.unit and claim.unit and rival.unit != claim.unit:
            notes.append("the file also holds a %s record that names %s, "
                         "while the one read names %s."
                         % ("GeoTIFF key" if wkt else "WKT",
                            UNIT_NAME[rival.unit], UNIT_NAME[claim.unit]))
    if claim.conflict:
        notes.append(claim.conflict)
    claim.conflict = " ".join(notes) or None
    return claim


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


def header_range_unchecked(h, zmin, zmax):
    """None, or a sentence when the header's Z range cannot be cross-checked.

    A header range of 0 to 0 is what a writer leaves when it never filled the
    field, so it proves nothing about the points.
    """
    if h.zmin == 0.0 and h.zmax == 0.0 and (zmin, zmax) != (0.0, 0.0):
        return ("the header records a Z range of 0 to 0, so it could not be "
                "cross-checked against the points (%.4f to %.4f)."
                % (zmin, zmax))
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
    if ("xllcenter" in head) != ("yllcenter" in head):
        raise GridError("the ASCII grid gives one origin as a cell centre "
                        "and the other as a corner. Esri requires both to "
                        "match.")
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


def _csv_encoding(path):
    """utf-16 when the file starts with a UTF-16 byte order mark, else utf-8.

    Excel's "Unicode Text" and PowerShell's -Encoding Unicode write UTF-16.
    """
    with open(path, "rb") as handle:
        mark = handle.read(2)
    return "utf-16" if mark in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"


def read_controls(path, xf, yf, zf):
    """(control points, count of rows skipped) from a CSV.

    X, Y and Z are ASCII numbers, so a byte that is not valid text (a cp1252
    degree sign in a description column) is replaced, not fatal.
    """
    with open(path, "r", encoding=_csv_encoding(path), errors="replace",
              newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        absent = [f for f in (xf, yf, zf) if f not in header]
        if absent:
            raise ColumnError(
                "%s: columns not in the CSV: %s. It has: %s. Column names are "
                "case sensitive. Name them with --x-field, --y-field and "
                "--z-field." % (path, ", ".join(absent),
                                ", ".join(header) if header
                                else "(no columns at all)"))
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
    It counts only when MIN_SHARE of the pairs lie within the tolerance of it
    too, because a median and a spread both ignore a minority.
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
        m, spread = residual_stats(d)
        tests.append(Test(unit, m, spread,
                          sum(1 for v in d if abs(v - m) <= tolerance)))
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
    if chosen.within < MIN_SHARE * len(pairs):
        kind = MIXED
    return Diagnosis(kind, len(pairs), relief, tests, fit=chosen)


def residuals_under(pairs, ref_unit, unit):
    """(pair, residual in metres) for each pair if the lidar Z is in unit."""
    return [(p, p.lidar * UNIT_FACTOR[unit] - p.ref * UNIT_FACTOR[ref_unit])
            for p in pairs]


# -------------------------------------------------------------------- report

def signed(value):
    """A signed length that never prints a negative zero."""
    return "%+.4f m" % (0.0 if abs(value) < 5e-5 else value)


def gate(report):
    """Exit status for a finished report."""
    d = report.diagnosis
    if report.findings or d.kind in (UNIT, OFFSET, UNIT_OFFSET, SCATTER,
                                    MIXED):
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
    named = "assumed" if report.assumed else "named by the header"
    fit = d.fit
    if d.kind == AGREES:
        text = ("AGREES. The Z values match the reference to within %.3f m "
                "read as %s (%s)." % (tol, c_unit, named))
    elif d.kind == UNIT:
        text = ("UNIT. The unit is %s (%s), but the Z values fit the "
                "reference as %s (median %s, spread %.4f m). Do not use the "
                "Z values until the header or the data is fixed."
                % (c_unit, named, UNIT_NAME[fit.unit], signed(fit.median),
                   fit.spread))
        if fit.unit != "m":
            text += (" The international foot and the US survey foot differ "
                     "by 2 parts per million, too little for this check to "
                     "say which one the data holds.")
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
    elif d.kind == MIXED:
        text = ("MIXED. Only %d of %d pairs (%.0f percent) lie within %.3f m "
                "of the one shift that fits best (%s, median %s), and at "
                "least %.0f percent must. Part of the tile disagrees with "
                "the rest: two deliveries merged, two units in one file, "
                "NoData read as heights, or a reference with bad cells."
                % (fit.within, d.n, 100.0 * fit.within / d.n, tol,
                   UNIT_NAME[fit.unit], signed(fit.median),
                   100.0 * MIN_SHARE))
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
    return [{"x": "%.3f" % p.x, "y": "%.3f" % p.y,
             "lidar_z": "%.4f" % p.lidar, "reference_z": "%.4f" % p.ref,
             "points": p.n, "residual_m": "%.4f" % r}
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
    """True when a and b are one file: a hard link or a case-folded spelling
    counts, which a comparison of the paths would miss."""
    if os.path.exists(a) and os.path.exists(b):
        return os.path.samefile(a, b)
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
    if not (math.isfinite(args.tolerance) and args.tolerance > 0):
        return "--tolerance must be a number above 0."
    if not (math.isfinite(args.radius) and args.radius > 0):
        return "--radius must be a number above 0."
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
                if claim.conflict:
                    print("FINDING   %s" % claim.conflict)
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
    unchecked = header_range_unchecked(h, zmin, zmax)
    if unchecked:
        report.notes.append("note: %s" % unchecked)
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
# ----------------------------------------------------------------- self-test

# Two files written by laspy 2.7.0 (zlib, then base64), kept as bytes a
# third-party writer produced so the reader is not tested only against the
# writer in this file. WKT14: LAS 1.4, point format 6, the WKT bit set, one
# vertical CRS record for EPSG:5703. KEYS12: LAS 1.2, point format 1, a
# GeoTIFF key directory with no vertical key. Both hold three points at X
# 1001..1003, Y 2001..2003, Z 10.5, 11.25 and 12.0, classes 2, 2 and 1.
_LASPY_WKT14 = (
    "eNrFU89u0zAYb9kEHDlw4oCsSIhNyqp1bOumHmjWdm1Y25Sm7YaiCnmJSUwTu7Kd"
    "dh1X7jwCb4B4DF6DKw+B+Nxkgk7bGSvO9/P38/fXdsdyTwuFJ4Xbo7jpDNvNQeH+"
    "EWM5W6K9UqW0eyf/svjr0aL4exN8werh83sdfXr6rfXz+vPr23JF1r7UMvm1tmZ0"
    "kevbuezkfONm31YuX9TuirlR+N+jA41/3xf8I/EV5axQe/x2w2nV0VBgJj9wkWCt"
    "RgPicxHkNuPmYFgfuJ7Rs8aNoyMUERpGyjDHDWs46oKaCxUhKyGC+pihMREKQIwa"
    "WKUJKh8fHRkTs+5685wwyxPTurDBYSjwnKrljiAxViTIPaOt9rZhpjOz0+y1hu1R"
    "zx56RkKUIAaYTsyRa7Wanlt3+k3PaBEeELk0EWEhZQRyYCGSqZgT0Ck+4xBjFlEf"
    "JXg2A64EuViDpuUZXXJFfY52EGcy4oKU0IhRnYWrIBmJtiDONtB1pzdyEWYBsuDy"
    "TfFfC0CgusQJruYcSEGvOVuBKbQUyyqq45hCaxkFbZ3HXOCAa8SYPgQ/VVXUgAYs"
    "sCBVdAo8DWAnFCZCbWIHOIL9dhxTxin4s1lAsQ5h8wX8z/IwZ4Sp1J8uq6jDUyqz"
    "LV0MTdFCLGMoQSMpsR+lkigFRl3qRzTETCPIR3KljaiU+oOGZQueCo04UyunPXIp"
    "smp7ZI6DlVygNk5mMqK6CL18Q4Qkywxnrc7wOy6mgFZ3po4Fh6rwzbqBp6sEnIjC"
    "bmcaQ+W6uY4gIYcc+4QxuYzneNXLQQRHj2yZFebydM1jtr7xOCS6OkmIhle6XSOF"
    "o6q+rAmUBYBCt1duzzFUwUKlA54Tqf6lqPTh7KlmljzJr9PJiXPhlfdLB2WzXNkr"
    "7e+ZlXJp39w5PCwd6/tqNzyj2XdbhnlQ2X01mRT0w9Lz2Wb2wB6sPdEf+SR3smYx"
    "m99ztrjG/gEnDE0p")
_LASPY_KEYS12 = (
    "eNrzcQx2Y8ACGJn8Qzxcgxhwg5zE4oJKBSM9cz0DrPLqjK/YHzM8hpknw8CMw6Bq"
    "kXXuD6ta7NFpsKRDvwOEnu+AoikCKu4BpX2g8i4wdRpQWtUhBUiCsBQLWIAJ2ZgT"
    "UJyKRU6HEYI3QOQYkeUARZgfSA==")

WKT_NAVD88_FTUS = (
    'VERT_CS["NAVD88 height (ftUS)",VERT_DATUM["North American Vertical '
    'Datum 1988",2005,AUTHORITY["EPSG","5103"]],UNIT["US survey foot",'
    '0.304800609601219,AUTHORITY["EPSG","9003"]],AXIS["Gravity-related '
    'height",UP],AUTHORITY["EPSG","6360"]]')
WKT_NAVD88_M2 = (
    'VERTCRS["NAVD88 height",VDATUM["North American Vertical Datum 1988"],'
    'CS[vertical,1],AXIS["gravity-related height (H)",up,LENGTHUNIT['
    '"metre",1]],ID["EPSG",5703]]')
WKT_COMPOUND = (
    'COMPD_CS["Flat plus height",PROJCS["Flat",GEOGCS["G",DATUM["D",'
    'SPHEROID["S",6378137,298.25,AUTHORITY["EPSG","7019"]]],PRIMEM["P",0],'
    'UNIT["degree",0.0174532925199433]],UNIT["US survey foot",'
    '0.304800609601219,AUTHORITY["EPSG","9003"]]],'
    'VERT_CS["Local height",VERT_DATUM["Local",2005],UNIT["metre",1],'
    'AXIS["Gravity-related height",UP]]]')


def self_test():
    """Run the assertions in a temp directory that is removed even when one
    of them raises, so a crash never leaves zdatum-selftest-* behind."""
    tmp = tempfile.mkdtemp(prefix="zdatum-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _terrain(x, y):
    """The synthetic ground, in metres: a slope with 11 m of relief over a
    100 by 100 unit square. It needs that much, because with less the metre
    and foot readings of a tile cannot be told apart at the default tolerance
    and the tool, correctly, will not decide."""
    return 12.0 + 0.06 * (x - 1000.0) + 0.05 * (y - 2000.0)


def _lattice(step=5):
    return [(1000 + step * i, 2000 + step * j)
            for i in range(100 // step + 1) for j in range(100 // step + 1)]


# Eight control points on lattice nodes, well inside the square.
_CONTROL_XY = [(1020, 2020), (1080, 2020), (1020, 2080), (1080, 2080),
               (1050, 2050), (1030, 2060), (1070, 2040), (1040, 2090)]


def _build_las(points, minor=2, fmt=1, scale=(0.01, 0.01, 0.01),
               offset=(0.0, 0.0, 0.0), vlrs=(), evlrs=(), encoding=0,
               zrange=None, rec_len=None, count=None, fmt_byte=None,
               extra=0):
    """LAS bytes for points of (x, y, z, class). A test fixture writer: it
    reads nothing back and shares no code with the reader."""
    size = 375 if minor >= 4 else 227
    head = bytearray(size)
    head[0:4] = b"LASF"
    head[24], head[25] = 1, minor
    struct.pack_into("<H", head, 6, encoding)
    struct.pack_into("<H", head, 94, size)
    block = b""
    for user, rid, payload in vlrs:
        block += struct.pack("<H16sHH32s", 0, user.encode("ascii"), rid,
                             len(payload), b"") + payload
    struct.pack_into("<II", head, 96, size + len(block), len(vlrs))
    head[104] = fmt if fmt_byte is None else fmt_byte
    natural = {0: 20, 1: 28, 6: 30}[fmt] + extra
    struct.pack_into("<H", head, 105, rec_len or natural)
    n = len(points) if count is None else count
    struct.pack_into("<I", head, 107, n if minor < 4 or fmt < 6 else 0)
    struct.pack_into("<3d", head, 131, *scale)
    struct.pack_into("<3d", head, 155, *offset)
    zs = [p[2] for p in points] or [0.0]
    zlo, zhi = zrange if zrange else (min(zs), max(zs))
    struct.pack_into("<2d", head, 211, zhi, zlo)
    body = b""
    for x, y, z, cls in points:
        ix = int(round((x - offset[0]) / scale[0]))
        iy = int(round((y - offset[1]) / scale[1]))
        iz = int(round((z - offset[2]) / scale[2]))
        if fmt == 6:
            rec = struct.pack("<iiiHBBBBhHd", ix, iy, iz, 0, 0x11, 0, cls,
                              0, 0, 0, 0.0)
        elif fmt == 1:
            rec = struct.pack("<iiiHBBbBHd", ix, iy, iz, 0, 0x09, cls, 0,
                              0, 0, 0.0)
        else:
            rec = struct.pack("<iiiHBBbBH", ix, iy, iz, 0, 0x09, cls, 0, 0,
                              0)
        body += rec + b"\0" * extra
    tail = b""
    if evlrs:
        struct.pack_into("<Q", head, 235, size + len(block) + len(body))
        struct.pack_into("<I", head, 243, len(evlrs))
        for user, rid, payload in evlrs:
            tail += struct.pack("<H16sHQ32s", 0, user.encode("ascii"), rid,
                                len(payload), b"") + payload
    if minor >= 4:
        struct.pack_into("<Q", head, 247, n)
    return bytes(head) + block + body + tail


def _geokeys(vertical=None, units=None, citation=None):
    """A GeoKeyDirectoryTag payload and the GeoAsciiParamsTag that goes
    with it, as (directory bytes, ascii bytes)."""
    keys, text = [(1024, 0, 1, 1)], b""
    if vertical is not None:
        keys.append((KEY_VERTICAL, 0, 1, vertical))
    if citation is not None:
        text = citation.encode("ascii") + b"|"
        keys.append((KEY_CITATION, TAG_ASCII, len(text), 0))
    if units is not None:
        keys.append((KEY_UNITS, 0, 1, units))
    flat = [1, 1, 0, len(keys)] + [v for key in keys for v in key]
    return struct.pack("<%dH" % len(flat), *flat), text


def _crs_vlrs(vertical=None, units=None, citation=None):
    directory, text = _geokeys(vertical, units, citation)
    out = [("LASF_Projection", 34735, directory)]
    if text:
        out.append(("LASF_Projection", 34737, text))
    return out


class _Harness(object):
    """The check()/raises() recorder, a class so the self-test can test it."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label, kind=ValueError):
        try:
            fn()
        except kind:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def footer(self):
        """Print the closing line and return the process exit status."""
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


def unpack(text):
    """Bytes from the zlib plus base64 text of a fixture."""
    return zlib.decompress(base64.b64decode(text))


def _self_test(tmp):
    """Assertions over the decision core and the whole CLI. No network, no
    credentials. The only files are the ones written under tmp."""
    harness = _Harness()
    check, raises = harness.check, harness.raises

    def path(name):
        return os.path.join(tmp, name)

    def slurp(file_path):
        with open(file_path, "rb") as handle:
            return handle.read()

    def put(name, data):
        with open(path(name), "wb") as handle:
            handle.write(data)
        return path(name)

    def put_text(name, text):
        return put(name, text.encode("utf-8"))

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def tile_points(f=lambda z: z, veg=False):
        pts = [(x, y, f(_terrain(x, y)), 2) for x, y in _lattice()]
        if veg:
            pts += [(x, y, f(_terrain(x, y)) + 8.0, 1)
                    for x, y in _CONTROL_XY]
        return pts

    def control_csv(name, unit_scale=1.0, shift=0.0, header="X,Y,Z"):
        rows = [header]
        for x, y in _CONTROL_XY:
            rows.append("%d,%d,%.4f" % (x, y,
                                        _terrain(x, y) * unit_scale + shift))
        return put_text(name, "\n".join(rows) + "\n")

    def tile(name, f=lambda z: z, vlrs=None, **kw):
        if vlrs is None:
            vlrs = _crs_vlrs(5703)
        veg = kw.pop("veg", False)
        pts = kw.pop("points", None) or tile_points(f, veg)
        return put(name, _build_las(pts, vlrs=vlrs, **kw))

    def error_of(fn, kind=ValueError):
        """The message of the error fn raises, or "" when it raises none."""
        try:
            fn()
        except kind as exc:
            return str(exc)
        return ""

    def pairs_of(refs, f, n=1):
        return [Pair(float(i), 0.0, f(r), r, n) for i, r in enumerate(refs)]

    def report_for(refs, f, claimed="m", ref_unit="m", finding=None,
                   assumed=False, tolerance=0.30, big_offset=5.0):
        claim = Claim(claimed, "Test height", 5703, "WKT", finding)
        return build_report("t.las", Header(), claim, claimed, assumed,
                            "ref.csv", "note", ref_unit, pairs_of(refs, f), 0,
                            len(refs), None, tolerance, big_offset)

    def text_of(report):
        return "\n".join(describe(report))

    print("zdatum self-test: temporary files only, no network, no "
          "credentials")
    print("-" * 68)

    # ---- the units the whole check rests on
    check(FOOT == 0.3048,
          "the international foot is exactly 0.3048 metre")
    check(US_SURVEY_FOOT == 1200.0 / 3937.0
          and abs(US_SURVEY_FOOT - 0.3048006096012192) < 1e-15,
          "the US survey foot is 1200/3937 metre, 0.3048006096 to ten places")
    check(UNIT_FACTOR == {"m": 1.0, "ft": 0.3048, "usft": 1200.0 / 3937.0}
          and set(UNIT_CHOICES) == set(UNIT_FACTOR)
          and set(UNIT_NAME) == set(UNIT_FACTOR),
          "every unit has a factor and a name, and the flag offers them all")
    check(EPSG_UNIT == {9001: "m", 9002: "ft", 9003: "usft"},
          "the GeoTIFF unit codes are the EPSG ones: 9001 metre, 9002 foot, "
          "9003 US survey foot")
    check(len(VERTICAL_CRS) == 8
          and VERTICAL_CRS[5714] == ("MSL height", "m")
          and VERTICAL_CRS[5773] == ("EGM96 height", "m")
          and VERTICAL_CRS[3855] == ("EGM2008 height", "m")
          and VERTICAL_CRS[5703] == ("NAVD88 height", "m")
          and VERTICAL_CRS[6360] == ("NAVD88 height (ftUS)", "usft")
          and VERTICAL_CRS[8228] == ("NAVD88 height (ft)", "ft")
          and VERTICAL_CRS[5702] == ("NGVD29 height (ftUS)", "usft")
          and VERTICAL_CRS[7968] == ("NGVD29 height (m)", "m"),
          "the vertical CRS table holds the EPSG codes checked against PROJ, "
          "and 5702 is the US survey foot one, not metres"
          "  <-- pinned defect")
    check(other_unit("m") == "ft" and other_unit("ft") == "m"
          and other_unit("usft") == "m",
          "the other unit of metres is feet, and of either foot is metres")

    # ---- median and spread
    check(median([3.0, 1.0, 2.0]) == 2.0, "the median of three is the middle")
    check(median([4.0, 1.0, 2.0, 3.0]) == 2.5,
          "the median of four is the mean of the middle two")
    raises(lambda: median([]), "the median of nothing is refused")
    check(residual_stats([1.0, 1.0, 1.0]) == (1.0, 0.0),
          "equal residuals have no spread")
    check(residual_stats([0.0, 1.0, 2.0, 3.0, 100.0]) == (2.0, 1.0),
          "one wild residual does not move the median or the spread")

    # ---- the decision core, on pairs with no files at all
    refs = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    d = diagnose(pairs_of(refs, lambda r: r), "m", "m")
    check(d.kind == AGREES and d.fit.unit == "m" and d.fit.median == 0.0,
          "Z equal to the reference in the claimed unit AGREES")
    check(abs(d.relief - 5.0) < 1e-12 and d.n == 6,
          "the relief of the reference is its range, here 5 m, over 6 pairs")
    d = diagnose(pairs_of(refs, lambda r: r / FOOT), "m", "m")
    check(d.kind == UNIT and d.fit.unit == "ft"
          and abs(d.fit.median) < 1e-9,
          "metres claimed but the values are the reference in feet is UNIT"
          "  <-- pinned defect")
    d = diagnose(pairs_of(refs, lambda r: r), "ft", "m")
    check(d.kind == UNIT and d.fit.unit == "m",
          "feet claimed but the values are the reference in metres is UNIT")
    d = diagnose(pairs_of(refs, lambda r: r), "usft", "m")
    check(d.kind == UNIT and d.fit.unit == "m",
          "US survey feet claimed but the values are metres is UNIT too")
    d = diagnose(pairs_of(refs, lambda r: r - 27.0), "m", "m")
    check(d.kind == OFFSET and abs(d.fit.median + 27.0) < 1e-9
          and d.fit.unit == "m",
          "a steady 27 m below the reference is OFFSET, units as claimed"
          "  <-- pinned defect")
    d = diagnose(pairs_of(refs, lambda r: (r + 3.0) / FOOT), "m", "m")
    check(d.kind == UNIT_OFFSET and d.fit.unit == "ft"
          and abs(d.fit.median - 3.0) < 1e-9,
          "feet that also sit 3 m high is UNIT_OFFSET, never plain OFFSET")
    d = diagnose(pairs_of(refs, lambda r: r + 0.25), "m", "m")
    check(d.kind == AGREES,
          "a bias inside the tolerance AGREES, because the tolerance is "
          "the definition of agreeing")
    d = diagnose(pairs_of(refs, lambda r: r + 0.31), "m", "m")
    check(d.kind == OFFSET,
          "a bias a hundredth over the tolerance is OFFSET")
    d = diagnose(pairs_of(refs, lambda r: r + 0.31), "m", "m", 0.5)
    check(d.kind == AGREES, "a wider tolerance moves the same bias to AGREES")
    scrambled = [10.0, 20.0, 11.0, 25.0, 12.0, 30.0]
    d = diagnose(pairs_of(refs, lambda r: scrambled[int(r) - 10]), "m", "m")
    check(d.kind == SCATTER and d.fit is None and len(d.tests) == 2,
          "residuals that fit no single shift in either unit are SCATTER")
    d = diagnose(pairs_of([0.05, 0.07, 0.06, 0.08, 0.05, 0.07],
                          lambda r: r), "m", "m")
    check(d.kind == UNDECIDED and "too little to tell metre from "
          "international foot" in d.reason,
          "a reference near zero cannot tell metres from feet, so the "
          "answer is NOT DECIDED, not AGREES"
          "  <-- pinned defect")
    d = diagnose(pairs_of([20.0, 20.01, 19.99, 20.0, 20.02, 19.98],
                          lambda r: r + 3.0), "m", "m")
    check(d.kind == UNDECIDED,
          "a flat reference with a bias fits both units with a shift, so "
          "that is NOT DECIDED, not OFFSET"
          "  <-- pinned defect")
    d = diagnose(pairs_of([20.0, 20.01, 19.99, 20.0, 20.02, 19.98],
                          lambda r: r), "m", "m")
    check(d.kind == AGREES,
          "a flat reference that agrees is AGREES, because only one reading "
          "is unbiased")
    d = diagnose(pairs_of(refs[:4], lambda r: r), "m", "m")
    check(d.kind == UNDECIDED and "only 4 pair(s)" in d.reason
          and d.tests == [],
          "four pairs are too few: NOT DECIDED, with no residual claimed")
    d = diagnose([], "m", "m")
    check(d.kind == UNDECIDED and d.n == 0,
          "no pairs at all is NOT DECIDED")
    d = diagnose(pairs_of([x / FOOT for x in refs], lambda r: r * FOOT),
                 "m", "ft")
    check(d.kind == AGREES,
          "a reference in feet is converted before it is compared")
    d = diagnose(pairs_of([x / US_SURVEY_FOOT for x in refs],
                          lambda r: r * US_SURVEY_FOOT), "m", "usft")
    check(d.kind == AGREES,
          "and a reference in US survey feet is converted with its own "
          "factor")

    # ---- the edges of every decision
    dflt = _parse([])
    check((MIN_PAIRS, MIN_SHARE, DEFAULT_TOLERANCE, DEFAULT_RADIUS,
           DEFAULT_MIN_POINTS, DEFAULT_MAX_SAMPLES, DEFAULT_BIG_OFFSET,
           DEFAULT_SAMPLE, GROUND) == (5, 0.9, 0.30, 10.0, 3, 20000, 5.0, 10,
                                       2)
          and (dflt.tolerance, dflt.radius, dflt.min_points, dflt.max_samples,
               dflt.big_offset, dflt.sample, dflt.ground_class, dflt.x_field,
               dflt.y_field, dflt.z_field)
          == (0.30, 10.0, 3, 20000, 5.0, 10, 2, "X", "Y", "Z"),
          "the documented defaults are the ones the flags and the constants "
          "hold: 0.30 m, radius 10, 3 points, 20000 samples, 5 m, 10 listed, "
          "class 2, a 5 pair minimum and a 90 percent share")
    d = diagnose(pairs_of(refs[:5], lambda r: r), "m", "m")
    check(d.kind == AGREES and d.n == 5,
          "exactly 5 pairs, the minimum, are enough to decide")
    d = diagnose(pairs_of(refs, lambda r: r + 0.5), "m", "m", 0.5)
    check(d.kind == AGREES,
          "a median exactly equal to the tolerance is inside it")
    d = diagnose(pairs_of(refs, lambda r: r + 0.5000001), "m", "m", 0.5)
    check(d.kind == OFFSET,
          "and a median a ten-millionth over it is not")
    swing = {10: 0.0, 11: 0.5, 12: -0.5, 13: 0.5, 14: -0.5}
    d = diagnose(pairs_of(refs[:5], lambda r: r + swing[int(r)]), "m", "m",
                 0.5)
    check(d.kind == AGREES and d.tests[0].spread == 0.5,
          "a spread exactly equal to the tolerance is steady")
    swing = {10: 0.0, 11: 0.5000001, 12: -0.5000001, 13: 0.5000001,
             14: -0.5000001}
    d = diagnose(pairs_of(refs[:5], lambda r: r + swing[int(r)]), "m", "m",
                 0.5)
    check(d.kind == SCATTER,
          "and a spread a ten-millionth over it is scatter")

    # ---- a minority that is wrong cannot hide behind the median
    ten = [10.0 + i for i in range(10)]
    d = diagnose(pairs_of(ten, lambda r: r + (40.0 if r < 11.0 else 0.0)),
                 "m", "m")
    check(d.kind == AGREES and d.fit.within == 9,
          "one pair of ten wrong by 40 m is 90 percent, which is the "
          "share asked for, so the tile still AGREES")
    d = diagnose(pairs_of(ten, lambda r: r + (40.0 if r < 12.0 else 0.0)),
                 "m", "m")
    check(d.kind == MIXED and d.fit.within == 8 and d.fit.unit == "m",
          "two pairs of ten wrong by 40 m is 80 percent, which is MIXED, "
          "not AGREES  <-- pinned defect")
    d = diagnose(pairs_of(ten, lambda r: r + (3.0 if r < 14.0 else 0.0)),
                 "m", "m")
    check(d.kind == MIXED,
          "40 percent of the pairs 3 m high is MIXED too"
          "  <-- pinned defect")
    d = diagnose(pairs_of(ten, lambda r: r + (0.5 if r >= 18.0 else 0.0)),
                 "m", "m", 0.5)
    check(d.kind == AGREES and d.fit.within == 10,
          "a pair exactly one tolerance from the median is within it")
    d = diagnose(pairs_of(ten, lambda r: r / FOOT if r < 14.0 else r),
                 "m", "m")
    check(d.kind == MIXED and d.fit.unit == "m",
          "40 percent of the values in feet under a metre label is MIXED, "
          "never AGREES  <-- pinned defect")
    check(gate(report_for(ten, lambda r: r + (40.0 if r < 12.0 else 0.0)))
          == 1, "MIXED exits 1, as every other finding does")

    # ---- what the report says, and what it must not say
    bad_ten = report_for(ten, lambda r: r + (40.0 if r < 12.0 else 0.0))
    text = text_of(bad_ten)
    check("VERDICT: MIXED. Only 8 of 10 pairs (80 percent) lie within 0.300 m"
          in text and "at least 90 percent must" in text
          and "pairs furthest from the reference" in text,
          "the MIXED verdict gives the count, the share and the bar, and "
          "lists the pairs that disagree")
    plain = text_of(report_for(refs, lambda r: r - 6.0))
    check("OFFSET. The Z values sit a steady -6.0000 m" in plain
          and "test the ellipsoid-height explanation first" in plain
          and "only if they are read as" not in plain,
          "a plain OFFSET names no other unit and, at 6 m, adds the "
          "ellipsoid hint")
    plain = text_of(report_for(refs, lambda r: r - 6.0, big_offset=6.0))
    check("steady -6.0000 m" in plain
          and "ellipsoid-height explanation" not in plain,
          "an offset exactly equal to --big-offset adds no ellipsoid hint")
    both = text_of(report_for(refs, lambda r: (r - 6.0) / FOOT))
    check("UNIT_OFFSET." in both and "only if they are read as "
          "international foot, not the metre (named by the header)" in both,
          "a UNIT_OFFSET names the reading it needs and the unit it left")
    plain = text_of(report_for(refs, lambda r: r, claimed="ft"))
    check("VERDICT: UNIT. The unit is international foot" in plain
          and "fit the reference as metre" in plain
          and "parts per million" not in plain,
          "metres held under a foot label is UNIT with no 2 ppm note, "
          "because the note is only for a foot")
    plain = text_of(report_for(refs, lambda r: r / FOOT))
    check("fit the reference as international foot" in plain
          and "parts per million" in plain,
          "and a foot held under a metre label is UNIT with the note")
    plain = text_of(report_for(refs, lambda r: r / FOOT))
    check([line for line in plain.splitlines()
           if "in the header's unit" in line][0].count("(metre)") == 1
          and [line for line in plain.splitlines()
               if "in the other unit" in line][0].count(
                   "(international foot)") == 1,
          "the two residual lines are labelled the right way round: the "
          "header's unit first, the other unit second  <-- pinned defect")
    plain = text_of(report_for(refs, lambda r: r, claimed="ft"))
    check([line for line in plain.splitlines()
           if "in the header's unit" in line][0].count(
               "(international foot)") == 1,
          "and for a foot label the header's unit is the foot")
    for label, rows in (
            ("AGREES", lambda r: r), ("UNIT", lambda r: r / FOOT),
            ("OFFSET", lambda r: r - 6.0)):
        plain = text_of(report_for(refs, rows))
        check("The header findings above still stand" not in plain
              and "FINDING" not in plain,
              "%s with no finding prints no 'still stand' sentence" % label)
    for label, rows in (("AGREES", lambda r: r), ("UNIT", lambda r: r / FOOT),
                        ("OFFSET", lambda r: r - 6.0)):
        plain = text_of(report_for(refs, rows, finding="a header problem"))
        check("FINDING   a header problem" in plain
              and ("still stand" in plain) == (label == "AGREES"),
              "%s with a finding keeps it, and only AGREES says the "
              "findings still stand" % label)
    plain = text_of(report_for(refs[:3], lambda r: r, finding="a problem"))
    check("NOT DECIDED" in plain and "findings above still stand" in plain
          and "relief of the reference" not in plain
          and "if stored Z" not in plain,
          "NOT DECIDED with a finding says the findings still stand")
    named = text_of(report_for(refs, lambda r: r))
    check("(named by the header)" in named and "ASSUMED" not in named
          and "reference: ref.csv, note, declared metre" in named,
          "a unit the header named is not called assumed, and the "
          "reference is named")
    assumed = text_of(report_for(refs, lambda r: r, assumed=True))
    check("(assumed)" in assumed and "ASSUMED with --assume-unit" in assumed
          and "named by the header" not in assumed,
          "an assumed unit is called assumed, in the header block and in "
          "the verdict")

    # ---- the header range check
    h = Header()
    h.scale, h.zmin, h.zmax = (0.01, 0.01, 0.01), 5.0, 9.0
    check(header_range_issue(h, 5.0, 9.0) is None,
          "a header range equal to the points' range is fine")
    check(header_range_issue(h, 5.01, 8.99) is None,
          "one scale step of slack is allowed for rounding")
    check("5.0000 to 9.0000" in header_range_issue(h, 5.0, 29.5),
          "a header whose range differs from the points' range is named")
    check("5.0000 to 9.0000" in header_range_issue(h, 6.5, 9.0),
          "and so is a minimum that is off")
    h.zmin = h.zmax = 0.0
    check(header_range_issue(h, 5.0, 9.0) is None,
          "a header range of 0 to 0 means not recorded, and is not a finding")
    h.scale, h.zmin, h.zmax = (0.5, 0.5, 0.5), 0.0, 9.0
    check(header_range_issue(h, 5.0, 9.0) is not None,
          "a header range that starts at 0 but ends at 9 is still checked")
    h.zmin, h.zmax = 5.0, 0.0
    check(header_range_issue(h, 5.0, 9.0) is not None,
          "and one that starts at 5 and ends at 0")
    h.scale, h.zmin, h.zmax = (0.5, 0.5, 0.5), 5.0, 9.0
    check(header_range_issue(h, 5.75, 9.0) is None
          and header_range_issue(h, 5.0, 8.25) is None,
          "a difference of exactly one and a half scale steps passes")
    check(header_range_issue(h, 5.7, 8.3) is None
          and header_range_issue(h, 6.0, 9.0) is not None
          and header_range_issue(h, 5.0, 8.0) is not None,
          "the slack is one and a half scale steps: 0.7 m of 0.5 m steps "
          "passes, 1.0 m does not, at either end of the range")

    # ---- the public header block
    pts = [(1000.0 + i, 2000.0 + i, 10.0 + i * 0.5, 2) for i in range(6)]
    raw = _build_las(pts, minor=2, fmt=1, scale=(0.01, 0.01, 0.005),
                     offset=(500.0, 600.0, 7.0))
    h = parse_header(raw[:375])
    check((h.major, h.minor, h.fmt, h.rec_len, h.n_points) == (1, 2, 1, 28, 6)
          and h.size == 227 and h.point_offset == 227 and h.n_vlr == 0,
          "a LAS 1.2 header gives version, format, record length, point "
          "count and where the points start")
    check(h.scale == (0.01, 0.01, 0.005) and h.offset == (500.0, 600.0, 7.0)
          and (h.zmin, h.zmax) == (10.0, 12.5),
          "the scale, the offset and the Z range come from the right bytes: "
          "the header stores max Z before min Z"
          "  <-- pinned defect")
    raw14 = _build_las(pts, minor=4, fmt=6, vlrs=_crs_vlrs(5703))
    h = parse_header(raw14[:375])
    check((h.minor, h.fmt, h.rec_len, h.size) == (4, 6, 30, 375)
          and h.n_points == 6 and h.n_vlr == 1,
          "a LAS 1.4 header takes the point count from the 64-bit field, "
          "because the legacy count is 0 for format 6"
          "  <-- pinned defect")
    raises(lambda: parse_header(b""), "an empty file is not LAS", LasError)
    raises(lambda: parse_header(b"PK\x03\x04" + b"\0" * 300),
           "a file that does not start with LASF is refused", LasError)
    raises(lambda: parse_header(b"LASF" + b"\0" * 100),
           "a header cut short is refused", LasError)
    check("truncated" in error_of(lambda: parse_header(b"LASF"), LasError)
          and "truncated" in error_of(
              lambda: parse_header(b"LASF" + b"\0" * 100), LasError)
          and "b'PK" + chr(92) + "x03" + chr(92) + "x04'" in error_of(
              lambda: parse_header(b"PK\x03\x04" + b"\0" * 300), LasError),
          "a header cut short says truncated, even at four bytes, and a "
          "file that is not LAS shows the four bytes it starts with")
    check(all(
        parse_header(_build_las([], fmt=1, fmt_byte=f, rec_len=n)[:375])
        .fmt == f and "shorter than the %d bytes" % n in error_of(
            lambda: parse_header(_build_las(
                [], fmt=1, fmt_byte=f, rec_len=n - 1)[:375]), LasError)
        for f, n in ((0, 20), (1, 28), (2, 26), (3, 34), (4, 57), (5, 63),
                     (6, 30), (7, 36), (8, 38), (9, 59), (10, 67))),
          "every point format defined by LAS 1.4 is accepted at the record "
          "length the specification gives it and refused one byte shorter")
    check((HEADER_BASE, HEADER_14, WKT_BIT, KEY_VERTICAL, KEY_CITATION,
           KEY_UNITS, TAG_ASCII, MAX_CRS_BYTES)
          == (227, 375, 16, 4096, 4097, 4099, 34737, 1048576),
          "the header sizes, the WKT bit and the GeoTIFF key numbers are "
          "the ones in the specifications, written out here on their own, "
          "because the fixture writer reads the same constants")
    raises(lambda: parse_header(raw14[:300]),
           "a LAS 1.4 header cut before byte 375 is refused", LasError)
    bad = bytearray(raw)
    bad[24] = 2
    raises(lambda: parse_header(bytes(bad)),
           "LAS version 2.0 is refused", LasError)
    bad = bytearray(raw)
    bad[25] = 5
    raises(lambda: parse_header(bytes(bad)),
           "LAS version 1.5 is refused, because its header is unknown",
           LasError)
    laz = _build_las(pts, fmt_byte=0x81)
    laz_message = error_of(lambda: parse_header(laz[:375]), LasError)
    check("LAZ" in laz_message and "bit 7" in laz_message,
          "LAZ (point format plus 128) is refused by name, not read as junk"
          "  <-- pinned defect")
    raises(lambda: parse_header(_build_las(pts, fmt_byte=11)[:375]),
           "point format 11 is not defined and is refused", LasError)
    raises(lambda: parse_header(_build_las(pts, rec_len=20)[:375]),
           "a record length shorter than the format needs is refused",
           LasError)
    raises(lambda: parse_header(_build_las([], scale=(0.01, 0.01, 0.0))[:375]),
           "a Z scale factor of 0 is refused", LasError)

    # ---- bytes written by a third party
    third = unpack(_LASPY_WKT14)
    h = parse_header(third[:375])
    check((h.minor, h.fmt, h.n_points, h.encoding & WKT_BIT)
          == (4, 6, 3, WKT_BIT) and h.scale == (0.01, 0.01, 0.01),
          "a LAS 1.4 file written by laspy has its header read correctly")
    put("laspy14.las", third)
    with open(path("laspy14.las"), "rb") as handle:
        claim = read_claim(handle, parse_header(handle.read(375)))
        got = []

        class Collect(object):
            def add(self, x, y, z):
                got.append((round(x, 3), round(y, 3), round(z, 3)))

        stats = scan(handle, parse_header(third[:375]), Collect())
    check(claim.unit == "m" and claim.code == 5703
          and claim.name == "NAVD88 height" and claim.source == "WKT",
          "the WKT record laspy wrote for EPSG:5703 reads as NAVD88 metres")
    check(got == [(1001.0, 2001.0, 10.5), (1002.0, 2002.0, 11.25)]
          and stats == (10.5, 12.0, 2),
          "the points laspy wrote scan back exactly, and the class 1 point "
          "is not ground")
    keys12 = unpack(_LASPY_KEYS12)
    put("laspy12.las", keys12)
    with open(path("laspy12.las"), "rb") as handle:
        h12 = parse_header(handle.read(375))
        claim = read_claim(handle, h12)
    check((h12.minor, h12.fmt, h12.n_points) == (2, 1, 3)
          and claim.unit is None and h12.n_vlr == 0,
          "a LAS 1.2 file written by laspy has no CRS record at all, so it "
          "claims nothing about Z")

    # ---- the GeoTIFF claim
    c = claim_from_geokeys(*_geokeys(5703))
    check((c.unit, c.code, c.name, c.conflict, c.source)
          == ("m", 5703, "NAVD88 height", None, "GeoTIFF keys"),
          "vertical key 5703 is NAVD88 height in metres")
    c = claim_from_geokeys(*_geokeys(6360, 9003))
    check(c.unit == "usft" and c.conflict is None,
          "6360 with a unit key of 9003 agrees with itself")
    c = claim_from_geokeys(*_geokeys(6360, 9001))
    check(c.unit == "usft" and "EPSG:6360" in c.conflict
          and "metre" in c.conflict,
          "6360 with a unit key of metres contradicts itself, and the claim "
          "keeps the CRS's own unit"
          "  <-- pinned defect")
    c = claim_from_geokeys(*_geokeys(5701, 9002, "Local height"))
    check(c.unit == "ft" and c.name == "Local height" and c.code == 5701,
          "an unknown code takes its unit from the unit key and its name "
          "from the citation")
    c = claim_from_geokeys(*_geokeys(5701))
    check(c.unit is None and c.code == 5701 and c.name is None,
          "an unknown code with no unit key claims no unit")
    c = claim_from_geokeys(*_geokeys(None, 9001))
    check(c.unit == "m" and c.code is None,
          "a unit key alone still gives the unit")
    c = claim_from_geokeys(*_geokeys())
    check(c.unit is None and c.code is None,
          "a directory with only horizontal keys claims nothing")
    directory, _ = _geokeys(5703)
    fewer = bytearray(directory)
    struct.pack_into("<H", fewer, 6, 1)
    c = claim_from_geokeys(bytes(fewer))
    check(c.code is None,
          "a directory that declares one key is read as one key, though "
          "more bytes follow")
    c = claim_from_geokeys(directory + b"\0")
    check(c.code == 5703,
          "and a stray byte after the directory is ignored")
    c = claim_from_geokeys(b"\x01\x00\x01")
    check(c.unit is None and c.source == "GeoTIFF keys",
          "a directory shorter than its own 4-value header claims nothing")
    directory, _ = _geokeys(5703)
    short = bytearray(directory)
    struct.pack_into("<H", short, 6, 9)
    c = claim_from_geokeys(bytes(short))
    check(c.code == 5703,
          "a key count larger than the directory holds is clipped, not "
          "read past the end")

    # ---- the WKT claim
    c = claim_from_wkt(WKT_NAVD88_FTUS)
    check((c.unit, c.code, c.name, c.source)
          == ("usft", 6360, "NAVD88 height (ftUS)", "WKT"),
          "WKT 1 for EPSG:6360 reads as US survey feet, and the last "
          "authority is the CRS's, not the datum's or the unit's"
          "  <-- pinned defect")
    c = claim_from_wkt(WKT_NAVD88_M2)
    check((c.unit, c.code) == ("m", 5703),
          "WKT 2 for EPSG:5703 reads as metres through LENGTHUNIT and ID")
    c = claim_from_wkt(WKT_COMPOUND)
    check(c.unit == "m" and c.name == "Local height" and c.code is None,
          "in a compound CRS the vertical part is read, not the horizontal "
          "feet before it")
    c = claim_from_wkt(
        'COMPD_CS["c",VERT_CS["V",AXIS["h",UP]],PROJCS["P",UNIT["US survey '
        'foot",0.3048006096]],AUTHORITY["EPSG","7777"]]')
    check(c.unit is None and c.code is None and c.name == "V",
          "a vertical CRS with no unit does not borrow the unit or the code "
          "of what follows it in a compound CRS")
    c = claim_from_wkt('PROJCS["Flat",UNIT["US survey foot",0.3048006096]]')
    check(c.unit is None and c.name is None and c.source == "WKT",
          "a WKT with no vertical CRS claims nothing")
    c = claim_from_wkt('VERT_CS["Odd",UNIT["cubit",0.4572]]')
    check(c.unit is None and c.name == "Odd",
          "a vertical unit this tool has no factor for claims no unit")
    c = claim_from_wkt('VERT_CS[UNIT["metre",1]]')
    check(c.unit == "m" and c.name is None,
          "a vertical CRS with no quoted name still gives its unit")
    c = claim_from_wkt('VERT_CS["Cut",UNIT["metre",1]')
    check(c.unit == "m" and c.name == "Cut",
          "a WKT cut off before its closing bracket still reads")
    c = claim_from_wkt('VERT_CS["No unit",AXIS["h",UP]]')
    check(c.unit is None,
          "a vertical CRS that names no unit claims none")
    c = claim_from_wkt('VERT_CS["Odd",UNIT["metre",e]]')
    check(c.unit is None and c.name == "Odd",
          "a unit factor that is not a number claims no unit, and is not a "
          "crash  <-- pinned defect")

    # ---- which record the file's CRS is in
    def claim_of(name, **kw):
        put(name, _build_las(pts, **kw))
        with open(path(name), "rb") as handle:
            return read_claim(handle, parse_header(handle.read(375)))

    wkt = [("LASF_Projection", 2112, WKT_NAVD88_FTUS.encode("ascii") + b"\0")]
    c = claim_of("c_geotiff.las", vlrs=_crs_vlrs(6360))
    check(c.unit == "usft" and c.source == "GeoTIFF keys",
          "with the WKT bit clear the GeoTIFF keys are the claim")
    c = claim_of("c_wkt.las", minor=4, fmt=6, vlrs=wkt, encoding=WKT_BIT)
    check(c.unit == "usft" and c.source == "WKT",
          "with the WKT bit set the WKT record is the claim")
    c = claim_of("c_evlr.las", minor=4, fmt=6, evlrs=wkt, encoding=WKT_BIT)
    check(c.unit == "usft" and c.source == "WKT",
          "a LAS 1.4 WKT record in an EVLR after the points is found")
    c = claim_of("c_bit_no_wkt.las", minor=4, fmt=6, vlrs=_crs_vlrs(6360),
                 encoding=WKT_BIT)
    check(c.unit is None,
          "the WKT bit set with only GeoTIFF keys present claims nothing, "
          "as the specification's table says it should")
    c = claim_of("c_wkt_no_bit.las", vlrs=wkt)
    check(c.unit is None,
          "a WKT record with the WKT bit clear claims nothing")
    c = claim_of("c_other_user.las",
                 vlrs=[("someone_else", 34735, _geokeys(6360)[0])])
    check(c.unit is None,
          "a record under another user id is not the CRS, even with the "
          "same record id")
    c = claim_of("c_none.las")
    check(c.unit is None, "a file with no records claims nothing")

    good = _build_las([], vlrs=_crs_vlrs(6360))
    broken = bytearray(good)
    struct.pack_into("<I", broken, 100, 2)
    put("vlr_missing.las", bytes(broken))
    with open(path("vlr_missing.las"), "rb") as handle:
        raises(lambda: read_claim(handle, parse_header(handle.read(375))),
               "a header that counts more records than the file holds is "
               "refused", LasError)
    put("vlr_last.las", good[:-1])
    with open(path("vlr_last.las"), "rb") as handle:
        raises(lambda: read_claim(handle, parse_header(handle.read(375))),
               "a record that promises one byte more than remains is "
               "refused", LasError)
    put("vlr_exact.las", good)
    with open(path("vlr_exact.las"), "rb") as handle:
        check(read_claim(handle, parse_header(handle.read(375))).unit
              == "usft",
              "and one that promises exactly what remains is read")
    put("vlr_cut.las", good[:227 + 54 + 3])
    with open(path("vlr_cut.las"), "rb") as handle:
        raises(lambda: read_claim(handle, parse_header(handle.read(375))),
               "a record that promises more bytes than remain is refused",
               LasError)

    # ---- the point scan
    class Sink(object):
        def __init__(self):
            self.got = []

        def add(self, x, y, z):
            self.got.append((x, y, z))

    def scan_bytes(name, data, **kw):
        put(name, data)
        sink = Sink()
        with open(path(name), "rb") as handle:
            h = parse_header(handle.read(375))
            stats = scan(handle, h, sink, **kw)
        return sink.got, stats

    mixed = [(1.0, 2.0, 10.0, 2), (3.0, 4.0, 20.0, 1), (5.0, 6.0, 30.0, 2),
             (7.0, 8.0, 40.0, 2 + 32), (9.0, 10.0, 50.0, 7)]
    got, stats = scan_bytes("scan1.las", _build_las(mixed, fmt=1))
    check([g[2] for g in got] == [10.0, 30.0, 40.0] and stats == (10.0, 50.0, 3),
          "format 1 keeps the ground points, and a class byte with flag "
          "bits above the low five still counts as ground"
          "  <-- pinned defect")
    got, stats = scan_bytes("scan6.las", _build_las(mixed, minor=4, fmt=6))
    check([g[2] for g in got] == [10.0, 30.0] and stats == (10.0, 50.0, 2),
          "format 6 reads the class from byte 16 as a whole byte, so class "
          "34 is not ground there"
          "  <-- pinned defect")
    got, stats = scan_bytes("scan0.las", _build_las(mixed, fmt=0))
    check([g[2] for g in got] == [10.0, 30.0, 40.0],
          "format 0 reads the same class byte as format 1")
    got, _ = scan_bytes("scan_any.las", _build_las(mixed), any_class=True)
    check(len(got) == 5, "any_class compares every point")
    got, _ = scan_bytes("scan_g7.las", _build_las(mixed), ground_class=7)
    check([g[2] for g in got] == [50.0],
          "another ground class is honoured")
    got, stats = scan_bytes(
        "scan_extra.las", _build_las(mixed, fmt=1, extra=4))
    check([g[2] for g in got] == [10.0, 30.0, 40.0],
          "extra bytes after each record, which the header's record length "
          "declares, are skipped, not read as the next record"
          "  <-- pinned defect")
    got, stats = scan_bytes(
        "scan_off.las", _build_las(mixed, scale=(0.01, 0.01, 0.005),
                                   offset=(100.0, 200.0, 1000.0)))
    check(tuple(round(v, 6) for v in got[0]) == (1.0, 2.0, 10.0)
          and tuple(round(v, 6) for v in stats[:2]) == (10.0, 50.0),
          "X, Y and Z are record times scale plus offset, so they read back "
          "as written when neither is trivial")
    got, stats = scan_bytes(
        "scan_neg.las", _build_las(mixed, scale=(0.01, 0.01, -0.01)))
    check(stats[0] <= stats[1]
          and tuple(round(v, 6) for v in stats[:2]) == (10.0, 50.0),
          "a negative Z scale still reports the Z range low to high")
    got, stats = scan_bytes("scan_empty.las", _build_las([]))
    check(got == [] and stats == (0.0, 0.0, 0),
          "a tile with no points scans to nothing without an error")
    put("scan_short.las", _build_las(mixed, count=9))
    with open(path("scan_short.las"), "rb") as handle:
        h = parse_header(handle.read(375))
        raises(lambda: scan(handle, h, Sink()),
               "a header that counts more points than the file holds is "
               "refused", LasError)

    put("scan_one_short.las", _build_las(mixed)[:-1])
    with open(path("scan_one_short.las"), "rb") as handle:
        h = parse_header(handle.read(375))
        raises(lambda: scan(handle, h, Sink()),
               "a file one byte shorter than its points need is refused, "
               "not read into a crash", LasError)

    many = [(float(i), 0.0, i * 0.01, 2) for i in range(50)]
    put("many.las", _build_las(many))
    put("many_evlr.las", _build_las(many, minor=4, fmt=6, encoding=WKT_BIT,
                                    evlrs=[("LASF_Projection", 2112,
                                            b"VERT_CS[]" * 9)]))
    picked = {}
    saved = globals()["CHUNK"]
    try:
        for name in ("many.las", "many_evlr.las"):
            for chunk in (50000, 7, 1):
                globals()["CHUNK"] = chunk
                sink = Sink()
                with open(path(name), "rb") as handle:
                    h = parse_header(handle.read(375))
                    scan(handle, h, sink, stride=3)
                picked[name, chunk] = [int(round(g[0])) for g in sink.got]
    finally:
        globals()["CHUNK"] = saved
    check(picked["many.las", 50000] == list(range(0, 50, 3))
          and all(v == picked["many.las", 50000] for v in picked.values()),
          "stride 3 picks the same points whatever the read chunk size, "
          "and a record list after the points is never read as points"
          "  <-- pinned defect")

    # ---- control points
    ctl = Controls([(0.0, 0.0, 5.0), (100.0, 0.0, 6.0)], 10.0)
    for x in (0.0, 10.0, 10.01, -10.0):
        ctl.add(x, 0.0, 1.0)
    ctl.add(6.0, 8.0, 2.0)
    ctl.add(7.0, 8.0, 3.0)
    check(sorted(ctl.heights[0]) == [1.0, 1.0, 1.0, 2.0],
          "a point exactly at the radius is inside it, a hundredth beyond "
          "is outside, and the distance is straight-line")
    got_pairs, thin = ctl.pairs(2)
    check(thin == 1 and len(got_pairs) == 1 and got_pairs[0].n == 4
          and got_pairs[0].lidar == 1.0 and got_pairs[0].ref == 5.0,
          "a control point with too few ground points is counted, not "
          "compared, and the median of the rest is used")
    check(ctl.pairs(4)[1] == 1 and len(ctl.pairs(4)[0]) == 1
          and ctl.pairs(5)[1] == 2 and ctl.pairs(5)[0] == [],
          "a control point with exactly --min-points ground points counts, "
          "and one fewer does not")
    ctl = Controls([(9.9, 9.9, 1.0)], 10.0)
    ctl.add(10.1, 10.1, 4.0)
    check(ctl.heights == [[4.0]],
          "a ground point in the next grid cell over is still found "
          "within the radius"
          "  <-- pinned defect")
    ctl = Controls([(-5.0, -5.0, 1.0)], 10.0)
    ctl.add(-4.0, -4.0, 4.0)
    check(ctl.heights == [[4.0]],
          "negative coordinates index cells the same way")

    # ---- the ASCII grid
    grid_text = ("ncols 3\nnrows 3\nxllcorner 100\nyllcorner 200\n"
                 "cellsize 10\nNODATA_value -9999\n"
                 "1 2 3\n4 5 6\n7 8 -9999\n")
    g = parse_grid(grid_text)
    check((g.ncols, g.nrows, g.cellsize) == (3, 3, 10.0),
          "the grid header gives columns, rows and cell size")
    check(g.value_at(105.0, 225.0) == 1.0 and g.value_at(125.0, 225.0) == 3.0
          and g.value_at(105.0, 205.0) == 7.0,
          "the first data row is the top row, and a corner origin puts "
          "the first cell centre half a cell in"
          "  <-- pinned defect")
    check(abs(g.value_at(110.0, 220.0) - 3.0) < 1e-12,
          "the value between four cell centres is their bilinear mean")
    check(abs(g.value_at(107.5, 225.0) - 1.25) < 1e-12,
          "and a quarter of the way along a row it is a quarter of the "
          "way between the two cells")
    check(g.value_at(104.9, 225.0) is None and g.value_at(105.0, 230.1) is None
          and g.value_at(125.1, 225.0) is None
          and g.value_at(105.0, 204.9) is None,
          "outside the ring of cell centres there is no value")
    check(g.value_at(125.0, 205.0) is None and g.value_at(120.0, 210.0) is None,
          "a cell holding NoData, or an interpolation touching one, gives "
          "no value")
    g = parse_grid("NCOLS 2\nNROWS 2\nXLLCENTER 0\nYLLCENTER 0\nCELLSIZE 1\n"
                   "1 2 3 4")
    check(g.value_at(0.0, 1.0) == 1.0 and g.value_at(1.0, 0.0) == 4.0
          and abs(g.value_at(0.5, 0.5) - 2.5) < 1e-12,
          "keywords are not case sensitive, the centre origin is not "
          "shifted, and line breaks between values are not needed")
    g = parse_grid("ncols 2 nrows 1 xllcorner 0 yllcorner 0 cellsize 2\n"
                   "-9999 7\n")
    check(g.value_at(3.0, 1.0) == 7.0 and g.value_at(1.0, 1.0) is None,
          "NoData defaults to -9999 when the header does not say"
          "  <-- pinned defect")
    g = parse_grid("ncols 1 nrows 1 xllcorner 0 yllcorner 0 cellsize 4 "
                   "nodata_value 0 9")
    check(g.value_at(2.0, 2.0) == 9.0 and g.value_at(2.1, 2.0) is None,
          "a one-cell grid answers only at its centre")
    g = parse_grid("ncols 2 nrows 1 xllcorner 0 yllcorner 0 cellsize 1 "
                   "nan 5")
    check(g.rows == [[None, 5.0]],
          "a nan cell is NoData")
    for label, text in (
            ("a header with no ncols", "nrows 1 xllcorner 0 yllcorner 0 "
                                      "cellsize 1 5"),
            ("a header cut off after a keyword", "ncols 1 nrows 1 xllcorner "
                                                 "0 yllcorner 0 cellsize"),
            ("a header with no origin", "ncols 1 nrows 1 cellsize 1 5"),
            ("an x origin by centre and a y origin by corner",
             "ncols 1 nrows 1 xllcenter 0 yllcorner 0 cellsize 1 5"),
            ("a header with no y origin", "ncols 1 nrows 1 xllcorner 0 "
                                         "cellsize 1 5"),
            ("a header value that is not a number", "ncols x nrows 1 "
             "xllcorner 0 yllcorner 0 cellsize 1 5"),
            ("a cell size of zero", "ncols 1 nrows 1 xllcorner 0 yllcorner 0 "
                                    "cellsize 0 5"),
            ("zero columns", "ncols 0 nrows 1 xllcorner 0 yllcorner 0 "
                             "cellsize 1"),
            ("too few cell values", "ncols 2 nrows 2 xllcorner 0 yllcorner 0 "
                                    "cellsize 1 1 2 3"),
            ("too many cell values", "ncols 1 nrows 1 xllcorner 0 yllcorner 0 "
                                     "cellsize 1 1 2"),
            ("a cell that is not a number", "ncols 1 nrows 1 xllcorner 0 "
                                            "yllcorner 0 cellsize 1 abc"),
            ("an empty file", "")):
        raises(lambda t=text: parse_grid(t), "%s is refused" % label,
               GridError)
    check("holds 3 cell values, and ncols times nrows is 4" in error_of(
        lambda: parse_grid("ncols 2 nrows 2 xllcorner 0 yllcorner 0 "
                           "cellsize 1 1 2 3"), GridError),
          "a grid with the wrong number of values says how many it holds "
          "and how many it needs")
    put_text("g_bom.asc", "\ufeff" + grid_text)
    check(parse_grid(read_text(path("g_bom.asc"))).ncols == 3,
          "a byte order mark in front of the grid header is skipped")

    # ---- the control CSV
    put_text("c_bom.csv", "\ufeffX,Y,Z\n1,2,3\n4,5,x\n,6,7\n8,9,inf\n10,11,12\n")
    pts_read, skipped = read_controls(path("c_bom.csv"), "X", "Y", "Z")
    check(pts_read == [(1.0, 2.0, 3.0), (10.0, 11.0, 12.0)] and skipped == 3,
          "a byte order mark is skipped, and rows with a blank, a word or an "
          "infinity are counted as skipped, not read as zero"
          "  <-- pinned defect")
    put_text("c_cols.csv", "E,N,H\n1,2,3\n")
    pts_read, _ = read_controls(path("c_cols.csv"), "E", "N", "H")
    check(pts_read == [(1.0, 2.0, 3.0)], "the column names are configurable")
    raises(lambda: read_controls(path("c_cols.csv"), "X", "Y", "H"),
           "a column that is not in the CSV is refused by name",
           ColumnError)
    put_text("c_empty.csv", "")
    empty_message = error_of(
        lambda: read_controls(path("c_empty.csv"), "X", "Y", "Z"),
        ColumnError)
    check("no columns at all" in empty_message,
          "an empty CSV says it has no columns")
    accented = ["X,Y,Z,Desc"] + [
        "%d,%d,%.4f,Mon\u00e9 %d\u00b0" % (x, y, _terrain(x, y), i)
        for i, (x, y) in enumerate(_CONTROL_XY)]
    cp1252_csv = put("c_cp1252.csv", "\n".join(accented).encode("cp1252"))
    pts_read, skipped = read_controls(cp1252_csv, "X", "Y", "Z")
    check(len(pts_read) == 8 and skipped == 0
          and pts_read[0] == (1020.0, 2020.0, _terrain(1020, 2020)),
          "a control CSV saved by Excel as Windows-1252, with an accent and "
          "a degree sign in a description column, reads: X, Y and Z are "
          "ASCII  <-- pinned defect")
    pts_read, skipped = read_controls(
        put("c_utf16.csv", "\n".join(accented).encode("utf-16")),
        "X", "Y", "Z")
    check(len(pts_read) == 8 and skipped == 0,
          "and a UTF-16 CSV, which Excel's Unicode Text and PowerShell's "
          "-Encoding Unicode write, reads too  <-- pinned defect")
    pts_read, skipped = read_controls(
        put("c_utf16be.csv", ("\ufeff" + "\n".join(accented)).encode(
            "utf-16-be")), "X", "Y", "Z")
    check(len(pts_read) == 8 and skipped == 0,
          "in either byte order")
    check(to_number("1.5") == 1.5 and to_number("nan") is None
          and to_number(None) is None and to_number("") is None,
          "to_number reads a finite float and nothing else")

    # ---- hardening: values the file controls
    nan = float("nan")
    raises(lambda: parse_header(_build_las([], scale=(0.01, 0.01, nan))[:375]),
           "a Z scale factor of NaN is refused, not read as a number",
           LasError)
    raises(lambda: parse_header(_build_las([], scale=(nan, 0.01, 0.01))[:375]),
           "an X scale factor of NaN is refused", LasError)
    raises(lambda: parse_header(
        _build_las([], offset=(0.0, float("inf"), 0.0))[:375]),
        "an infinite offset is refused", LasError)
    short_header = bytearray(_build_las(pts))
    struct.pack_into("<H", short_header, 94, 100)
    raises(lambda: parse_header(bytes(short_header[:375])),
           "a header that says it is 100 bytes is refused", LasError)

    wkt_rec = ("LASF_Projection", 2112, WKT_NAVD88_M2.encode("ascii") + b"\0")

    def claim_file(name, **kw):
        put(name, _build_las(pts, minor=4, fmt=6, **kw))
        with open(path(name), "rb") as handle:
            return read_claim(handle, parse_header(handle.read(375)))

    base = _build_las(pts, minor=4, fmt=6, evlrs=[wkt_rec], encoding=WKT_BIT)
    huge = bytearray(base)
    struct.pack_into("<I", huge, 243, 4294967295)
    put("evlr_count.las", bytes(huge))
    with open(path("evlr_count.las"), "rb") as handle:
        raises(lambda: read_claim(handle, parse_header(handle.read(375))),
               "an EVLR count of 4 billion stops at the first missing header, "
               "not after 4 billion passes"
               "  <-- pinned defect", LasError)
    huge = bytearray(base)
    start = struct.unpack_from("<Q", huge, 235)[0]
    struct.pack_into("<Q", huge, start + 20, 2 ** 63)
    put("evlr_len.las", bytes(huge))
    with open(path("evlr_len.las"), "rb") as handle:
        raises(lambda: read_claim(handle, parse_header(handle.read(375))),
               "an EVLR length of 2^63 is refused, not allocated"
               "  <-- pinned defect", LasError)
    saved = globals()["MAX_CRS_BYTES"]
    try:
        globals()["MAX_CRS_BYTES"] = 8
        put("evlr_cap.las", base)
        with open(path("evlr_cap.las"), "rb") as handle:
            raises(lambda: read_claim(handle, parse_header(handle.read(375))),
                   "a CRS record over the size cap is refused", LasError)
        globals()["MAX_CRS_BYTES"] = len(wkt_rec[2])
        with open(path("evlr_cap.las"), "rb") as handle:
            at_cap = read_claim(handle, parse_header(handle.read(375)))
        big = [("other_vendor", 7, b"\1" * 500), wkt_rec]
        put("evlr_skip.las", _build_las(pts, minor=4, fmt=6, evlrs=big,
                                        encoding=WKT_BIT))
        globals()["MAX_CRS_BYTES"] = len(wkt_rec[2]) + 10
        with open(path("evlr_skip.las"), "rb") as handle:
            c = read_claim(handle, parse_header(handle.read(375)))
    finally:
        globals()["MAX_CRS_BYTES"] = saved
    check(at_cap.unit == "m",
          "a CRS record exactly the size of the cap is read")
    check(c.unit == "m",
          "a large record of another vendor is stepped over, and the CRS "
          "record after it is still found")

    # ---- hardening: which CRS record, duplicates and disagreement
    c = claim_file("k_bit_clear.las", vlrs=[wkt_rec])
    check(c.unit == "m" and "WKT bit" in c.conflict,
          "point format 6 with the WKT bit clear reads the WKT record and "
          "names the error"
          "  <-- pinned defect")
    c = claim_file("k_bit_clear_keys.las", vlrs=_crs_vlrs(6360))
    check(c.unit is None and "WKT bit" in c.conflict,
          "point format 6 with the WKT bit clear and only GeoTIFF keys "
          "claims nothing, and says why"
          "  <-- pinned defect")
    c = claim_file("k_dup.las", vlrs=[wkt_rec, wkt_rec], encoding=WKT_BIT)
    check(c.unit == "m" and "2 WKT records" in c.conflict
          and "different units" not in c.conflict,
          "two identical WKT records are named as a duplicate"
          "  <-- pinned defect")
    ftus = ("LASF_Projection", 2112, WKT_NAVD88_FTUS.encode("ascii") + b"\0")
    c = claim_file("k_dup_units.las", vlrs=[wkt_rec, ftus],
                   encoding=WKT_BIT)
    check(c.unit == "m" and "different units" in c.conflict,
          "two WKT records that name different units say so, and the first "
          "is the one read"
          "  <-- pinned defect")
    keys2 = _crs_vlrs(5703) + _crs_vlrs(6360)[:1]
    c = claim_of("k_dup_keys.las", vlrs=keys2)
    check(c.unit == "m" and "2 GeoTIFF key records" in c.conflict,
          "two GeoTIFF key directories are named as a duplicate")
    c = claim_file("k_rival.las", vlrs=[wkt_rec] + _crs_vlrs(6360),
                   encoding=WKT_BIT)
    check(c.unit == "m" and "GeoTIFF key record that names US survey foot"
          in c.conflict,
          "a WKT record and GeoTIFF keys that name different units are a "
          "conflict"
          "  <-- pinned defect")
    c = claim_of("k_rival_old.las", vlrs=[wkt_rec] + _crs_vlrs(6360))
    check(c.unit == "usft" and "WKT record that names metre" in c.conflict,
          "and the same from a LAS 1.2 file, read the other way round")
    c = claim_file("k_agree.las", vlrs=[wkt_rec] + _crs_vlrs(5703),
                   encoding=WKT_BIT)
    check(c.unit == "m" and c.conflict is None,
          "a WKT record and GeoTIFF keys that agree are not a conflict")
    c = claim_file("k_super.las", vlrs=[("LASF_Spec", 2112,
                                         ftus[2]), wkt_rec],
                   encoding=WKT_BIT)
    check(c.unit == "m" and c.conflict is None,
          "a superseded CRS record (LASF_Spec) is ignored, so the live one "
          "is not a duplicate")
    c = claim_file("k_conflict_kept.las", vlrs=[wkt_rec, wkt_rec]
                   + _crs_vlrs(6360, 9001), encoding=WKT_BIT)
    check("2 WKT records" in c.conflict and c.unit == "m",
          "a conflict already on the claim is kept beside the new notes")
    keys_bad = _crs_vlrs(6360, 9001)
    c = claim_of("k_self.las", vlrs=keys_bad)
    check("EPSG:6360" in c.conflict and c.unit == "usft",
          "a claim that contradicts itself keeps that finding")

    c = claim_of("k_cit.las", vlrs=_crs_vlrs(5701, 9002, "Local height"))
    check(c.unit == "ft" and c.name == "Local height",
          "the GeoTIFF citation in the ASCII params record names the CRS")
    put("vlr_short.las", _build_las(
        [], vlrs=[("someone_else", 1, b"\0" * 60)]))
    cut_file = bytearray(slurp(path("vlr_short.las")))
    struct.pack_into("<I", cut_file, 100, 2)
    put("vlr_short.las", bytes(cut_file))
    with open(path("vlr_short.las"), "rb") as handle:
        message = error_of(lambda: read_claim(
            handle, parse_header(handle.read(375))), LasError)
    check("record header ends the file" in message,
          "a record list whose second header is cut off by the end of the "
          "file is refused by name")
    check(error_of(lambda: None) == "",
          "error_of reports no error as an empty message")

    # ---- hardening: a header range of 0 to 0
    h = Header()
    check(header_range_unchecked(h, 5.0, 9.0) is not None
          and "could not be cross-checked" in header_range_unchecked(
              h, 5.0, 9.0),
          "a header range of 0 to 0 over points that are not at 0 is "
          "reported as not cross-checked, not passed in silence"
          "  <-- pinned defect")
    check(header_range_unchecked(h, 0.0, 0.0) is None,
          "and over points that are all at 0 there is nothing to report")
    h.zmin, h.zmax = 1.0, 2.0
    check(header_range_unchecked(h, 5.0, 9.0) is None,
          "a recorded range is not an unchecked one")

    check(signed(-1e-7) == "+0.0000 m" and signed(-0.5) == "-0.5000 m"
          and signed(27.0) == "+27.0000 m",
          "a residual too small to show prints as +0.0000, never -0.0000"
          "  <-- pinned defect")

    # ---- the whole tool, one run per outcome
    def go(*argv):
        return run(*argv)

    ok = tile("ok.las")
    ctl_m = control_csv("ctl_m.csv")
    code, out, err = go(ok, "--control", ctl_m, "--ref-unit", "m")
    check(code == 0 and "VERDICT: AGREES" in out and err == "",
          "metres in the tile and the control agree: exit 0")
    check("8 compared, 0 skipped" in out
          and "header claims: NAVD88 height (EPSG:5703), unit metre" in out
          and "8 control point(s) read, 0 row(s) skipped" in out,
          "the report names the claim, the control count and the pairs")
    check("FINDING" not in out and "pairs furthest" not in out,
          "an agreeing tile prints no finding and no list of worst pairs")
    check(not any(word in out.replace(tmp, "") for word in (
        "ASSUMED", "ignored", "no point has class", "cross-checked",
        "still stand", "None", "UNIT_OFFSET", "parts per million"))
          and "reference: %s, 8 control" % ctl_m in out
          and "(named by the header)" in out,
          "and none of the conditional sentences (assumed, ignored, no "
          "ground, not cross-checked, still stand) is printed, while the "
          "reference is named by its path")

    feet_tile = tile("feet.las", f=lambda z: z / FOOT)
    code, out, _ = go(feet_tile, "--control", ctl_m, "--ref-unit", "m")
    check(code == 1 and "VERDICT: UNIT." in out
          and "fit the reference as international foot" in out
          and "2 parts per million" in out
          and "pairs furthest from the reference" in out,
          "a tile that claims metres and holds feet: exit 1, UNIT, with the "
          "worst pairs listed"
          "  <-- pinned defect")
    code, out, _ = go(feet_tile, "--control", ctl_m, "--ref-unit", "m",
                      "--sample", "0")
    check(code == 1 and "pairs furthest" not in out,
          "--sample 0 turns the worst-pairs list off")
    code, out, _ = go(feet_tile, "--control", ctl_m, "--ref-unit", "m",
                      "--sample", "2")
    check(out.count("  x 10") == 2, "--sample 2 lists two pairs")
    code, out, _ = go(feet_tile, "--control", ctl_m, "--ref-unit", "m",
                      "--sample", "1")
    check(out.count("  x 10") == 1, "--sample 1 lists one pair")
    code, out, _ = go(ok, "--control", ctl_m, "--ref-unit", "m",
                      "--min-points", "1")
    check(code == 0 and "8 compared" in out,
          "--min-points 1 is the smallest value allowed")

    low = tile("low27.las", f=lambda z: z - 27.0)
    code, out, _ = go(low, "--control", ctl_m, "--ref-unit", "m")
    check(code == 1 and "VERDICT: OFFSET." in out and "-27.0000 m" in out
          and "test the ellipsoid-height explanation first" in out
          and "cannot tell them apart" in out,
          "a steady 27 m shift: exit 1, OFFSET, the ellipsoid hint, and "
          "the admission that the cause is unknown"
          "  <-- pinned defect")
    code, out, _ = go(tile("low1.las", f=lambda z: z - 1.0),
                      "--control", ctl_m, "--ref-unit", "m")
    check(code == 1 and "VERDICT: OFFSET." in out
          and "test the ellipsoid-height" not in out,
          "a steady 1 m shift is OFFSET without the ellipsoid hint")
    code, out, _ = go(low, "--control", ctl_m, "--ref-unit", "m",
                      "--big-offset", "40")
    check("test the ellipsoid-height" not in out,
          "--big-offset moves the line above which the hint appears")
    code, out, _ = go(tile("ft_high.las", f=lambda z: (z + 3.0) / FOOT),
                      "--control", ctl_m, "--ref-unit", "m")
    check(code == 1 and "VERDICT: UNIT_OFFSET." in out
          and "only if they are read as international foot" in out,
          "feet that also sit 3 m high: UNIT_OFFSET, with the reading named")
    code, out, _ = go(ok, "--control", ctl_m, "--ref-unit", "m",
                      "--tolerance", "0.001")
    check(code == 0, "an exact tile passes even a 1 mm tolerance")
    code, out, _ = go(tile("off25.las", f=lambda z: z + 0.25),
                      "--control", ctl_m, "--ref-unit", "m")
    check(code == 0 and "VERDICT: AGREES" in out,
          "a 0.25 m bias is inside the default 0.30 m tolerance")

    rows = ["X,Y,Z"] + ["%d,%d,%.4f" % (x, y, [30.0, 11.0, 50.0, 12.0, 40.0,
                                                  13.0, 55.0, 14.0][i])
                        for i, (x, y) in enumerate(_CONTROL_XY)]
    code, out, _ = go(ok, "--control", put_text("ctl_scatter.csv",
                                                "\n".join(rows) + "\n"),
                      "--ref-unit", "m")
    check(code == 1 and "VERDICT: SCATTER." in out,
          "control that fits no single shift: exit 1, SCATTER")

    code, out, _ = go(ok, "--control", control_csv("ctl_ft.csv",
                                                   unit_scale=1 / FOOT),
                      "--ref-unit", "ft")
    check(code == 0 and "declared international foot" in out,
          "control in feet with --ref-unit ft is converted and agrees")
    code, out, _ = go(ok, "--control",
                      control_csv("ctl_usft.csv", unit_scale=1 / US_SURVEY_FOOT),
                      "--ref-unit", "usft")
    check(code == 0 and "declared US survey foot" in out,
          "control in US survey feet agrees with its own factor")
    code, out, _ = go(ok, "--control", ctl_m, "--ref-unit", "ft")
    check(code == 1, "declaring the control as feet when it is metres is "
                     "not hidden")

    code, out, _ = go(ok, "--control", control_csv("ctl_E.csv", header="E,N,H"),
                      "--ref-unit", "m", "--x-field", "E", "--y-field", "N",
                      "--z-field", "H")
    check(code == 0, "--x-field, --y-field and --z-field rename the columns")
    code, out, err = go(ok, "--control", ctl_m, "--ref-unit", "m",
                        "--z-field", "Height")
    check(code == 64 and "Height" in err,
          "a column the operator names that is not there is a usage error, "
          "exit 64, not a data error"
          "  <-- pinned defect")
    code, out, err = go(ok, "--control", put_text(
        "ctl_lower.csv", "x,y,z\n1020,2020,1\n"), "--ref-unit", "m")
    check(code == 64 and "x, y, z" in err and "case sensitive" in err
          and "--x-field, --y-field and --z-field" in err,
          "lowercase column names are refused with the flags that fix it  "
          "<-- pinned defect")
    code, out, err = go(ok, "--control", cp1252_csv, "--ref-unit", "m")
    check(code == 0 and "VERDICT: AGREES" in out and err == "",
          "a Windows-1252 control file gives a verdict, not exit 70"
          "  <-- pinned defect")
    code, out, err = go(ok, "--control", path("c_utf16.csv"), "--ref-unit",
                        "m")
    check(code == 0 and "8 control point(s) read" in out and err == "",
          "and so does a UTF-16 one  <-- pinned defect")
    code, out, _ = go(ok, "--control", put_text(
        "ctl_dirty.csv", "X,Y,Z\n1020,2020,%.4f\n,,\nx,y,z\n"
        % _terrain(1020, 2020)), "--ref-unit", "m")
    check(code == 4 and "2 row(s) skipped" in out
          and "NOT DECIDED" in out and "only 1 pair(s)" in out,
          "unreadable control rows are counted, and one good pair decides "
          "nothing: exit 4")
    code, out, _ = go(ok, "--control", put_text(
        "ctl_far.csv", "X,Y,Z\n9000,9000,1\n9100,9000,1\n"),
        "--ref-unit", "m")
    check(code == 4 and "0 compared, 2 skipped" in out,
          "control that lies outside the tile has no overlap: exit 4")
    code, out, _ = go(ok, "--control", ctl_m, "--ref-unit", "m",
                      "--min-points", "500")
    check(code == 4 and "0 compared, 8 skipped" in out,
          "--min-points above what any control point has leaves no pairs")
    code, out, _ = go(ok, "--control", ctl_m, "--ref-unit", "m",
                      "--radius", "0.5")
    check(code == 4, "--radius too small to reach a ground point: exit 4")

    flat = tile("flat.las", f=lambda z: 20.0 + 0.0 * z)
    flat_ctl = put_text("ctl_flat.csv", "X,Y,Z\n" + "\n".join(
        "%d,%d,%.4f" % (x, y, 17.0 + 0.001 * i)
        for i, (x, y) in enumerate(_CONTROL_XY)) + "\n")
    code, out, _ = go(flat, "--control", flat_ctl, "--ref-unit", "m")
    check(code == 4 and "too little to tell" in out
          and "Use control that spans more relief" in out,
          "a flat site with a 3 m bias cannot tell a shift from a unit "
          "error: NOT DECIDED, exit 4"
          "  <-- pinned defect")

    # ---- a grid as the reference
    def grid_file(name, scale=1.0, shift=0.0, x0=997.5):
        lines = []
        for r in range(21):
            y = 2100 - 5 * r
            lines.append(" ".join(
                "%.4f" % (_terrain(1000 + 5 * c, y) * scale + shift)
                for c in range(21)))
        return put_text(name, "ncols 21\nnrows 21\nxllcorner %s\n"
                        "yllcorner 1997.5\ncellsize 5\nNODATA_value -9999\n"
                        % x0 + "\n".join(lines) + "\n")

    grid_m = grid_file("dem_m.asc")
    code, out, err = go(ok, "--grid", grid_m, "--ref-unit", "m")
    check(code == 0 and "VERDICT: AGREES" in out
          and "441 compared, 0 skipped" in out and "21 x 21 grid" in out,
          "a tile against a grid in the same unit: exit 0, every ground "
          "point compared")
    code, out, _ = go(feet_tile, "--grid", grid_m, "--ref-unit", "m")
    check(code == 1 and "VERDICT: UNIT." in out
          and out.count("(1 point(s))") == 10,
          "grid mode finds the same unit error as control mode, and each "
          "pair is one ground point")
    code, out, _ = go(ok, "--grid", grid_m, "--ref-unit", "m",
                      "--max-samples", "100")
    check(code == 0 and "111 compared" in out,
          "--max-samples 100 takes every 4th point of 441, which is 111")
    code, out, _ = go(ok, "--grid", grid_m, "--ref-unit", "m",
                      "--max-samples", "1")
    check(code == 4 and "1 compared" in out,
          "--max-samples 1 is the smallest value allowed, and one pair "
          "decides nothing")
    code, out, _ = go(ok, "--grid", grid_file("dem_ft.asc", 1 / FOOT),
                      "--ref-unit", "ft")
    check(code == 0, "a grid in feet with --ref-unit ft agrees")
    code, out, _ = go(tile("g_low.las", f=lambda z: z - 27.0), "--grid",
                      grid_m, "--ref-unit", "m")
    check(code == 1 and "VERDICT: OFFSET." in out,
          "a steady shift is found against a grid too")
    for label, wrong, n_wrong, want in (
            ("40 percent of the points in feet under a metre label",
             lambda z: z / FOOT, 8, "Only 264 of 441 pairs (60 percent)"),
            ("45 percent of the points 3 m high", lambda z: z + 3.0, 9,
             "Only 242 of 441 pairs (55 percent)")):
        merged = tile("merged.las", points=[
            (x, y, wrong(_terrain(x, y)) if k % 20 < n_wrong
             else _terrain(x, y), 2) for k, (x, y) in enumerate(_lattice())])
        code, out, _ = go(merged, "--grid", grid_m, "--ref-unit", "m")
        check(code == 1 and "VERDICT: MIXED." in out and want in out
              and "VERDICT: AGREES" not in out
              and "pairs furthest from the reference" in out,
              "%s: MIXED and exit 1, not AGREES and exit 0  "
              "<-- pinned defect" % label)
    sentinel_rows = []
    for r in range(21):
        y = 2100 - 5 * r
        sentinel_rows.append(" ".join(
            "-3.40282347e+38" if r < 10 else "%.4f" % _terrain(1000 + 5 * c, y)
            for c in range(21)))
    sentinel = put_text("dem_sentinel.asc",
                        "ncols 21\nnrows 21\nxllcorner 997.5\n"
                        "yllcorner 1997.5\ncellsize 5\n"
                        "NODATA_value -3.4028234663852886e+38\n"
                        + "\n".join(sentinel_rows) + "\n")
    code, out, _ = go(ok, "--grid", sentinel, "--ref-unit", "m")
    check(code == 1 and "VERDICT: MIXED." in out
          and "Only 231 of 441 pairs (52 percent)" in out,
          "a NoData value written with different digits in the header and "
          "in the cells reads 210 cells as heights of -3e38: MIXED, not "
          "AGREES  <-- pinned defect")
    code, out, _ = go(ok, "--grid", grid_file("dem_far.asc", x0=50000.0),
                      "--ref-unit", "m")
    check(code == 4 and "0 compared, 441 skipped" in out,
          "a grid that does not cover the tile: exit 4, every point counted "
          "as skipped")
    code, out, err = go(ok, "--grid", put_text("bad.asc", "ncols 2\n"),
                        "--ref-unit", "m")
    check(code == 2 and "lacks" in err,
          "a malformed grid is a data error: exit 2")

    # ---- the claim decides what is checked
    nolabel = tile("nolabel.las", vlrs=[])
    code, out, err = go(nolabel, "--control", ctl_m, "--ref-unit", "m")
    check(code == 3 and "REFUSED" in out and "--assume-unit" in out
          and "FINDING" not in out,
          "a header with no vertical unit is refused: exit 3, never "
          "guessed")
    code, out, _ = go(nolabel, "--control", ctl_m, "--ref-unit", "m",
                      "--assume-unit", "m")
    check(code == 0 and "ASSUMED with --assume-unit" in out
          and "header claims: no vertical CRS" in out
          and "read as metre (assumed)" in out,
          "--assume-unit lets a tile with no claim be checked, and the "
          "report says the unit was assumed")
    code, out, _ = go(tile("coded.las", vlrs=_crs_vlrs(5701)),
                      "--control", ctl_m, "--ref-unit", "m",
                      "--assume-unit", "m")
    check(code == 0 and "unnamed vertical CRS (EPSG:5701), unit not stated"
          in out,
          "a code with no unit is reported as such, and --assume-unit "
          "fills the gap")
    code, out, _ = go(ok, "--control", ctl_m, "--ref-unit", "m",
                      "--assume-unit", "ft")
    check(code == 0 and "--assume-unit ignored" in out,
          "--assume-unit never overrides a unit the header names")
    code, out, _ = go(nolabel, "--control", ctl_m, "--ref-unit", "m",
                      "--assume-unit", "ft")
    check(code == 1 and "VERDICT: UNIT." in out,
          "an assumed unit that is wrong is a finding")

    contradict = tile("contradict.las", f=lambda z: z / US_SURVEY_FOOT,
                      vlrs=_crs_vlrs(6360, 9001))
    code, out, _ = go(contradict, "--control", ctl_m, "--ref-unit", "m")
    check(code == 1 and "FINDING   the vertical CRS EPSG:6360" in out
          and "VERDICT: AGREES" in out
          and "The header findings above still stand." in out,
          "a header that contradicts itself is a finding even when the "
          "values agree with the unit it resolves to"
          "  <-- pinned defect")
    code, out, _ = go(tile("badrange.las", zrange=(5.0, 60.0)),
                      "--control", ctl_m, "--ref-unit", "m")
    check(code == 1 and "records a Z range of 5.0000 to 60.0000" in out,
          "a header Z range the points do not match is a finding")
    code, out, _ = go(tile("zeroed.las", zrange=(0.0, 0.0)),
                      "--control", ctl_m, "--ref-unit", "m")
    check(code == 0 and "could not be cross-checked" in out
          and "FINDING" not in out,
          "a header range of 0 to 0 is a note, not a finding, and the run "
          "still decides"
          "  <-- pinned defect")
    nowkt = put("wkt_ft.las", _build_las(
        [(x, y, _terrain(x, y) / US_SURVEY_FOOT, 2) for x, y in _lattice()],
        minor=4, fmt=6, vlrs=[ftus], encoding=WKT_BIT))
    code, out, _ = go(nowkt, "--control", ctl_m, "--ref-unit", "m")
    check(code == 0 and "unit US survey foot (from the WKT)" in out,
          "a LAS 1.4 tile with a WKT claim of US survey feet is read, "
          "scanned and agrees")
    code, out, _ = go(put("bit_clear6.las", _build_las(
        tile_points(), minor=4, fmt=6, vlrs=_crs_vlrs(5703))),
        "--control", ctl_m, "--ref-unit", "m", "--assume-unit", "m")
    check(code == 1 and "FINDING   the WKT bit of the global encoding" in out
          and "ASSUMED" in out,
          "a claim lost to a clear WKT bit can be assumed, and the header "
          "error stays a finding")
    code, out, _ = go(put("bit_clear6b.las", _build_las(
        tile_points(), minor=4, fmt=6, vlrs=_crs_vlrs(5703))),
        "--control", ctl_m, "--ref-unit", "m")
    check(code == 3 and "FINDING   the WKT bit of the global encoding" in out,
          "a refusal still prints the header conflict that caused it")

    ground_none = tile("noground.las", points=[(x, y, _terrain(x, y), 1)
                                               for x, y in _lattice()])
    code, out, _ = go(ground_none, "--control", ctl_m, "--ref-unit", "m")
    check(code == 4 and "no point has class 2" in out,
          "a tile with no ground points says so and decides nothing")
    code, out, _ = go(ground_none, "--control", ctl_m, "--ref-unit", "m",
                      "--any-class")
    check(code == 0 and "no point has class" not in out,
          "--any-class compares points of every class, and does not say "
          "that nothing was compared")
    code, out, _ = go(ground_none, "--control", ctl_m, "--ref-unit", "m",
                      "--ground-class", "1")
    check(code == 0, "--ground-class names another class as the ground")
    code, out, _ = go(tile("veg.las", veg=True), "--control", ctl_m,
                      "--ref-unit", "m", "--any-class")
    check(code == 0, "a few non-ground points do not move a median")

    # ---- files that are not usable
    code, _, err = go(put("laz.las", _build_las(tile_points(),
                                                fmt_byte=0x81)),
                      "--control", ctl_m, "--ref-unit", "m")
    check(code == 2 and "LAZ" in err,
          "a LAZ tile is refused with its name: exit 2")
    code, _, err = go(put_text("junk.las", "not a las file at all" * 20),
                      "--control", ctl_m, "--ref-unit", "m")
    check(code == 2 and "not a LAS file" in err, "a non-LAS file: exit 2")
    code, _, err = go(path("missing.las"), "--control", ctl_m,
                      "--ref-unit", "m")
    check(code == 2 and "missing.las" in err,
          "a tile that does not exist: exit 2")
    code, _, err = go(ok, "--control", path("missing.csv"), "--ref-unit", "m")
    check(code == 2 and "missing.csv" in err,
          "a control file that does not exist: exit 2")
    code, _, err = go(put("cut.las", _build_las(tile_points(),
                                            vlrs=_crs_vlrs(5703))[:900]),
                      "--control", ctl_m, "--ref-unit", "m")
    check(code == 2 and "truncated" in err,
          "a tile cut short is a data error: exit 2")
    put_text("nul.csv", "X,Y,Z\n1,2,\x00\n")
    code, _, err = go(ok, "--control", path("nul.csv"), "--ref-unit", "m")
    check(code in (2, 4), "a control row with a NUL byte does not crash")

    # ---- usage errors are 64, and nothing is written
    hard_link = path("ok_link.las")
    os.link(ok, hard_link)
    for label, argv, word in (
            ("no tile", ["--control", ctl_m, "--ref-unit", "m"], "tile"),
            ("no reference", [ok, "--ref-unit", "m"], "exactly one"),
            ("two references", [ok, "--control", ctl_m, "--grid", grid_m,
                                "--ref-unit", "m"], "exactly one"),
            ("no --ref-unit", [ok, "--control", ctl_m], "--ref-unit"),
            ("tolerance 0", [ok, "--control", ctl_m, "--ref-unit", "m",
                             "--tolerance", "0"], "--tolerance"),
            ("tolerance nan", [ok, "--control", ctl_m, "--ref-unit", "m",
                               "--tolerance", "nan"], "--tolerance"),
            ("radius inf", [ok, "--control", ctl_m, "--ref-unit", "m",
                            "--radius", "inf"], "--radius"),
            ("radius nan", [ok, "--control", ctl_m, "--ref-unit", "m",
                            "--radius", "nan"], "--radius"),
            ("radius 0", [ok, "--control", ctl_m, "--ref-unit", "m",
                          "--radius", "0"], "--radius"),
            ("min-points 0", [ok, "--control", ctl_m, "--ref-unit", "m",
                              "--min-points", "0"], "--min-points"),
            ("sample -1", [ok, "--control", ctl_m, "--ref-unit", "m",
                           "--sample", "-1"], "--sample"),
            ("apply without out", [ok, "--control", ctl_m, "--ref-unit",
                                   "m", "--apply"], "--apply needs --out"),
            ("out is the tile", [ok, "--control", ctl_m, "--ref-unit", "m",
                                 "--out", ok], "also an input"),
            ("out is the control", [ok, "--control", ctl_m, "--ref-unit",
                                    "m", "--out", ctl_m], "also an input"),
            ("out is the grid", [ok, "--grid", grid_m, "--ref-unit", "m",
                                 "--out", grid_m], "also an input"),
            ("out is a hard link to the tile", [ok, "--control", ctl_m,
                                                "--ref-unit", "m", "--out",
                                                hard_link, "--apply"],
             "also an input"),
            ("out is the tile by another spelling", [
                ok, "--control", ctl_m, "--ref-unit", "m", "--out",
                os.path.join(tmp, ".", os.path.basename(ok))], "also an "
             "input"),
            ("a unit that is not offered", [ok, "--control", ctl_m,
                                            "--ref-unit", "yd"], "yd"),
            ("an unknown flag", [ok, "--control", ctl_m, "--ref-unit", "m",
                                 "--force"], "--force")):
        code, out, err = go(*argv)
        check(code == 64 and word in err and out == "",
              "usage error, exit 64 and no report: %s" % label)

    # ---- the write flag and the parser
    target = path("resid.csv")
    for prefix in ("--ap", "--app", "--appl"):
        code, out, err = go(ok, "--control", ctl_m, "--ref-unit", "m",
                            "--out", target, prefix)
        check(code == 64 and not os.path.exists(target) and out == "",
              "a prefix %s of --apply is REFUSED by the parser, and nothing "
              "is written  <-- pinned defect" % prefix)
    code, out, err = go("--self", ok)
    check(code == 64 and "--self" in err,
          "a prefix --self of --self-test is refused, not run"
          "  <-- pinned defect")
    code, out, err = go(ok, "--control", ctl_m, "--ref-unit", "m",
                        "--ou", target)
    check(code == 64 and not os.path.exists(target),
          "a prefix --ou of --out is refused too")
    check(_parse(["--self-test"]).self_test is True
          and _parse([]).apply is False,
          "the full flags parse, and --apply is off by default")
    code, out, err = go(ok, "--control", ctl_m, "--ref-unit", "m", "--out",
                        target)
    check(code == 0 and not os.path.exists(target)
          and "Check only" in out and "was not written" in out,
          "--out without --apply writes nothing and says so"
          "  <-- pinned defect")
    before = hashlib.sha256(slurp(ok)).hexdigest()
    code, out, err = go(feet_tile, "--control", ctl_m, "--ref-unit", "m",
                        "--out", target, "--apply")
    check(code == 1 and os.path.exists(target) and "wrote" in out,
          "--out with --apply writes the residual CSV, and a finding still "
          "exits 1")
    with open(target, "r", newline="") as handle:
        written = list(csv.DictReader(handle))
    check(list(written[0].keys()) == list(OUT_COLUMNS) and len(written) == 8,
          "the residual CSV has the documented columns and one row per pair")
    check(all(r["points"] == "13" for r in written),
          "each control point is compared with the 13 ground points of the "
          "5 unit lattice within 10 units of it, none skipped by a stride")
    check(abs(float(written[0]["residual_m"])) > 5.0
          and len(written[0]["residual_m"].split(".")[1]) == 4,
          "and its residuals are the ones for the unit the header claims, to "
          "four places")
    check(all(abs(float(r["lidar_z"]) - float(r["reference_z"])
                  - float(r["residual_m"])) < 3e-4 for r in written)
          and all(float(r["residual_m"]) > 5.0 for r in written),
          "each residual is the lidar Z minus the reference Z, so a tile "
          "that holds feet under a metre label sits high, not low"
          "  <-- pinned defect")
    code, out, _ = go(feet_tile, "--control", ctl_m, "--ref-unit", "m",
                      "--sample", "3", "--out", target, "--apply")
    with open(target, "r", newline="") as handle:
        written = list(csv.DictReader(handle))
    furthest = sorted((float(r["residual_m"]) for r in written),
                      key=lambda v: -abs(v))[:3]
    listed = [line for line in out.splitlines() if line.startswith("  x ")]
    check(len(listed) == 3 and all(
        ("%+.4f m" % v) in line for v, line in zip(furthest, listed)),
          "the worst pairs are listed furthest first, with the residual the "
          "CSV holds  <-- pinned defect")
    usft_tile = tile("usft_ft.las", f=lambda z: z / US_SURVEY_FOOT,
                     vlrs=_crs_vlrs(6360))
    code, out, _ = go(usft_tile, "--control", control_csv(
        "ctl_ft3.csv", unit_scale=1 / FOOT), "--ref-unit", "ft", "--out",
        target, "--apply")
    with open(target, "r", newline="") as handle:
        written = list(csv.DictReader(handle))
    check(code == 0 and len(written) == 8 and all(
        abs(float(r["residual_m"])) < 0.02
        and abs(float(r["residual_m"]) - (
            float(r["lidar_z"]) * US_SURVEY_FOOT
            - float(r["reference_z"]) * FOOT)) < 5e-4 for r in written),
          "a US survey foot tile against a control file in feet: each "
          "factor multiplies its own side, so the residuals are near zero"
          "  <-- pinned defect")
    go(ok, "--control", ctl_m, "--ref-unit", "m", "--out", target, "--apply")
    check(hashlib.sha256(slurp(ok)).hexdigest() == before,
          "the tile is byte for byte what it was after the runs")
    code, out, err = go(ok, "--control", ctl_m, "--ref-unit", "m", "--out",
                        tmp, "--apply")
    check(code == 2 and "could not write" in err,
          "an --out that cannot be opened is exit 2, not a crash")
    code, out, err = go("--help")
    check(code == 0 and "--apply" in out and "Nothing is written without"
          in out.replace("\n", " ").replace("  ", " "),
          "--help exits 0 and says nothing is written without --apply")

    # ---- the file run as a program, and imported
    saved_argv = sys.argv
    source_path = __file__.replace(".pyc", ".py")
    status, helped = "no exit", io.StringIO()
    try:
        sys.argv = ["zdatum.py", "--help"]
        with contextlib.redirect_stdout(helped):
            runpy.run_path(source_path, run_name="zdatum_imported")
            try:
                runpy.run_path(source_path, run_name="__main__")
            except SystemExit as exc:
                status = exc.code
    finally:
        sys.argv = saved_argv
    check(status == 0 and helped.getvalue().count("usage:") == 1,
          "run as a program the file exits with the status main returns, "
          "and imported it runs nothing")

    # ---- the source itself
    with open(__file__.replace(".pyc", ".py"), "rb") as handle:
        source = handle.read()
    check(max(source) < 128,
          "the source is pure ASCII")
    check(not re.search(br"^\s*(import|from)\s+(socket|urllib|http|ssl|"
                        br"requests|ftplib|smtplib)", source, re.M),
          "the tool imports nothing that can reach a network")
    check(not re.search(br"[^\w]f['\"]", source),
          "the source has no f-string, for Python 3.9")

    # ---- a console that cannot encode a character does not crash print()
    class Stream(object):
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kw):
            self.calls.append(kw)

    real_out, real_err = sys.stdout, sys.stderr
    fake_out, fake_err = Stream(), Stream()
    try:
        sys.stdout, sys.stderr = fake_out, fake_err
        _tolerate_odd_characters()
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    check(fake_out.calls == [{"errors": "replace"}]
          and fake_err.calls == [{"errors": "replace"}],
          "stdout and stderr are told to replace what they cannot encode")

    # ---- a crash is 70, never a finding
    real = globals()["_run"]

    def boom(argv):
        raise RuntimeError("synthetic fault")

    try:
        globals()["_run"] = boom
        code, out, err = go(ok)
    finally:
        globals()["_run"] = real
    check(code == 70 and "fault in the tool, not a finding" in err
          and "RuntimeError" in err,
          "an unexpected error exits 70, which no scheduled job reads as a "
          "finding"
          "  <-- pinned defect")

    # ---- the recorder records a failure
    inner = _Harness()
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        inner.check(False, "a failing check")
        inner.raises(lambda: None, "no error")
        inner.raises(lambda: 1 // 0, "wrong error")
        inner.raises(lambda: int("x"), "the right error")
        status = inner.footer()
    check(status == 1 and len(inner.failed) == 3 and inner.passed == 1,
          "check() and raises() really do record a failure"
          "  <-- pinned defect")
    check("4 assertions, 3 failed" in quiet.getvalue()
          and "FAILED: no error" in quiet.getvalue(),
          "the footer reports failures by count and by name, and exits 1")
    with contextlib.redirect_stdout(io.StringIO()) as empty:
        status = _Harness().footer()
    check(status == 0 and "0 assertions, 0 failed" in empty.getvalue(),
          "an empty run reports zero failures and exits 0")

    return harness.footer()
# SELF-TEST-END


if __name__ == "__main__":
    sys.exit(main())
