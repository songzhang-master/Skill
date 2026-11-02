"""Download Esri World Imagery aligned exactly to an EPSG:3857 reference raster."""
from __future__ import annotations

import argparse
import json
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path

from osgeo import gdal

gdal.UseExceptions()

DEFAULT_SERVICE = "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-raster", required=True, help="North-up EPSG:3857 reference GeoTIFF")
    parser.add_argument("--out", required=True, help="New output RGB GeoTIFF; existing files are refused")
    parser.add_argument("--work-dir", required=True, help="Temporary JPG/VRT directory, outside final output directory")
    parser.add_argument("--service-url", default=DEFAULT_SERVICE, help="ArcGIS MapServer export endpoint")
    parser.add_argument("--block-size", type=int, default=2048, help="Export block side in pixels; default is 2048")
    return parser.parse_args()


def valid_image(path: Path, width: int, height: int) -> bool:
    if not path.is_file():
        return False
    try:
        ds = gdal.Open(str(path))
        valid = bool(ds and ds.RasterCount == 3 and
                     (ds.RasterXSize, ds.RasterYSize) == (width, height) and
                     all(ds.GetRasterBand(i).DataType == gdal.GDT_Byte for i in range(1, 4)))
        ds = None
        return valid
    except RuntimeError:
        return False


def download(url: str, target: Path, width: int, height: int) -> None:
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Codex-GIS/1.0"})
            with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            if valid_image(target, width, height):
                return
            raise RuntimeError("Service response is not an RGB image of the requested size")
        except Exception:
            target.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))


def validate_reference(ds: gdal.Dataset) -> tuple[tuple[float, ...], str]:
    gt, projection = ds.GetGeoTransform(), ds.GetProjection()
    if abs(gt[2]) > 1e-9 or abs(gt[4]) > 1e-9 or gt[1] <= 0 or gt[5] >= 0:
        raise ValueError("Reference raster must be north-up with positive X and negative Y pixel size")
    if "Pseudo-Mercator" not in projection and "3857" not in projection:
        raise ValueError("Reference raster must use EPSG:3857")
    return gt, projection


def write_vrt(vrt: Path, sources: list[tuple[Path, int, int, int, int]], cols: int, rows: int, gt, projection: str) -> None:
    srs = projection.replace("&", "&amp;")
    with vrt.open("w", encoding="utf-8") as stream:
        stream.write(f'<VRTDataset rasterXSize="{cols}" rasterYSize="{rows}">\n')
        stream.write(f'  <SRS>{srs}</SRS>\n')
        stream.write(f'  <GeoTransform>{gt[0]}, {gt[1]}, 0, {gt[3]}, 0, {gt[5]}</GeoTransform>\n')
        for band in range(1, 4):
            stream.write(f'  <VRTRasterBand dataType="Byte" band="{band}">\n')
            for tile, xoff, yoff, width, height in sources:
                name = str(tile).replace("&", "&amp;")
                stream.write("    <SimpleSource>\n")
                stream.write(f'      <SourceFilename relativeToVRT="0">{name}</SourceFilename>\n')
                stream.write(f"      <SourceBand>{band}</SourceBand>\n")
                stream.write(f'      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>\n')
                stream.write(f'      <DstRect xOff="{xoff}" yOff="{yoff}" xSize="{width}" ySize="{height}"/>\n')
                stream.write("    </SimpleSource>\n")
            stream.write("  </VRTRasterBand>\n")
        stream.write("</VRTDataset>\n")


def main() -> None:
    args = parse_args()
    reference, out, work = Path(args.reference_raster), Path(args.out), Path(args.work_dir)
    if out.exists() or out.with_suffix(".tfw").exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {out}")
    if args.block_size < 256 or args.block_size > 4096:
        raise ValueError("block-size must be between 256 and 4096")
    source = gdal.Open(str(reference))
    if not source:
        raise RuntimeError(f"Cannot open reference raster: {reference}")
    gt, projection = validate_reference(source)
    cols, rows = source.RasterXSize, source.RasterYSize
    work.mkdir(parents=True, exist_ok=True)
    tiles: list[tuple[Path, int, int, int, int]] = []
    for yoff in range(0, rows, args.block_size):
        height = min(args.block_size, rows - yoff)
        for xoff in range(0, cols, args.block_size):
            width = min(args.block_size, cols - xoff)
            xmin, xmax = gt[0] + xoff * gt[1], gt[0] + (xoff + width) * gt[1]
            ymax, ymin = gt[3] + yoff * gt[5], gt[3] + (yoff + height) * gt[5]
            tile = work / f"tile_{width}x{height}_x{xoff}_y{yoff}.jpg"
            if not valid_image(tile, width, height):
                query = urllib.parse.urlencode({
                    "bbox": f"{xmin:.8f},{ymin:.8f},{xmax:.8f},{ymax:.8f}",
                    "bboxSR": "3857", "imageSR": "3857", "size": f"{width},{height}",
                    "format": "jpg", "transparent": "false", "f": "image",
                })
                print(f"Download x={xoff}, y={yoff}, {width}x{height}", flush=True)
                download(f"{args.service_url}?{query}", tile, width, height)
            check = gdal.Open(str(tile))
            if not check or check.RasterCount != 3 or (check.RasterXSize, check.RasterYSize) != (width, height):
                raise RuntimeError(f"Unexpected service image: {tile}")
            tiles.append((tile, xoff, yoff, width, height))
    vrt = work / "mosaic.vrt"
    write_vrt(vrt, tiles, cols, rows, gt, projection)
    out.parent.mkdir(parents=True, exist_ok=True)
    gdal.Translate(str(out), str(vrt), format="GTiff", creationOptions=[
        "TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER", "INTERLEAVE=PIXEL", "TFW=YES"
    ])
    result = gdal.Open(str(out))
    report = {
        "output": str(out), "size": [result.RasterXSize, result.RasterYSize], "bands": result.RasterCount,
        "dtype": gdal.GetDataTypeName(result.GetRasterBand(1).DataType),
        "geotransform_matches_reference": result.GetGeoTransform() == gt,
        "projection_matches_reference": result.GetProjection() == projection,
        "compression": result.GetMetadata("IMAGE_STRUCTURE").get("COMPRESSION"),
        "tfw_exists": out.with_suffix(".tfw").exists(),
    }
    print(json.dumps(report, ensure_ascii=False))
    if not all([report["geotransform_matches_reference"], report["projection_matches_reference"], report["tfw_exists"]]):
        raise RuntimeError("Output validation failed")


if __name__ == "__main__":
    main()
