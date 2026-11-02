"""Download EOX Sentinel-2 cloudless WMTS tiles and align them to a reference GeoTIFF."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

from osgeo import gdal, osr

DEFAULT_CAPABILITIES = "https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml"
DEFAULT_LAYER = "s2cloudless-2025_3857"
DEFAULT_MATRIX_SET = "GoogleMapsCompatible"
WEB_MERCATOR_HALF_WORLD = 20037508.342789244


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-raster", required=True, help="North-up EPSG:3857 reference GeoTIFF")
    parser.add_argument("--raw-vrt", required=True, help="New VRT on the selected native WMTS tile grid")
    parser.add_argument("--out", required=True, help="New reference-grid RGB GeoTIFF; existing files are refused")
    parser.add_argument("--work-dir", required=True, help="Persistent cache directory for original JPEG WMTS tiles")
    parser.add_argument("--capabilities-url", default=DEFAULT_CAPABILITIES, help="WMTS GetCapabilities URL")
    parser.add_argument("--layer", default=DEFAULT_LAYER, help="Capabilities layer identifier")
    parser.add_argument("--tile-matrix-set", default=DEFAULT_MATRIX_SET, help="Linked Web Mercator tile matrix set")
    parser.add_argument("--zoom", type=int, default=13, help="TileMatrix identifier; default is 13")
    return parser.parse_args()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if local_name(child.tag) == name]


def child_text(element: ET.Element, name: str, required: bool = True) -> str | None:
    found = next((child for child in children(element, name)), None)
    if found is None or not (found.text or "").strip():
        if required:
            raise ValueError(f"Capabilities element is missing {name}")
        return None
    return found.text.strip()


def fetch_capabilities(url: str) -> ET.Element:
    request = urllib.request.Request(url, headers={"User-Agent": "Codex-GIS/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return ET.fromstring(response.read())


def get_layer_and_matrix(root: ET.Element, layer_id: str, matrix_set_id: str, zoom: int):
    contents = next((node for node in root.iter() if local_name(node.tag) == "Contents"), None)
    if contents is None:
        raise ValueError("WMTS capabilities has no Contents element")

    layers = [node for node in children(contents, "Layer") if child_text(node, "Identifier") == layer_id]
    if not layers:
        raise ValueError(f"WMTS layer not found: {layer_id}")
    layer = layers[0]
    formats = [node.text.strip() for node in children(layer, "Format") if node.text]
    if "image/jpeg" not in formats:
        raise ValueError(f"Layer {layer_id} does not advertise image/jpeg: {formats}")

    linked_sets = [
        child_text(link, "TileMatrixSet")
        for link in children(layer, "TileMatrixSetLink")
    ]
    if matrix_set_id not in linked_sets:
        raise ValueError(f"Layer {layer_id} is not linked to matrix set {matrix_set_id}: {linked_sets}")

    styles = children(layer, "Style")
    style = next((node for node in styles if node.attrib.get("isDefault", "").lower() == "true"), None)
    if style is None and styles:
        style = styles[0]
    style_id = child_text(style, "Identifier") if style is not None else "default"

    resources = [
        node for node in children(layer, "ResourceURL")
        if node.attrib.get("resourceType") == "tile" and node.attrib.get("format") == "image/jpeg"
    ]
    if not resources or not resources[0].attrib.get("template"):
        raise ValueError(f"No REST tile URL template advertised for {layer_id}")
    template = resources[0].attrib["template"]
    parsed = urllib.parse.urlsplit(template)
    if parsed.scheme == "http" and parsed.hostname == "tiles.maps.eox.at":
        template = urllib.parse.urlunsplit(parsed._replace(scheme="https"))

    matrix_sets = [node for node in children(contents, "TileMatrixSet") if child_text(node, "Identifier") == matrix_set_id]
    if not matrix_sets:
        raise ValueError(f"Tile matrix set not found: {matrix_set_id}")
    matrix_set = matrix_sets[0]
    supported_crs = child_text(matrix_set, "SupportedCRS")
    normalized_crs = supported_crs.lower()
    if "3857" not in normalized_crs and "900913" not in normalized_crs:
        raise ValueError(f"Matrix set must be Web Mercator, got {supported_crs}")

    matrices = [node for node in children(matrix_set, "TileMatrix") if child_text(node, "Identifier") == str(zoom)]
    if not matrices:
        raise ValueError(f"Zoom/TileMatrix {zoom} is not available in {matrix_set_id}")
    matrix = matrices[0]
    matrix_info = {
        "top_left": [float(v) for v in child_text(matrix, "TopLeftCorner").split()],
        "scale": float(child_text(matrix, "ScaleDenominator")),
        "matrix_width": int(child_text(matrix, "MatrixWidth")),
        "matrix_height": int(child_text(matrix, "MatrixHeight")),
        "tile_width": int(child_text(matrix, "TileWidth")),
        "tile_height": int(child_text(matrix, "TileHeight")),
    }
    if len(matrix_info["top_left"]) != 2:
        raise ValueError("TileMatrix TopLeftCorner must contain two coordinates")
    matrix_info["pixel_span"] = matrix_info["scale"] * 0.00028
    return layer, style_id, template, supported_crs, matrix_info


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
    info = {
        "cols": ds.RasterXSize,
        "rows": ds.RasterYSize,
        "gt": gt,
        "projection": projection,
        "bounds": (gt[0], gt[3] + ds.RasterYSize * gt[5],
                   gt[0] + ds.RasterXSize * gt[1], gt[3]),
    }
    ds = None
    return info


def checked_tile(path: Path, width: int, height: int) -> bool:
    if not path.exists():
        return False
    ds = gdal.Open(str(path))
    valid = bool(ds and ds.RasterCount == 3 and
                 (ds.RasterXSize, ds.RasterYSize) == (width, height) and
                 all(ds.GetRasterBand(i).DataType == gdal.GDT_Byte for i in range(1, 4)))
    ds = None
    return valid


def download_tile(url: str, target: Path, width: int, height: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "Codex-GIS/1.0",
                "Accept": "image/jpeg",
            })
            with urllib.request.urlopen(request, timeout=90) as response, partial.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            if not checked_tile(partial, width, height):
                raise RuntimeError(f"Invalid JPEG WMTS tile: {url}")
            partial.replace(target)
            return
        except Exception:
            partial.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def write_vrt(path: Path, tiles: list[tuple[Path, int, int]], cols: int, rows: int,
              tile_width: int, tile_height: int, gt: tuple[float, ...], projection: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(f'<VRTDataset rasterXSize="{cols}" rasterYSize="{rows}">\n')
        stream.write(f"  <SRS>{escape(projection)}</SRS>\n")
        stream.write("  <GeoTransform>" + ", ".join(f"{v:.15g}" for v in gt) + "</GeoTransform>\n")
        for band in range(1, 4):
            stream.write(f'  <VRTRasterBand dataType="Byte" band="{band}">\n')
            for tile_path, xoff, yoff in tiles:
                relative = Path(os.path.relpath(tile_path, path.parent)).as_posix()
                stream.write("    <SimpleSource>\n")
                stream.write(f'      <SourceFilename relativeToVRT="1">{escape(relative)}</SourceFilename>\n')
                stream.write(f"      <SourceBand>{band}</SourceBand>\n")
                stream.write(f'      <SrcRect xOff="0" yOff="0" xSize="{tile_width}" ySize="{tile_height}"/>\n')
                stream.write(f'      <DstRect xOff="{xoff}" yOff="{yoff}" xSize="{tile_width}" ySize="{tile_height}"/>\n')
                stream.write("    </SimpleSource>\n")
            stream.write("  </VRTRasterBand>\n")
        stream.write("</VRTDataset>\n")
    temp.replace(path)


def main() -> None:
    args = parse_args()
    reference_path, raw_vrt, out, work = map(Path, (
        args.reference_raster, args.raw_vrt, args.out, args.work_dir
    ))
    if raw_vrt.exists():
        raise FileExistsError(f"Refusing to overwrite existing raw VRT: {raw_vrt}")
    if out.exists() or out.with_suffix(".tfw").exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {out}")
    if raw_vrt.resolve() == out.resolve():
        raise ValueError("--raw-vrt and --out must be different paths")

    reference = inspect_reference(reference_path)
    root = fetch_capabilities(args.capabilities_url)
    layer, style_id, template, source_crs, matrix = get_layer_and_matrix(
        root, args.layer, args.tile_matrix_set, args.zoom
    )

    origin_x, origin_y = matrix["top_left"]
    span_x = matrix["pixel_span"] * matrix["tile_width"]
    span_y = matrix["pixel_span"] * matrix["tile_height"]
    if span_x * matrix["matrix_width"] < 2 * WEB_MERCATOR_HALF_WORLD * 0.999:
        raise ValueError("TileMatrix horizontal extent is smaller than the Web Mercator world")

    xmin, ymin, xmax, ymax = reference["bounds"]
    epsilon = 1e-12
    col0 = math.floor((xmin - origin_x) / span_x)
    col1 = math.ceil((xmax - origin_x) / span_x - epsilon) - 1
    row0 = math.floor((origin_y - ymax) / span_y)
    row1 = math.ceil((origin_y - ymin) / span_y - epsilon) - 1
    col0, col1 = max(0, col0), min(matrix["matrix_width"] - 1, col1)
    row0, row1 = max(0, row0), min(matrix["matrix_height"] - 1, row1)
    if col0 > col1 or row0 > row1:
        raise ValueError("Reference raster does not intersect the selected WMTS matrix")

    tile_width, tile_height = matrix["tile_width"], matrix["tile_height"]
    tile_cols, tile_rows = col1 - col0 + 1, row1 - row0 + 1
    raw_cols, raw_rows = tile_cols * tile_width, tile_rows * tile_height
    raw_gt = (origin_x + col0 * span_x, matrix["pixel_span"], 0.0,
              origin_y - row0 * span_y, 0.0, -matrix["pixel_span"])

    tiles: list[tuple[Path, int, int]] = []
    tile_root = work / "tiles" / args.layer / args.tile_matrix_set / str(args.zoom)
    total = tile_cols * tile_rows
    done = 0
    for row in range(row0, row1 + 1):
        for col in range(col0, col1 + 1):
            tile = tile_root / f"{row}_{col}.jpg"
            if not checked_tile(tile, tile_width, tile_height):
                url = template.format(
                    Layer=args.layer,
                    Style=style_id,
                    TileMatrixSet=args.tile_matrix_set,
                    TileMatrix=str(args.zoom),
                    TileRow=str(row),
                    TileCol=str(col),
                )
                print(f"Download tile {done + 1}/{total}: row={row}, col={col}", flush=True)
                download_tile(url, tile, tile_width, tile_height)
            done += 1
            tiles.append((tile, (col - col0) * tile_width, (row - row0) * tile_height))

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    write_vrt(raw_vrt, tiles, raw_cols, raw_rows, tile_width, tile_height,
              raw_gt, srs.ExportToWkt())
    raw_ds = gdal.Open(str(raw_vrt))
    if raw_ds is None or raw_ds.RasterCount != 3:
        raise RuntimeError("Raw WMTS VRT validation failed")
    raw_ds = None

    out.parent.mkdir(parents=True, exist_ok=True)
    warp_options = gdal.WarpOptions(
        format="GTiff",
        outputBounds=reference["bounds"],
        width=reference["cols"],
        height=reference["rows"],
        dstSRS=reference["projection"],
        resampleAlg="bilinear",
        outputType=gdal.GDT_Byte,
        creationOptions=[
            "TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER",
            "INTERLEAVE=PIXEL", "TFW=YES",
        ],
    )
    result = gdal.Warp(str(out), str(raw_vrt), options=warp_options)
    if result is None:
        raise RuntimeError("GDAL warp from WMTS VRT failed")
    actual_gt = result.GetGeoTransform()
    gt_matches = all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9)
                     for a, b in zip(actual_gt, reference["gt"]))
    projection_matches = same_epsg_3857(result.GetProjection())
    report = {
        "layer": args.layer,
        "style": style_id,
        "capabilities_url": args.capabilities_url,
        "tile_matrix_set": args.tile_matrix_set,
        "tile_matrix": args.zoom,
        "source_crs": source_crs,
        "tile_count": total,
        "raw_tiles_directory": str(tile_root),
        "raw_vrt": str(raw_vrt),
        "raw_size": [raw_cols, raw_rows],
        "raw_pixel_size": matrix["pixel_span"],
        "output": str(out),
        "size": [result.RasterXSize, result.RasterYSize],
        "bands": result.RasterCount,
        "dtype": gdal.GetDataTypeName(result.GetRasterBand(1).DataType),
        "geotransform_matches_reference": gt_matches,
        "projection_matches_epsg_3857": projection_matches,
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
        projection_matches,
        report["tfw_exists"],
        report["overviews"] == 0,
    ]):
        raise RuntimeError("Aligned EOX WMTS output validation failed")


if __name__ == "__main__":
    main()
