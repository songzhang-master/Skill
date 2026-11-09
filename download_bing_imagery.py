"""Download Bing aerial tiles and align them to an EPSG:3857 reference raster."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import time
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

from imagery_sources import BING_TEMPLATE, open_source

from osgeo import gdal, osr

WEB_MERCATOR_HALF_WORLD = 20037508.342789244
TILE_SIZE = 256


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-raster", required=True, help="North-up EPSG:3857 reference GeoTIFF")
    parser.add_argument("--out", required=True, help="New aligned RGB GeoTIFF; existing files are refused")
    parser.add_argument("--max-blocks", type=int, help="Optional limit on source blocks or tiles")
    parser.add_argument("--work-dir", required=True, help="Persistent cache directory for source JPEG tiles and VRT")
    parser.add_argument("--zoom", type=int, required=True, help="Bing tile zoom level (1-23)")
    parser.add_argument("--url-template", default=BING_TEMPLATE, choices=[BING_TEMPLATE],
                        help="Tile URL template with {q}, {x}, {y}, and {z} placeholders")
    return parser.parse_args()


def same_epsg_3857(projection: str) -> bool:
    actual, expected = osr.SpatialReference(), osr.SpatialReference()
    actual.ImportFromWkt(projection)
    expected.ImportFromEPSG(3857)
    if hasattr(actual, "SetAxisMappingStrategy"):
        actual.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        expected.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return bool(actual.IsSame(expected))


def inspect_reference(path: Path):
    ds = gdal.Open(str(path))
    if ds is None:
        raise RuntimeError(f"Cannot open reference raster: {path}")
    gt, projection = ds.GetGeoTransform(), ds.GetProjection()
    if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12 or gt[1] <= 0 or gt[5] >= 0:
        raise ValueError("Reference raster must be north-up with positive X and negative Y pixel size")
    if not same_epsg_3857(projection):
        raise ValueError("Reference raster must use EPSG:3857")
    bounds = (gt[0], gt[3] + ds.RasterYSize * gt[5],
              gt[0] + ds.RasterXSize * gt[1], gt[3])
    limit = WEB_MERCATOR_HALF_WORLD
    if bounds[0] < -limit or bounds[1] < -limit or bounds[2] > limit or bounds[3] > limit:
        raise ValueError("Reference bounds must lie inside the Web Mercator tile matrix")
    info = {
        "cols": ds.RasterXSize,
        "rows": ds.RasterYSize,
        "gt": gt,
        "projection": projection,
        "bounds": bounds,
    }
    ds = None
    return info


def quadkey(x: int, y: int, zoom: int) -> str:
    digits = []
    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)
        digit = (1 if x & mask else 0) + (2 if y & mask else 0)
        digits.append(str(digit))
    return "".join(digits)


def checked_tile(path: Path) -> bool:
    if not path.exists():
        return False
    ds = gdal.Open(str(path))
    valid = bool(ds and ds.RasterCount == 3 and
                 (ds.RasterXSize, ds.RasterYSize) == (TILE_SIZE, TILE_SIZE) and
                 all(ds.GetRasterBand(i).DataType == gdal.GDT_Byte for i in range(1, 4)))
    ds = None
    return valid


def download_tile(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "Codex-GIS/1.0",
                "Accept": "image/jpeg",
            })
            with open_source("bing", request, timeout=45) as response, partial.open("wb") as stream:
                if "no-tile" in response.headers.get("X-VE-Tile-Info", "").lower():
                    raise ValueError("Bing 标记此处没有影像瓦片。")
                shutil.copyfileobj(response, stream)
            if not checked_tile(partial):
                raise RuntimeError(f"Invalid 256x256 RGB tile: {url}")
            partial.replace(target)
            return
        except Exception:
            partial.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def write_vrt(path: Path, tiles: list[tuple[Path, int, int]], cols: int, rows: int,
              gt: tuple[float, ...], projection: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(f'<VRTDataset rasterXSize="{cols}" rasterYSize="{rows}">\n')
        stream.write(f"  <SRS>{escape(projection)}</SRS>\n")
        stream.write("  <GeoTransform>" + ", ".join(f"{v:.15g}" for v in gt) + "</GeoTransform>\n")
        for band in range(1, 4):
            stream.write(f'  <VRTRasterBand dataType="Byte" band="{band}">\n')
            color = ("Red", "Green", "Blue")[band - 1]
            stream.write(f"    <ColorInterp>{color}</ColorInterp>\n")
            for tile_path, xoff, yoff in tiles:
                relative = Path(os.path.relpath(tile_path, path.parent)).as_posix()
                stream.write("    <SimpleSource>\n")
                stream.write(f'      <SourceFilename relativeToVRT="1">{escape(relative)}</SourceFilename>\n')
                stream.write(f"      <SourceBand>{band}</SourceBand>\n")
                stream.write(f'      <SrcRect xOff="0" yOff="0" xSize="{TILE_SIZE}" ySize="{TILE_SIZE}"/>\n')
                stream.write(f'      <DstRect xOff="{xoff}" yOff="{yoff}" xSize="{TILE_SIZE}" ySize="{TILE_SIZE}"/>\n')
                stream.write("    </SimpleSource>\n")
            stream.write("  </VRTRasterBand>\n")
        stream.write("</VRTDataset>\n")
    temp.replace(path)


def main() -> None:
    args = parse_args()
    if not 1 <= args.zoom <= 23:
        raise ValueError("zoom must be between 1 and 23")
    reference_path, out, work = map(Path, (args.reference_raster, args.out, args.work_dir))
    if out.exists() or out.with_suffix(".tfw").exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {out}")
    if "{q}" not in args.url_template and not all(token in args.url_template for token in ("{x}", "{y}", "{z}")):
        raise ValueError("URL template must include {q} or all of {x}, {y}, and {z}")

    reference = inspect_reference(reference_path)
    xmin, ymin, xmax, ymax = reference["bounds"]
    matrix_width = 1 << args.zoom
    tile_span = 2 * WEB_MERCATOR_HALF_WORLD / matrix_width
    pixel_span = tile_span / TILE_SIZE
    epsilon = tile_span * 1e-12
    col0 = math.floor((xmin + WEB_MERCATOR_HALF_WORLD) / tile_span)
    col1 = math.ceil((xmax + WEB_MERCATOR_HALF_WORLD) / tile_span - epsilon) - 1
    row0 = math.floor((WEB_MERCATOR_HALF_WORLD - ymax) / tile_span)
    row1 = math.ceil((WEB_MERCATOR_HALF_WORLD - ymin) / tile_span - epsilon) - 1
    col0, col1 = max(0, col0), min(matrix_width - 1, col1)
    row0, row1 = max(0, row0), min(matrix_width - 1, row1)
    if col0 > col1 or row0 > row1:
        raise ValueError("Reference raster does not intersect the selected Bing tile matrix")

    tile_cols, tile_rows = col1 - col0 + 1, row1 - row0 + 1
    raw_cols, raw_rows = tile_cols * TILE_SIZE, tile_rows * TILE_SIZE
    raw_gt = (-WEB_MERCATOR_HALF_WORLD + col0 * tile_span, pixel_span, 0.0,
              WEB_MERCATOR_HALF_WORLD - row0 * tile_span, 0.0, -pixel_span)
    tile_root = work / "tiles" / "bing" / str(args.zoom)
    tiles: list[tuple[Path, int, int]] = []
    total = tile_cols * tile_rows
    if args.max_blocks is not None and total > args.max_blocks:
        raise ValueError(f"BLOCK_LIMIT：需要 {total} 块瓦片，上限为 {args.max_blocks}。")
    done = 0
    for row in range(row0, row1 + 1):
        for col in range(col0, col1 + 1):
            tile = tile_root / f"{row}_{col}.jpeg"
            if not checked_tile(tile):
                url = args.url_template.format(q=quadkey(col, row, args.zoom), x=col, y=row, z=args.zoom)
                print(f"Download tile {done + 1}/{total}: z={args.zoom}, x={col}, y={row}", flush=True)
                download_tile(url, tile)
                time.sleep(0.2)
            done += 1
            tiles.append((tile, (col - col0) * TILE_SIZE, (row - row0) * TILE_SIZE))

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    vrt = work / f"bing_z{args.zoom}.vrt"
    write_vrt(vrt, tiles, raw_cols, raw_rows, raw_gt, srs.ExportToWkt())
    raw_ds = gdal.Open(str(vrt))
    if raw_ds is None or raw_ds.RasterCount != 3:
        raise RuntimeError("Bing tile mosaic VRT validation failed")
    raw_ds = None

    out.parent.mkdir(parents=True, exist_ok=True)
    options = gdal.WarpOptions(
        format="GTiff",
        outputBounds=reference["bounds"],
        width=reference["cols"],
        height=reference["rows"],
        dstSRS=reference["projection"],
        resampleAlg="bilinear",
        outputType=gdal.GDT_Byte,
        creationOptions=[
            "TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER",
            "INTERLEAVE=PIXEL", "PHOTOMETRIC=RGB", "TFW=YES",
        ],
    )
    result = gdal.Warp(str(out), str(vrt), options=options)
    if result is None:
        raise RuntimeError("GDAL warp from Bing tile mosaic failed")
    result.FlushCache()
    result = None
    result = gdal.Open(str(out))
    if result is None:
        raise RuntimeError("Cannot reopen aligned Bing output")
    actual_gt = result.GetGeoTransform()
    gt_matches = all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9)
                     for a, b in zip(actual_gt, reference["gt"]))
    report = {
        "provider": "bing",
        "zoom": args.zoom,
        "tile_count": total,
        "tile_cache": str(tile_root),
        "raw_vrt": str(vrt),
        "output": str(out),
        "size": [result.RasterXSize, result.RasterYSize],
        "bands": result.RasterCount,
        "dtype": gdal.GetDataTypeName(result.GetRasterBand(1).DataType),
        "geotransform_matches_reference": gt_matches,
        "projection_matches_epsg_3857": same_epsg_3857(result.GetProjection()),
        "tfw_exists": out.with_suffix(".tfw").exists(),
        "overviews": result.GetRasterBand(1).GetOverviewCount(),
        "compression": result.GetMetadata("IMAGE_STRUCTURE").get("COMPRESSION"),
    }
    result = None
    print(json.dumps(report, ensure_ascii=False))
    if not all([
        report["size"] == [reference["cols"], reference["rows"]],
        report["bands"] == 3,
        report["dtype"] == "Byte",
        gt_matches,
        report["projection_matches_epsg_3857"],
        report["tfw_exists"],
        report["overviews"] == 0,
    ]):
        raise RuntimeError("Aligned Bing imagery output validation failed")


if __name__ == "__main__":
    main()
