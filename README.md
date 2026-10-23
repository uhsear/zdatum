# zdatum

Check the Z values of a lidar tile against a reference surface you trust, and say whether they are
in the unit and the vertical datum the LAS header claims. It never changes the tile.

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

This tool adds a comparison. It does not add a new way to convert or to validate.

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

`gh search repos` on the day this was written returned nothing for `zdatum`, and nothing that checks
Z against a reference for the queries `lidar vertical datum`, `las header vertical datum`, `lidar
vertical accuracy check control points` and `lidar units feet meters mismatch` except the
conversion tool above. That shows only that the search found nothing. Desktop lidar packages may
have vertical accuracy reports. I did not look at those.

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
because a bias of scattered numbers is noise. The verdict follows.

| Verdict | Meaning | Exit |
|---|---|---|
| `AGREES` | The header's unit fits, and the median is inside the tolerance. | 0 |
| `UNIT` | The other unit fits. The label is wrong. | 1 |
| `OFFSET` | The unit fits, but the values sit a steady distance away. | 1 |
| `UNIT_OFFSET` | The other unit fits, and with a steady distance too. | 1 |
| `SCATTER` | Neither unit gives one steady shift. | 1 |
| `NOT DECIDED` | Too few pairs, no overlap, or a reference that cannot tell the units apart. | 4 |

A steady offset is what a different vertical datum, a different geoid model, or an ellipsoid
height read as an orthometric height looks like. The tool cannot tell those apart, and it says so.
When the offset is over `--big-offset` (5 m by default) it adds a hint to test the
ellipsoid-height explanation first. That threshold is a heuristic, not a standard.

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
- **LAZ** is refused with exit 2 by name. Decompress it first.

## Requirements

Python 3.9 or newer. The standard library only. No `arcpy`, no `laspy`, no network.

```
git clone https://github.com/uhsear/zdatum.git
python zdatum.py --self-test
```

The self-test prints the same assertions on every host listed here, and the output matches line for
line between Windows 3.13.2 and Ubuntu 3.12.3.

| Host | Python | Assertions |
|---|---|---|
| Windows | 3.13.2 | 233, 0 failed |
| Windows | 3.12 | 233, 0 failed |
| Windows | 3.9.25 | 233, 0 failed |
| Ubuntu | 3.12.3 | 233, 0 failed |

The counts are equal because no assertion depends on the platform. The self-test builds every
file it needs in a temporary directory and removes it, even when an assertion raises.

`coverage run --branch zdatum.py --self-test` reports 100 percent of statements and branches. One
line carries `# pragma: no cover`: the `if __name__ == "__main__":` line, whose false branch cannot
run when the file is the program.

## The self-test

```
$ python zdatum.py --self-test
zdatum self-test: temporary files only, no network, no credentials
--------------------------------------------------------------------
...
PASS  the international foot is exactly 0.3048 metre
...
PASS  metres claimed but the values are the reference in feet is UNIT  <-- pinned defect
...
PASS  a steady 27 m below the reference is OFFSET, units as claimed  <-- pinned defect
...
PASS  a reference near zero cannot tell metres from feet, so the answer is NOT DECIDED, not AGREES  <-- pinned defect
...
PASS  LAZ (point format plus 128) is refused by name, not read as junk  <-- pinned defect
...
PASS  an EVLR length of 2^63 is refused, not allocated  <-- pinned defect
...
PASS  point format 6 with the WKT bit clear reads the WKT record and names the error  <-- pinned defect
...
PASS  a prefix --ap of --apply is REFUSED by the parser, and nothing is written  <-- pinned defect
...
PASS  a prefix --appl of --apply is REFUSED by the parser, and nothing is written  <-- pinned defect
...
PASS  --out without --apply writes nothing and says so  <-- pinned defect
...
PASS  an unexpected error exits 70, which no scheduled job reads as a finding  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
...
--------------------------------------------------------------------
233 assertions, 0 failed
```

Each `...` is where this block is cut. The full run prints all 233 assertions. A line marked
`<-- pinned defect` is a bug found while building, or a failure the tool exists to catch. It
fails if the fix is removed. I checked that by breaking the code on purpose: 19 changes to the
tool each made the self-test fail, and one changed nothing. That one was a count check that the
loop already made unnecessary, and I deleted it.

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
| `--x-field`, `--y-field`, `--z-field` | Control CSV column names. Default `X`, `Y`, `Z`. |
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
self-test pins `--ap`, `--app` and `--appl`, and checks that nothing is written.

The tolerance is a bias test. It is not an accuracy standard and it is not taken from one. The 5 m
`--big-offset`, the 10 unit `--radius` and the 5 pair minimum are defaults I chose. They are
constants at the top of the file.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | The Z values agree with the reference in the header's unit. |
| 1 | At least one finding: a unit error, a steady offset, scatter, or a header that contradicts itself or its Z range. |
| 2 | A file could not be read, or is a format this tool does not read (LAZ). |
| 3 | The header names no vertical unit and `--assume-unit` was not given. |
| 4 | Not decided: too few pairs, no overlap, or a reference that cannot tell metres from feet. |
| 64 | Usage error, including a misspelt flag, a missing `--ref-unit`, and a CSV column that is not there. |
| 70 | An unexpected crash. It is never a finding. |

Exit 70 matters. Python exits 1 on an uncaught error, and a scheduled job would read that as "a
finding". A fault in the tool is not a fact about your data.

The residual CSV has the columns `x, y, lidar_z, reference_z, points, residual_m`, with
`residual_m` computed for the unit the header names.

## The reference

**Control points** are a CSV with a header row. Rows with a blank, a word or an infinity in X, Y or
Z are counted as skipped, never read as zero. A byte order mark is skipped.

**A grid** is an Esri ASCII raster. Per Esri's
[format page](https://resources.arcgis.com/en/help/main/10.1/009t/009t0000000z000000.htm), the
header holds `NCOLS`, `NROWS`, an origin as a corner or a centre, `CELLSIZE`, and an optional
`NODATA_VALUE` whose default is -9999. Row 1 is the top. The origin keys must match: both corner or
both centre. The tool refuses a grid that mixes them. Esri's page does not say whether the keywords
are case sensitive. The tool accepts either case. Values are sampled bilinearly between cell
centres. A sample that touches NoData gives no value and counts as skipped.

Neither reference is projected or converted. The tile and the reference must share a horizontal
coordinate system, and `--radius` is in the tile's horizontal units.

## Sources checked

Each fact the tool relies on, and where it was checked.

- **LAS 1.4 R15** ([ASPRS](https://www.asprs.org/wp-content/uploads/2019/07/LAS_1_4_r15.pdf)), read
  from a local copy. The WKT bit and Table 2 of the CRS representation. The rule that more than one
  GeoTIFF or WKT record is a file error. The "Superseded" convention. Max Z stored before Min Z.
  Formats 6 to 10 must use WKT.
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

The grid run above (`feet_labelled_m.las` against `dem_m.asc`) was repeated on Ubuntu 3.12.3 and
gave the same verdict and exit 1. With `--out resid.csv --apply` it wrote 441 rows plus the header.

A tile of 3 million points (90 MB) took about 1 second against a grid and about 7 seconds against
control points on this workstation. Control mode visits every ground point near every control point,
so it costs more per point.

## What it will not do

- **Name the datum.** A steady offset is one number. It does not say NAVD88 against an ellipsoid.
- **Check the horizontal coordinates.** They are assumed to share a system with the reference.
- **Tell one foot from the other.** The two feet differ by 2 parts per million. Z values at the
  heights of a normal survey cannot show it.
- **Trust a single bad control point.** The median resists one wild value. A reference with several
  bad points gives `SCATTER`, which is also a finding about the reference.
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
- **Withheld, synthetic and key-point flags are ignored.** For formats 0 to 5 the class is the low
  five bits of the class byte, so a class 2 point that carries a flag still counts as ground.
- **Control mode memory.** It keeps the ground heights near each control point. A dense tile with
  many control points holds many floats.
- **The ellipsoid hint is a heuristic.** Five metres is an assumption, not a figure from a
  standard.
- **The `if __name__ == "__main__":` line carries `# pragma: no cover`.** Every other statement and
  branch is covered.
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
