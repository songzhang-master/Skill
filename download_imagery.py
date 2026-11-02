"""Download imagery using either a reference raster or a WGS84 bounding box."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

EARTH_RADIUS = 6378137.0
HALF_WORLD = math.pi * EARTH_RADIUS
MAX_LATITUDE = math.degrees(math.atan(math.sinh(math.pi)))
PROVIDER_SCRIPTS = {
    "esri": "download_esri_world_imagery.py",
    "eox": "download_eox_wmts.py",
    "bing": "download_bing_imagery.py",
}
PROVIDER_OPTIONS = {
    "esri": ("block_size", "service_url"),
    "eox": ("zoom", "layer", "tile_matrix_set", "capabilities_url"),
    "bing": ("zoom", "url_template"),
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=PROVIDER_SCRIPTS)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--reference-raster", type=Path,
                        help="Existing north-up EPSG:3857 DEM or other reference raster")
    inputs.add_argument("--bbox", nargs=4, type=float,
                        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                        help="WGS84 longitude/latitude bounds in decimal degrees")
    parser.add_argument("--resolution", type=float,
                        help="Required with --bbox: EPSG:3857 map metres per output pixel")
    parser.add_argument("--out", required=True, type=Path, help="New output RGB GeoTIFF")
    parser.add_argument("--work-dir", required=True, type=Path, help="Persistent workspace/cache")
    parser.add_argument("--raw-vrt", type=Path, help="EOX only: optional new raw-tile VRT path")
    parser.add_argument("--zoom", type=int, help="EOX: defaults to 13; Bing: required (1-23)")
    parser.add_argument("--layer", help="EOX layer; defaults to s2cloudless-2025_3857")
    parser.add_argument("--tile-matrix-set", help="EOX matrix set; defaults to GoogleMapsCompatible")
    parser.add_argument("--block-size", type=int, help="Esri export block side; defaults to 2048")
    parser.add_argument("--service-url", help="Override the Esri export endpoint")
    parser.add_argument("--capabilities-url", help="Override the EOX WMTS capabilities URL")
    parser.add_argument("--url-template", help="Override the Bing tile URL template")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print grid and command without downloading or writing files")
    args = parser.parse_args(argv)
    if args.bbox is not None and args.resolution is None:
        parser.error("--bbox requires --resolution (map metres per pixel)")
    if args.reference_raster is not None and args.resolution is not None:
        parser.error("--resolution is only used with --bbox; reference mode preserves its grid")
    for option in {item for items in PROVIDER_OPTIONS.values() for item in items}:
        if getattr(args, option) is not None and option not in PROVIDER_OPTIONS[args.source]:
            parser.error(f"--{option.replace('_', '-')} is not supported for {args.source}")
    if args.raw_vrt is not None and args.source != "eox":
        parser.error("--raw-vrt is only supported for eox")
    if args.source == "bing" and (args.zoom is None or not 1 <= args.zoom <= 23):
        parser.error("bing requires --zoom between 1 and 23")
    if args.source == "eox" and args.zoom is not None and args.zoom < 0:
        parser.error("--zoom must be non-negative")
    if args.block_size is not None and not 256 <= args.block_size <= 4096:
        parser.error("--block-size must be between 256 and 4096")
    return args


def grid_from_bbox(bbox, resolution):
    """Cover the requested box with whole square pixels, expanding by <1 pixel/axis."""
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise ValueError("--bbox needs four finite numbers: WEST SOUTH EAST NORTH")
    west, south, east, north = bbox
    if not -180 <= west < east <= 180:
        raise ValueError("Require -180 <= WEST < EAST <= 180; split antimeridian-crossing areas")
    if not -MAX_LATITUDE <= south < north <= MAX_LATITUDE:
        raise ValueError(f"Require -{MAX_LATITUDE:.12f} <= SOUTH < NORTH <= {MAX_LATITUDE:.12f}")
    if resolution is None or not math.isfinite(resolution) or resolution <= 0:
        raise ValueError("--resolution must be a positive finite number")

    # Spherical Mercator (EPSG:3857); asinh(tan(phi)) equals log(tan(pi/4 + phi/2)).
    xmin, xmax = (EARTH_RADIUS * math.radians(lon) for lon in (west, east))
    ymin, ymax = (max(-HALF_WORLD, min(HALF_WORLD,
                  EARTH_RADIUS * math.asinh(math.tan(math.radians(lat)))))
                  for lat in (south, north))
    width, height = (xmax - xmin) / resolution, (ymax - ymin) / resolution
    if not all(math.isfinite(v) and 0 < v <= 2147483647 for v in (width, height)):
        raise ValueError("Grid dimensions are invalid or too large; reduce bounds or increase resolution")
    cols, rows = math.ceil(width), math.ceil(height)
    # Normally extend east/south; near the world edge extend west/north instead.
    left = min(xmin, HALF_WORLD - cols * resolution)
    top = max(ymax, -HALF_WORLD + rows * resolution)
    if left < -HALF_WORLD or top > HALF_WORLD:
        raise ValueError("Whole pixels would exceed the Web Mercator world; adjust bounds or resolution")
    return {
        "crs": "EPSG:3857",
        "size": [cols, rows],
        "geotransform": [left, resolution, 0.0, top, 0.0, -resolution],
        "bounds": [left, top - rows * resolution, left + cols * resolution, top],
    }


def gdal_modules():
    try:
        from osgeo import gdal, osr
    except ImportError as exc:
        raise RuntimeError(
            "This Python environment needs GDAL Python bindings (osgeo). "
            "Run this script with a GDAL-enabled Python; bbox --dry-run needs only Python."
        ) from exc
    gdal.UseExceptions()
    return gdal, osr


def grid_from_reference(path):
    if not path.is_file():
        raise FileNotFoundError(f"Reference raster not found: {path}")
    gdal, osr = gdal_modules()
    ds = gdal.Open(str(path))
    gt = ds.GetGeoTransform()
    if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12 or gt[1] <= 0 or gt[5] >= 0:
        raise ValueError("Reference raster must be north-up with positive X and negative Y pixel size")
    actual, expected = osr.SpatialReference(), osr.SpatialReference()
    actual.ImportFromWkt(ds.GetProjection())
    expected.ImportFromEPSG(3857)
    actual.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    expected.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    if not actual.IsSame(expected):
        raise ValueError("Reference raster must use EPSG:3857; reproject it before downloading")
    cols, rows = ds.RasterXSize, ds.RasterYSize
    ds = None
    return {"crs": "EPSG:3857", "size": [cols, rows], "geotransform": list(gt),
            "bounds": [gt[0], gt[3] + rows * gt[5], gt[0] + cols * gt[1], gt[3]]}


def reference_vrt(grid):
    cols, rows = grid["size"]
    transform = ", ".join(format(v, ".17g") for v in grid["geotransform"])
    return (f'<VRTDataset rasterXSize="{cols}" rasterYSize="{rows}">\n'
            '  <SRS>EPSG:3857</SRS>\n'
            f'  <GeoTransform>{transform}</GeoTransform>\n'
            '  <VRTRasterBand dataType="Byte" band="1"/>\n'
            '</VRTDataset>\n')


def make_plan(args):
    grid = (grid_from_bbox(args.bbox, args.resolution) if args.bbox is not None
            else grid_from_reference(args.reference_raster))
    options = {key: getattr(args, key) for key in PROVIDER_OPTIONS[args.source]
               if getattr(args, key) is not None}
    identity = json.dumps({"grid": grid, "source": args.source, "options": options}, sort_keys=True)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    work = args.work_dir.resolve() / f"{args.source}_{digest}"
    reference = (work / "bbox_reference.vrt" if args.bbox is not None
                 else args.reference_raster.resolve())
    out = args.out.resolve()
    raw_vrt = None
    if args.source == "eox":
        raw_vrt = args.raw_vrt.resolve() if args.raw_vrt else work / "source_tiles.vrt"
    # Check paths before creating any working files or making network requests.
    outputs = [out, out.with_suffix(".tfw")]
    if raw_vrt is not None:
        outputs.append(raw_vrt)
    if len(set(outputs + [reference])) != len(outputs) + 1:
        raise ValueError("Output, world file, raw VRT and reference must use different paths")
    for path in outputs:
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    command = [sys.executable, str(Path(__file__).resolve().with_name(PROVIDER_SCRIPTS[args.source])),
               "--reference-raster", str(reference), "--out", str(out), "--work-dir", str(work)]
    if raw_vrt is not None:
        command.extend(["--raw-vrt", str(raw_vrt)])
    for key, value in options.items():
        command.extend(["--" + key.replace("_", "-"), str(value)])
    return {"input_mode": "bbox" if args.bbox is not None else "reference-raster",
            "source": args.source, "requested_bbox_wgs84": args.bbox, "grid": grid,
            "uncompressed_rgb_bytes": grid["size"][0] * grid["size"][1] * 3,
            "reference_raster": str(reference), "output": str(out),
            "work_dir": str(work), "raw_vrt": str(raw_vrt) if raw_vrt else None,
            "command": command}


def main(argv=None):
    args = parse_args(argv)
    try:
        plan = make_plan(args)
        if args.dry_run:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        gdal_modules()  # Fail before writing files if the child interpreter cannot run the downloader.
        if args.bbox is not None:
            reference = Path(plan["reference_raster"])
            content = reference_vrt(plan["grid"])
            reference.parent.mkdir(parents=True, exist_ok=True)
            if reference.exists():
                if reference.read_text(encoding="utf-8") != content:
                    raise ValueError(f"Existing generated reference differs: {reference}")
            else:
                with reference.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(content)
        print(json.dumps(plan, ensure_ascii=False), flush=True)
        return subprocess.run(plan["command"], check=False).returncode
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
