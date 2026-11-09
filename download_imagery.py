"""Download imagery using either a reference raster or a WGS84 bounding box."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

from imagery_sources import BING_TEMPLATE, ESRI_EXPORT, EOX_CAPABILITIES, EOX_DEFAULT_LAYER, cloudless_layer
from imagery_request import date_mode, request_arguments
from imagery_result import download_and_verify
from imagery_dates import candidate_days, coverage_date

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
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    argv = list(sys.argv[1:] if argv is None else argv)
    from_form = any(a == "--request" or a.startswith("--request=") for a in argv)
    if from_form:
        form = argparse.ArgumentParser(description="运行固定参数表单", allow_abbrev=False)
        form.add_argument("--request", required=True)
        form.add_argument("--dry-run", action="store_true")
        task = form.parse_args(argv)
        try:
            argv = request_arguments(task.request)
        except (OSError, ValueError, OverflowError) as exc:
            form.error(str(exc))
        if task.dry_run:
            argv.append("--dry-run")
    parser.epilog = "固定表单：python download_imagery.py --request 参数.json [--dry-run]"
    parser.add_argument("--source", choices=PROVIDER_SCRIPTS)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--reference-raster", type=Path,
                        help="Existing north-up EPSG:3857 DEM or other reference raster")
    inputs.add_argument("--bbox", nargs=4, type=float,
                        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                        help="WGS84 longitude/latitude bounds in decimal degrees")
    parser.add_argument("--resolution", type=float,
                        help="Required with --bbox: EPSG:3857 map metres per output pixel")
    destinations = parser.add_mutually_exclusive_group(required=True)
    destinations.add_argument("--out", type=Path, help="New output RGB GeoTIFF")
    destinations.add_argument("--output-dir", type=Path, help="Directory for automatically named output")
    parser.add_argument("--max-blocks", type=int, help="Optional limit on source blocks or tiles")
    parser.add_argument("--work-dir", type=Path, help="Persistent workspace/cache")
    parser.add_argument("--date", default="不限", help="成像时间要求：不限、YYYY 或 YYYY-MM-DD")
    parser.add_argument("--max-pixels", type=int, help="Optional maximum output pixel count")
    parser.add_argument("--raw-vrt", type=Path, help="EOX only: optional new raw-tile VRT path")
    parser.add_argument("--zoom", type=int, help="EOX: defaults to 13; Bing: required (1-23)")
    parser.add_argument("--layer", type=cloudless_layer, help="EOX cloudless layer")
    parser.add_argument("--tile-matrix-set", choices=["GoogleMapsCompatible", "g"])
    parser.add_argument("--block-size", type=int, help="Esri export block side; defaults to 2048")
    parser.add_argument("--service-url", choices=[ESRI_EXPORT])
    parser.add_argument("--capabilities-url", choices=[EOX_CAPABILITIES])
    parser.add_argument("--url-template", choices=[BING_TEMPLATE])
    parser.add_argument("--dry-run", action="store_true",
                        help="Print grid and command without downloading or writing files")
    args = parser.parse_args(argv)
    try:
        mode = date_mode(args.date)
    except ValueError as exc:
        parser.error(str(exc))
    if mode == "daily":
        if args.source not in (None, "esri"):
            parser.error("DATE_UNSUPPORTED：该接口无法核验日级影像，请用标准表单自动匹配。")
        args.source = "esri"
    if mode == "annual":
        if args.source is None:
            args.source = "eox"
        if args.source != "eox":
            parser.error("DATE_UNSUPPORTED：只有 eox 支持选择年度合成。")
        layer = f"s2cloudless-{args.date}_3857"
        if args.layer is not None and args.layer != layer:
            parser.error("日期年份与指定的 EOX 图层冲突。")
        args.layer = layer
    if args.work_dir is None:
        if args.output_dir is None:
            parser.error("--out requires --work-dir")
        dest = args.output_dir.resolve()
        args.work_dir = dest.parent / ".imagery-cache" / dest.name
    if args.source is None and args.output_dir is None:
        parser.error("多来源任务需要 --output-dir。")
    if args.max_blocks is not None and args.max_blocks < 1:
        parser.error("--max-blocks must be positive")
    if args.max_pixels is not None and args.max_pixels < 1:
        parser.error("--max-pixels must be positive")
    if args.bbox is not None and args.resolution is None:
        parser.error("--bbox requires --resolution (map metres per pixel)")
    if args.reference_raster is not None and args.resolution is not None:
        parser.error("--resolution is only used with --bbox; reference mode preserves its grid")
    for option in {item for items in PROVIDER_OPTIONS.values() for item in items}:
        if getattr(args, option) is not None and option not in PROVIDER_OPTIONS.get(args.source, ()):
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


def request_grid(args):
    return (grid_from_bbox(args.bbox, args.resolution) if args.bbox is not None
            else grid_from_reference(args.reference_raster))


def make_plan(args, grid=None, evidence=None):
    if grid is None:
        grid = request_grid(args)
    options = {key: getattr(args, key) for key in PROVIDER_OPTIONS[args.source]
               if getattr(args, key) is not None}
    pixels = grid["size"][0] * grid["size"][1]
    if args.max_pixels is not None and pixels > args.max_pixels:
        raise ValueError(f"PIXEL_LIMIT：任务需要 {pixels} 个像元，设定上限为 {args.max_pixels}。")
    layer = (args.layer or EOX_DEFAULT_LAYER) if args.source == "eox" else None
    if args.source == "esri":
        effective = {"block_size": args.block_size or 2048}
        count = math.prod(math.ceil(n / effective["block_size"]) for n in grid["size"])
    else:
        zoom = args.zoom if args.zoom is not None else 13
        effective = {"zoom": zoom}
        if layer:
            effective.update(layer=layer, tile_matrix_set=args.tile_matrix_set or "GoogleMapsCompatible")
        count = None
        if 0 <= zoom <= 30:
            span = 2 * HALF_WORLD / (1 << zoom)
            left, bottom, right, top = grid["bounds"]
            def tiles(start, stop):
                first = max(0, math.floor(start / span))
                end = min(1 << zoom, math.ceil(stop / span - 1e-12))
                return max(0, end - first)
            count = tiles(left + HALF_WORLD, right + HALF_WORLD) * tiles(HALF_WORLD - top, HALF_WORLD - bottom)
    if args.max_blocks is not None and count is not None and count > args.max_blocks:
        raise ValueError(f"BLOCK_LIMIT：预计需要 {count} 个源分块或瓦片，设定上限为 {args.max_blocks}。")
    daily = date_mode(args.date) == "daily"
    task_key = {"grid": grid, "source": args.source, "options": effective}
    if daily:
        task_key["target_date"] = args.date
    identity = json.dumps(task_key, sort_keys=True)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    work = args.work_dir.resolve() / f"{args.source}_{digest}"
    if evidence is not None:
        work = work / evidence["footprint_sha256"]
    reference = (work / "bbox_reference.vrt" if args.bbox is not None
                 else args.reference_raster.resolve())
    product = layer or args.source + "_basemap"
    if daily:
        product += "_requested_" + args.date
    out = (args.out or args.output_dir / f"{product}_{digest}.tif").resolve()
    raw_vrt = None
    if args.source == "eox":
        raw_vrt = args.raw_vrt.resolve() if args.raw_vrt else work / "source_tiles.vrt"
    # Check paths before creating any working files or making network requests.
    outputs = [out, out.with_suffix(".tfw"), out.with_suffix(".json")]
    if raw_vrt is not None:
        outputs.append(raw_vrt)
    for parent in {work, *(p.parent for p in outputs)}:
        if any(p.exists() and not p.is_dir() for p in (parent, *parent.parents)):
            raise ValueError(f"输出或缓存目录被文件占用：{parent}")
    if len(set(outputs + [reference])) != len(outputs) + 1:
        raise ValueError("Output, world file, raw VRT and reference must use different paths")
    for path in outputs:
        if path.exists():
            raise FileExistsError(f"OUTPUT_EXISTS：已有文件，已停止写入：{path}")
    command = [sys.executable, str(Path(__file__).resolve().with_name(PROVIDER_SCRIPTS[args.source])),
               "--reference-raster", str(reference), "--out", str(out), "--work-dir", str(work)]
    if raw_vrt is not None:
        command.extend(["--raw-vrt", str(raw_vrt)])
    if args.max_blocks is not None:
        command.extend(["--max-blocks", str(args.max_blocks)])
    for key, value in options.items():
        command.extend(["--" + key.replace("_", "-"), str(value)])
    return {"input_mode": "bbox" if args.bbox is not None else "reference-raster",
            "source": args.source, "requested_bbox_wgs84": args.bbox, "grid": grid,
            "requested_date": args.date, "layer": layer, "pixel_count": pixels,
            "source_endpoint": {"bing": BING_TEMPLATE, "eox": EOX_CAPABILITIES, "esri": ESRI_EXPORT}[args.source],
            "source_options": effective, "estimated_source_blocks_or_tiles": count,
            "max_pixels": args.max_pixels, "max_blocks": args.max_blocks, "receipt": str(out.with_suffix(".json")),
            "uncompressed_rgb_bytes": grid["size"][0] * grid["size"][1] * 3,
            "reference_raster": str(reference), "output": str(out),
            "work_dir": str(work), "raw_vrt": str(raw_vrt) if raw_vrt else None,
            "command": command}


def run_all_sources(args):
    grid = request_grid(args)
    results = []
    plans = []
    for source in PROVIDER_SCRIPTS:
        provider = argparse.Namespace(**vars(args))
        provider.source = source
        if source == "bing":
            gt = grid["geotransform"]
            pixel = min(gt[1], -gt[5])
            level = math.ceil(math.log2(2 * HALF_WORLD / 256) - math.log2(pixel))
            provider.zoom = max(1, min(23, level))
        result = {"source": source}
        results.append(result)
        try:
            plan = make_plan(provider, grid)
        except (OSError, ValueError, RuntimeError) as exc:
            result.update(status="OUTPUT_EXISTS" if isinstance(exc, FileExistsError) else "PREFLIGHT_FAILED",
                          reason=str(exc))
            continue
        plans.append((plan, result))
        result.update(status="READY", output=plan["output"], receipt=plan["receipt"])
        if args.dry_run:
            result["plan"] = plan
    if args.dry_run:
        print(json.dumps({"status": "PREVIEW", "results": results}, ensure_ascii=False, indent=2))
        return 0 if plans else 1
    if plans:
        gdal_modules()
    for plan, result in plans:
        try:
            vrt = reference_vrt(grid) if args.bbox is not None else None
            code = download_and_verify(plan, vrt)
            result.update(status="SUCCESS" if code == 0 else "DOWNLOAD_FAILED", exit_code=code)
        except (OSError, ValueError, RuntimeError) as exc:
            result.update(status="DOWNLOAD_FAILED", reason=str(exc))
    successful = sum(r["status"] == "SUCCESS" for r in results)
    status = "SUCCESS" if successful == len(results) else ("PARTIAL" if successful else "FAILED")
    print(json.dumps({"status": status, "results": results}, ensure_ascii=False))
    return 0 if status == "SUCCESS" else 1


def run_dated(args):
    grid = request_grid(args)
    gt = grid["geotransform"]
    if not math.isclose(gt[1], -gt[5], rel_tol=1e-10):
        raise ValueError("日期核验需要正方形输出像元，以便确定导出比例尺。")
    days = candidate_days(args.date)
    plan = make_plan(args, grid)
    if args.dry_run:
        print(json.dumps(dict(plan, status="PREVIEW", candidate_dates=days), ensure_ascii=False, indent=2))
        return 0
    gdal_modules()
    evidence = coverage_date(grid)
    checked = []
    failed = False
    for offset, day in enumerate(days):
        checked.append(day)
        if day != evidence["acquisition_date"]:
            continue
        plan = make_plan(args, grid, evidence)
        plan.update(acquisition_date=day, lookback_days=offset, checked_dates=list(checked),
                    date_evidence=evidence)
        vrt = reference_vrt(grid) if args.bbox is not None else None
        if download_and_verify(plan, vrt) == 0:
            return 0
        failed = True
    result = {"status": "DOWNLOAD_FAILED" if failed else "NO_VERIFIABLE_IMAGE",
              "requested_date": args.date, "checked_dates": checked, "date_evidence": evidence}
    print(json.dumps(result, ensure_ascii=False))
    return 1


def main(argv=None):
    args = parse_args(argv)
    try:
        if date_mode(args.date) == "daily":
            return run_dated(args)
        if args.source is None:
            return run_all_sources(args)
        plan = make_plan(args)
        if args.dry_run:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        gdal_modules()  # Fail before writing files if the child interpreter cannot run the downloader.
        print(json.dumps(plan, ensure_ascii=False), flush=True)
        vrt = reference_vrt(plan["grid"]) if args.bbox is not None else None
        return download_and_verify(plan, vrt)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
