# zdatum

Check the Z values of a lidar tile against a reference surface you trust. It tells you when the
values are in a different unit than the LAS header claims, or sit a large steady distance from the
reference, or when part of the tile disagrees with the rest. It never changes the tile.

It does not confirm a vertical datum. A datum or geoid difference smaller than the tolerance (0.30 m
by default) reads as `AGREES`. The reference must itself be in the datum the header names. If it is
not, an `OFFSET` verdict is as likely to be a fault in the reference as in the tile.

Here is how the failure goes. A contractor delivers a whole project area of lidar tiles. Every header says
`NAVD88 height`, metres. One batch was cut from a source in US survey feet, and a script wrote the
numbers out with the metre label. Every reader opens the tiles without a word. The Z values look
like plausible numbers. Nobody checks, because nothing in the file can prove the claim.

The first sign is a surface that does not meet its neighbour. The second is a flood line that moved
by 40 metres of elevation. By then the tiles are in the terrain model and the model is in a
report. This page is a composite of a failure that is easy to produce, not a record of one case.

A header is a claim. A reference is evidence. This tool compares the two.

```
$ python zdatum.py feet_labelled_m.las --grid dem_m.asc --ref-unit m --sample 2
feet_labelled_m.las: LAS 1.4, point format 6, 441 point(s), 441 ground
header claims: NAVD88 height (EPSG:5703), unit metre (from the WKT)
reference: dem_m.asc, 21 x 21 grid, cell 5, declared metre
pairs: 441 compared, 0 skipped for lack of a reference value or of enough ground points. Tolerance 0.300 m.

relief of the reference: 11.000 m
  if stored Z is in the header's unit (metre): median +39.9150 m, spread 3.8780 m
  if stored Z is in the other unit    (international foot): median +0.0000 m, spread 0.0001 m

pairs furthest from the reference, if the header is right:
  x 1100.000 y 2100.000  lidar 75.4590  reference 23.0000  +52.4590 m  (1 point(s))
  x 1100.000 y 2095.000  lidar 74.6390  reference 22.7500  +51.8890 m  (1 point(s))

VERDICT: UNIT. The unit is metre (named by the header), but the Z values fit the reference as
international foot (median +0.0000 m, spread 0.0001 m). Do not use the Z values until the header or
the data is fixed. The international foot and the US survey foot differ by 2 parts per million,
too little for this check to say which one the data holds.
$ echo $?
1
```

That tile was written by laspy 2.7.0, not by this tool. The reference grid is synthetic: a plane
sampled on a 5 unit lattice. The VERDICT text is wrapped here for width. The generating scripts are
not in this repo, which holds four files. The self-test builds equivalent tiles itself.

## What already exists

This tool adds a quick comparison. It does not add a new way to convert, to validate, or to certify
accuracy.

- **Vertical accuracy checkpoints.** This is the usual way to check lidar heights, and it does a
  job this tool does not. The USGS page on its
  [updated accuracy standards](https://www.usgs.gov/ngp-standards-and-specifications/adopt-updated-accuracy-standards)
  (Lidar Base Specification 2024 rev. A, which follows the second edition of the ASPRS positional
  accuracy standards) tests a product against independent survey checkpoints. It names a vertical
  RMSE as the measure, at least 30 checkpoints, and a check source at least two times more accurate
  than the data. It ends in an accuracy statement you can put in a report. I read that page. I did
  not run any lidar package that implements the procedure. This tool gives no accuracy figure.
  It is a first look that tells you, before the formal check, that the unit or the level is wrong.
- **LIDAR-QAQC.** [gvonkrosigk/LIDAR-QAQC](https://github.com/gvonkrosigk/LIDAR-QAQC) is described as
  a USGS LiDAR QA/QC system for 3DEP vertical accuracy. Its README describes a web application
  that registers surfaces, imports checkpoints, samples raster or point-surface elevations and
  shows QA runs. I read the README. I did not run it. It is a database-backed application and this
  is a single file.
- **PDAL.** [`filters.reprojection`](https://pdal.io/en/latest/stages/filters.reprojection.html)
  moves X, Y and Z between coordinate systems. Its example converts ellipsoidal heights to
  orthometric heights with a PROJ geoid grid. It starts from the system you name or the one the file
  carries: its `in_srs` is the "Spatial reference system of the input data". Nothing on that page
  compares Z against a reference. Use PDAL to fix a tile once you know what is wrong. This tool is
  for finding out that something is.
- **lasvalidate.** [LASvalidator/lasvalidate](https://github.com/LASvalidator/lasvalidate) describes
  itself as "a simple tool (LGPL) to validate whether LAS/LAZ files conform to the LAS
  specification". I read that description and its README. I did not run it. A check of conformance
  looks at the file against the specification. It has no reference surface to compare Z against.
- **Lidar-Geoid-Conversion.** [djoshi1000/Lidar-Geoid-Conversion](https://github.com/djoshi1000/Lidar-Geoid-Conversion)
  is described as converting the horizontal and vertical datum of an aerial point cloud. I did not
  run it or read its code, so I cannot say whether it checks its input.

`gh search repos` on 2026-10-29 returned only this repository for `zdatum`. It returned nothing
that checks Z against a reference for `lidar vertical datum check`, `las header vertical datum`,
`lidar vertical accuracy check control points`, `lidar units feet meters mismatch` and `las z units
feet metres validate`. The queries `lidar qaqc` and `lidar geoid` found the two projects above. That
shows only that the search found nothing more. Desktop lidar packages may have vertical accuracy
reports. I did not look at those.

## What it decides

You supply a reference. It is either survey control points (a CSV) or a grid (an Esri ASCII
raster). You also say the Z unit of the reference with `--ref-unit`. The unit of the reference is
never guessed.

The tool takes the ground points of the tile (class 2). It pairs them with the reference. For
control points, each pair is the median ground height within `--radius` of the point. For a grid,
each pair is one ground point and the grid value at its position.

Then it asks two questions of the residuals, which are lidar minus reference, in metres:

1. If the stored Z is in the unit the header names, what is the median residual and its spread?
2. If it is in the other unit (metres against feet), what are they?

The spread is the median absolute deviation. A median counts only when the spread is small,
because a bias of scattered numbers is noise. A median and a spread both ignore a minority, so it
also counts only when at least 90 percent of the pairs lie within the tolerance of it. Without
that, a tile that is 40 percent wrong still reads `AGREES`. The verdict follows.

| Verdict | Meaning | Exit |
|---|---|---|
| `AGREES` | The header's unit fits, the median is inside the tolerance, and 90 percent of the pairs are. | 0 |
| `UNIT` | The other unit fits. The label is wrong. | 1 |
| `OFFSET` | The unit fits, but the values sit a steady distance away. | 1 |
| `UNIT_OFFSET` | The other unit fits, and with a steady distance too. | 1 |
| `SCATTER` | Neither unit gives one steady shift. | 1 |
| `MIXED` | One shift fits most pairs, but fewer than 90 percent of them. Part of the tile is wrong. | 1 |
| `NOT DECIDED` | Too few pairs, no overlap, or a reference that cannot tell the units apart. | 4 |

A steady offset is what a different vertical datum, a different geoid model, or an ellipsoid
height read as an orthometric height looks like. The tool cannot tell those apart, and it says so.
When the offset is over `--big-offset` (5 m by default) it adds a hint to test the
ellipsoid-height explanation first. That threshold is a heuristic, not a standard.

`MIXED` is what a tile merged from two deliveries looks like, and what a reference with bad cells
looks like. This run has 40 percent of the points in feet under a metre label:

```
$ python zdatum.py mixed.las --grid dem_m.asc --ref-unit m --sample 2
relief of the reference: 11.000 m
  if stored Z is in the header's unit (metre): median +0.0000 m, spread 0.0000 m
  if stored Z is in the other unit    (international foot): median -10.5670 m, spread 3.8236 m

pairs furthest from the reference, if the header is right:
  x 1100.000 y 2100.000  lidar 75.4600  reference 23.0000  +52.4600 m  (1 point(s))
  x 1100.000 y 2035.000  lidar 64.8000  reference 19.7500  +45.0500 m  (1 point(s))

VERDICT: MIXED. Only 264 of 441 pairs (60 percent) lie within 0.300 m of the one shift that fits
best (metre, median +0.0000 m), and at least 90 percent must. Part of the tile disagrees with the
rest: two deliveries merged, two units in one file, NoData read as heights, or a reference with bad
cells.
$ echo $?
1
```

The median and the spread of that tile are both zero. Before the share check it printed `AGREES`
and exited 0.

`NOT DECIDED` is the important answer. A flat site cannot tell metres from feet, because the same
numbers fit both with a shift. Here the reference spans 0.12 m and the tile is 27 m low:

```
relief of the reference: 0.120 m
  if stored Z is in the header's unit (metre): median -27.0000 m, spread 0.0000 m
  if stored Z is in the other unit    (international foot): median -22.1962 m, spread 0.0348 m

VERDICT: NOT DECIDED. the reference spans only 0.120 m, too little to tell metre from international foot. Both explanations fit the residuals equally well. Use control that spans more relief.
```

The exit code is 4. It is not 0. A tool that said `OFFSET` here would be guessing.

## What it reads from the header

- **The vertical unit.** From the GeoTIFF keys (LAS record 34735) or the WKT record (2112).
- **Which record counts.** The LAS 1.4 specification says the WKT bit of the global encoding picks
  the record, and point formats 6 to 10 must use WKT. A file with the bit clear and format 6 to 10 is
  "Error" in the specification's table. The tool reads the WKT record, and reports the clear bit as a
  finding.
- **Duplicates.** The specification calls more than one GeoTIFF or WKT record "a file error". The
  tool reads the first, and reports the duplicate, and reports it again if the records name
  different units. It also reports a WKT record and a GeoTIFF record that name different units.
- **Superseded records.** The specification says a superseded record has its user ID changed to
  `LASF_Spec`. The tool ignores those.
- **The Z range.** The header stores max Z before min Z. A range that does not match the points is a
  finding. A range of 0 to 0 is a note ("could not be cross-checked"), not a finding, because a
  writer that never filled the field leaves it that way.
- **A header with no vertical unit** is refused with exit 3. Give `--assume-unit` if you know it.
  The datum itself is never assumed.
- **Record lengths and counts** come from the file, so each is checked against the bytes that
  remain before it is read. A scale factor or offset that is not a finite number is refused.
- **LAZ** is refused with exit 2 by name. Decompress it first.

## Requirements

Python 3.9 or newer. The standard library only. No `arcpy`, no `laspy`, no network. The self-test
makes one hard link in its temporary directory, so that directory must be on a file system that
supports them (NTFS and ext4 do).

```
git clone https://github.com/uhsear/zdatum.git
python zdatum.py --self-test
```

The self-test prints the same assertions on every host listed here. After the carriage returns of
Windows are removed, the output of Windows 3.9.25 and 3.13.2 matches the output of Ubuntu 3.12.3
line for line.

| Host | Python | Assertions |
|---|---|---|
| Windows | 3.13.2 | 301, 0 failed |
| Windows | 3.12 | 301, 0 failed |
| Windows | 3.9.25 | 301, 0 failed |
| Ubuntu | 3.12.3 | 301, 0 failed |

The counts are equal because no assertion depends on the platform. The self-test builds every
file it needs in a temporary directory and removes it, even when an assertion raises.

`coverage run --branch zdatum.py --self-test` reports 100 percent of statements and branches, and
excludes nothing. The self-test runs the file a second time with `runpy`, once under the name
`__main__` and once under another name, so both sides of the `if __name__ == "__main__":` line run.

## The self-test

```
$ python zdatum.py --self-test
zdatum self-test: temporary files only, no network, no credentials
--------------------------------------------------------------------
PASS  the international foot is exactly 0.3048 metre
...
PASS  metres claimed but the values are the reference in feet is UNIT  <-- pinned defect
...
PASS  a steady 27 m below the reference is OFFSET, units as claimed  <-- pinned defect
...
PASS  a reference near zero cannot tell metres from feet, so the answer is NOT DECIDED, not AGREES  <-- pinned defect
...
PASS  two pairs of ten wrong by 40 m is 80 percent, which is MIXED, not AGREES  <-- pinned defect
...
PASS  LAZ (point format plus 128) is refused by name, not read as junk  <-- pinned defect
...
PASS  a control CSV saved by Excel as Windows-1252, with an accent and a degree sign in a description column, reads: X, Y and Z are ASCII  <-- pinned defect
...
PASS  an EVLR length of 2^63 is refused, not allocated  <-- pinned defect
...
PASS  point format 6 with the WKT bit clear reads the WKT record and names the error  <-- pinned defect
...
PASS  40 percent of the points in feet under a metre label: MIXED and exit 1, not AGREES and exit 0  <-- pinned defect
...
PASS  a NoData value written with different digits in the header and in the cells reads 210 cells as heights of -3e38: MIXED, not AGREES  <-- pinned defect
...
PASS  a prefix --ap of --apply is REFUSED by the parser, and nothing is written  <-- pinned defect
...
PASS  a prefix --appl of --apply is REFUSED by the parser, and nothing is written  <-- pinned defect
...
PASS  --out without --apply writes nothing and says so  <-- pinned defect
...
PASS  each residual is the lidar Z minus the reference Z, so a tile that holds feet under a metre label sits high, not low  <-- pinned defect
...
PASS  an unexpected error exits 70, which no scheduled job reads as a finding  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
...
--------------------------------------------------------------------
301 assertions, 0 failed
```

Each `...` is where this block is cut. The full run prints all 301 assertions. A line marked
`<-- pinned defect` is a bug found while building, or a failure the tool exists to catch. It
fails if the fix is removed.

I also broke the decision and command code on purpose with 769 small changes: a comparison
flipped, a constant moved by one, a branch forced on or off. The self-test failed for 735 of them.
The other 34 change nothing a caller can see. They are default values that every caller overwrites,
a `1e-9` slack that no input can land on, a read size that only changes how many bytes are read at
once, a test that is already implied by the one after it, and a few characters of a slice that no later
step reads. The harness is not in this repo.

## Quick start

```
python zdatum.py --self-test
python zdatum.py tile.las --control control.csv --ref-unit m
python zdatum.py tile.las --grid dem.asc --ref-unit ft
python zdatum.py tile.las --control control.csv --ref-unit m --tolerance 0.2
python zdatum.py tile.las --control control.csv --ref-unit m --assume-unit m
python zdatum.py tile.las --control control.csv --ref-unit m --out residuals.csv --apply
```

## Usage

| Flag | Meaning |
|---|---|
| `TILE` | The LAS file. LAZ is refused. |
| `--control FILE` | Reference: a CSV of survey control points. |
| `--grid FILE` | Reference: an Esri ASCII raster. Name exactly one of `--control` and `--grid`. |
| `--ref-unit {m,ft,usft}` | The Z unit of the reference. Required. |
| `--assume-unit {m,ft,usft}` | The tile's Z unit, used only when the header names none. |
| `--x-field`, `--y-field`, `--z-field` | Control CSV column names. Default `X`, `Y`, `Z`. Names are case sensitive and are not trimmed. |
| `--tolerance M` | Metres a median residual or its spread may reach. Default 0.30. |
| `--radius R` | How far a ground point may be from a control point, in the tile's horizontal units. Default 10. |
| `--min-points N` | Ground points needed near a control point. Default 3. |
| `--max-samples N` | Grid mode: about this many ground points are compared. Default 20000. |
| `--ground-class N` | The class that means bare earth. Default 2. |
| `--any-class` | Compare points of every class. Vegetation and buildings bias the result high. |
| `--big-offset M` | Offset above which the ellipsoid-height hint is printed. Default 5. |
| `--sample N` | How many worst pairs to list. Default 10. |
| `--out FILE` | Path for the residual CSV. |
| `--apply` | Write `--out`. Without it nothing is written. |
| `--self-test` | Run the offline assertions and exit. |

The only file this tool can write is the `--out` CSV, and only with `--apply`. The parser is built
with `allow_abbrev=False`. A prefix of a flag is refused, so `--ap` does not mean `--apply`. The
self-test pins `--ap`, `--app` and `--appl`, and checks that nothing is written. It also checks that
`--out` is refused when it names the tile, the control file or the grid, including by a hard link
or by a `./` spelling.

The tolerance is a bias test. It is not an accuracy standard and it is not taken from one. The 5 m
`--big-offset`, the 10 unit `--radius` and the 3 point `--min-points` are defaults I chose. The 5 pair
minimum and the 90 percent share are constants at the top of the file, not flags.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | The Z values agree with the reference in the header's unit. |
| 1 | At least one finding: a unit error, a steady offset, scatter, a mixed tile, or a header that contradicts itself or its Z range. |
| 2 | A file could not be read, or is a format this tool does not read (LAZ). |
| 3 | The header names no vertical unit and `--assume-unit` was not given. |
| 4 | Not decided: too few pairs, no overlap, or a reference that cannot tell metres from feet. |
| 64 | Usage error, including a misspelt flag, a missing `--ref-unit`, and a CSV column that is not there. |
| 70 | An unexpected crash. It is never a finding. |

A header finding overrides the verdict. A duplicate CRS record, a Z range that does not match the
points, or a clear WKT bit on point format 6 to 10 makes the exit code 1 even when the verdict is
`AGREES` (which would be 0) or `NOT DECIDED` (which would be 4).

Exit 70 matters. Python exits 1 on an uncaught error, and a scheduled job would read that as "a
finding". A fault in the tool is not a fact about your data.

The residual CSV has the columns `x, y, lidar_z, reference_z, points, residual_m`, with
`residual_m` computed for the unit the header names: lidar Z times that unit's factor, minus the
reference Z times its factor, in metres.

## The reference

**Control points** are a CSV with a header row. Rows with a blank, a word or an infinity in X, Y or
Z are counted as skipped, never read as zero. A byte order mark is skipped. A file saved as
Windows-1252 (Excel's "CSV") or as UTF-16 (Excel's "Unicode Text", PowerShell's
`-Encoding Unicode`) is read: a byte that is not valid UTF-8 is replaced, because X, Y and Z are
plain numbers. Column names are matched exactly. Lowercase `x,y,z` exits 64, and the message names
`--x-field`, `--y-field` and `--z-field`.

**A grid** is an Esri ASCII raster. Per Esri's
[format page](https://resources.arcgis.com/en/help/main/10.1/009t/009t0000000z000000.htm), the
header holds `NCOLS`, `NROWS`, an origin as a corner or a centre, `CELLSIZE`, and an optional
`NODATA_VALUE` whose default is -9999. Row 1 is the top. The origin keys must match: both corner or
both centre. The tool refuses a grid that mixes them. Esri's page does not say whether the keywords
are case sensitive. The tool accepts either case. Values are sampled bilinearly between cell
centres. A sample that touches NoData gives no value and counts as skipped.

Only these two forms are read. A GeoTIFF or a file geodatabase DEM must be exported to an ASCII
grid first, for example with the ArcGIS Pro
[Raster to ASCII](https://pro.arcgis.com/en/pro-app/latest/tool-reference/conversion/raster-to-ascii.htm)
tool.

Neither reference is projected or converted. The tile and the reference must share a horizontal
coordinate system, and `--radius` is in the tile's horizontal units.

## Sources checked

Each fact the tool relies on, and where it was checked.

- **LAS 1.4 R15**, published by ASPRS, read from a local copy. The maintained text is the
  [ASPRSorg/LAS](https://github.com/ASPRSorg/LAS) repository. The PDF address ASPRS used for R15
  (`asprs.org/wp-content/uploads/2019/07/LAS_1_4_r15.pdf`) returned HTTP 404 on 2026-10-29, so it is
  not linked. The WKT bit and Table 2 of the CRS representation. The rule that more than one
  GeoTIFF or WKT record is a file error. The "Superseded" convention. Max Z stored before Min Z.
  Formats 6 to 10 must use WKT. The record length of each point format.
- **The foot.** The international foot is exactly 0.3048 m. The US survey foot is 1200/3937 m.
  [NIST](https://www.nist.gov/pml/us-surveyfoot) and NOAA's National Geodetic Survey
  [deprecated](https://geodesy.noaa.gov/web/news/pdf/Notice-Final-Determination-Retiring-US-Survey-Foot.pdf)
  the survey foot as of 31 December 2022. Files made before then still carry it. The two feet differ
  by about 2 parts per million, which Z values at survey heights cannot show.
- **EPSG codes and units.** Checked with PROJ 9.8.1 through pyproj 3.8.0: 5703 NAVD88 height (metre),
  6360 NAVD88 height (ftUS), 8228 NAVD88 height (ft), 5702 NGVD29 height (ftUS), 7968 NGVD29 height
  (m), 5714 MSL height, 5773 EGM96 height, 3855 EGM2008 height. Unit codes 9001, 9002 and 9003 are
  metre, foot and US survey foot, with factors 1, 0.3048 and 0.30480060960121924.
- **GeoTIFF keys.** 4096 `VerticalCSTypeGeoKey`, 4097 `VerticalCitationGeoKey`, 4099
  `VerticalUnitsGeoKey`, and tag 34737 for the ASCII parameters. I checked these against
  implementation code in [deegree](https://download.deegree.org/deegree2.5/api/src-html/org/deegree/io/geotiff/GeoTiffKey.html)
  and [WebWorldWind](https://worldwind.arc.nasa.gov/autodocs/WebWorldWind/formats_geotiff_GeoTiffConstants.js.html).
  The GeoTIFF specification page itself was not reachable when I tried, so these are not checked
  against the specification text.
- **Esri ASCII raster.** The [format page](https://resources.arcgis.com/en/help/main/10.1/009t/009t0000000z000000.htm):
  the header keys, the -9999 default, row 1 at the top, and the origin match rule. The ArcGIS Pro
  [ASCII to Raster](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/conversion/ascii-to-raster.html)
  page names the centre alternative to the corner keys.
- **Vertical accuracy checkpoints.** The USGS
  [page on updated accuracy standards](https://www.usgs.gov/ngp-standards-and-specifications/adopt-updated-accuracy-standards),
  for the sentences in "What already exists".
- **PDAL.** The [reprojection stage](https://pdal.io/en/latest/stages/filters.reprojection.html), for
  the sentences in "What already exists".

## Real runs

These ran on Windows, Python 3.13.2, against tiles written by laspy 2.7.0 with a compound CRS
(`EPSG:6439+5703`, and `EPSG:6443+6360` for feet). Each tile holds 441 ground points on a 5 unit
lattice over a sloping plane with 11 m of relief. The control file has 8 points. The cases:

| Tile | Stored Z | Verdict | Exit |
|---|---|---|---|
| `good_m.las` | metres, labelled metres | `AGREES` | 0 |
| `good_usft.las` | US survey feet, labelled US survey feet | `AGREES` | 0 |
| `feet_labelled_m.las` | feet, labelled metres | `UNIT` | 1 |
| `usft_as_m_label.las` | US survey feet, labelled metres | `UNIT` (as international foot, with the 2 ppm note) | 1 |
| `ellipsoid_like.las` | metres, 27 m low | `OFFSET`, with the ellipsoid hint | 1 |
| `flat_low.las` | 0.12 m of relief, 27 m low | `NOT DECIDED` | 4 |

I ran these again on 2026-10-29 against the current file. The verdicts and exit codes did not
change. The `MIXED` run above used a tile the self-test builder wrote, not laspy.

The grid run above (`feet_labelled_m.las` against `dem_m.asc`) was repeated on Ubuntu 3.12.3 and
gave the same verdict and exit 1. With `--out resid.csv --apply` it wrote 441 rows plus the header.

A tile of 3.0 million points (90 MB, point format 6) took 1.7 to 2.0 seconds against a grid and
9.6 to 10.2 seconds against control points. These are three runs each on this Windows workstation
with Python 3.13.2. Control mode visits every ground point near every control point, so it costs
more per point.

## What it will not do

- **Name the datum.** A steady offset is one number. It does not say NAVD88 against an ellipsoid.
- **See a small datum shift.** A shift under `--tolerance` reads as `AGREES`.
- **Check the horizontal coordinates.** They are assumed to share a system with the reference.
- **Tell one foot from the other.** The two feet differ by 2 parts per million. Z values at the
  heights of a normal survey cannot show it.
- **Trust a single bad control point.** The median resists one wild value. A reference with several
  bad points gives `SCATTER` or `MIXED`, which is also a finding about the reference.
- **Decide without relief.** See `NOT DECIDED`.
- **Read LAZ.** Decompress it first.
- **Repair anything.** It writes no tile. Use PDAL or your lidar package to fix one.

## Limits

- **The GeoTIFF vertical keys were exercised with a writer in the self-test only.** laspy 2.7.0
  writes no vertical GeoTIFF keys, and no PDAL, GDAL or LAStools was on either host. The WKT path
  was exercised with files laspy wrote. The key path was exercised with keys the self-test builds
  from the specification, plus one laspy LAS 1.2 file that holds no CRS record.
- **LAZ is exercised with a flipped format byte, not a real LAZ file.** The refusal is by the
  compression bit in the point format byte, which is how the LAS specification marks it.
- **Real survey data were not used.** Every tile and every control file is synthetic. The numbers
  show the logic, not the accuracy on a real site.
- **A reference grid coarser than the terrain relief allows gives `SCATTER` on a correct tile.**
  On a synthetic surface with 10 m of amplitude and a 75 m wavelength, cells of 10 m left a spread of
  0.35 m, which is over the default 0.30 m. Cells of 20 m left 1.6 m and cells of 30 m left 2.7 m.
  Raise `--tolerance` or use a finer reference on hilly ground.
- **The 90 percent share is a constant I chose.** Ground points that sit on something else count
  against it, and `--any-class` makes that worse.
- **NoData is matched by exact float equality with the header value.** A sentinel written with
  different precision in the header and in the cells (for example `-3.4028234663852886e+38` against
  `-3.40282347e+38`) is read as real data. The `MIXED` verdict catches it only when it touches more
  than a tenth of the pairs.
- **Grid points closer than half a cell to the raster edge get no reference value** and count as
  skipped. So do points on the first or last cell centre that float rounding places a hair outside.
- **The check that the file holds the promised number of points counts trailing EVLR bytes as point
  room.** An over-stated point count on a LAS 1.4 file with EVLRs reads EVLR bytes as points
  instead of exiting 2. The run I tried ended in `NOT DECIDED` with no pairs.
- **A header Z range that is NaN skips the range cross-check silently.** Every comparison with NaN
  is false.
- **A scale, offset or radius extreme enough to overflow a float to infinity exits 70.** It raises
  `OverflowError` in `Controls._cell`. `--radius 1e-320` does it.
- **A file that is shorter than the header promises because it changed during the read exits 70**
  with a `struct.error`, not 2.
- **For a WKT vertical CRS with no `AUTHORITY` of its own, the EPSG code shown is the last EPSG
  authority inside the block.** That can be the datum's code (5103) or the unit's (9001). It is
  display only. The unit is read separately.
- **A WKT vertical record whose EPSG code and unit disagree raises no finding.** `AUTHORITY
  ["EPSG","5703"]` (metre) with `UNIT["US survey foot",...]` is read as US survey feet. The GeoTIFF
  key path flags the same contradiction, so the two paths differ.
- **A WKT vertical unit that is not metre, foot or US survey foot** (a decimetre, say) is treated as
  no unit. With `--assume-unit` the report then says "ASSUMED ... The header did not say", though it
  did name a unit.
- **Esri-flavoured WKT that spells the vertical CRS `VERTCS[...]` is not recognised.** The tool
  reports that the header names no vertical unit and exits 3. That is a refusal, not a wrong
  verdict, but the message does not say that the record was present and not parsed.
- **GeoTIFF vertical keys are read as direct values** even when the key says the value lives in
  another tag.
- **The WKT bit of the global encoding is read for LAS 1.0 to 1.3 files too,** where the bit is not
  defined. A stray set bit sends the read to the WKT record and can end in exit 3.
- **The residual CSV is written even when the verdict is `NOT DECIDED`,** and then holds a header
  row and few or no rows.
- **`--apply` overwrites an existing `--out` file without asking.** Only the tile, the control file
  and the grid are protected, and they are compared as files, so a hard link is caught.
- **The `--out` file is written in place.** A disk-full or I/O error part-way through leaves a
  partial CSV, and the tool exits 2 after it printed the verdict.
- **A control CSV column name with a space in it needs the space in the flag.** `--z-field " Z"`
  reads a header `X,Y, Z`.
- **Only Esri ASCII grids and control CSVs are read.** The grid reader loads the whole file into
  memory and splits it into tokens, so a grid of hundreds of millions of cells needs many gigabytes
  and may exit 70 with `MemoryError`.
- **Text taken from the files is printed raw,** including control and escape characters: the
  vertical CRS name in the header, the CSV header names in a column error, and the tile path. A
  hostile file can write terminal escape sequences. Do not pipe the output into something that
  interprets them.
- **Output piped to a program that closes early** (`zdatum.py ... | head -1`) can end with a Python
  flush error or a non-zero exit that does not mean a finding. The tool does not handle a closed
  stdout.
- **Ctrl-C ends with Python's traceback and the signal exit status,** not one of the documented
  exit codes.
- **Withheld, synthetic and key-point flags are ignored.** For formats 0 to 5 the class is the low
  five bits of the class byte, so a class 2 point that carries a flag still counts as ground.
- **Control mode memory.** It keeps the ground heights near each control point. A very dense tile
  with many control points and a large `--radius` holds many floats.
- **The ellipsoid hint is a heuristic.** Five metres is an assumption, not a figure from a
  standard.
- **No network and no credentials.** The tool reads local files only. The self-test checks that its
  own source imports no network module, so no stub server was needed.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [datumcheck](https://github.com/uhsear/datumcheck) - the horizontal side of the same question:
  exit non-zero when PROJ picks a null-shift operation
- [nodatalie](https://github.com/uhsear/nodatalie) - the same habit of trusting stored values only
  after a recomputation, applied to raster statistics
