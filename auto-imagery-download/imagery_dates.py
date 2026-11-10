"""Check the published Esri imagery footprint at the export scale for a dated request."""
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request

from imagery_sources import ESRI_MAP, open_source

LOOKBACK_DAYS = 15
EXPORT_DPI = 96


def candidate_days(target):
    start = date.fromisoformat(target)
    count = min(LOOKBACK_DAYS + 1, start.toordinal())
    return [(start - timedelta(days=n)).isoformat() for n in range(count)]


def metadata_json(endpoint, **params):
    url = endpoint + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "Codex-GIS/1.0"})
    with open_source("esri_metadata", req, timeout=60) as response:
        data = json.load(response)
    if not isinstance(data, dict) or "error" in data:
        raise RuntimeError(f"METADATA_ERROR：Esri 日期核验接口返回异常：{str(data)[:400]}")
    return data


def capture_day(attrs):
    # SRC_DATE is YYYYMMDD. SRC_DATE2 is the corresponding ArcGIS UTC timestamp.
    raw, stamp = attrs.get("SRC_DATE"), attrs.get("SRC_DATE2")
    if raw is None and stamp is None:
        return None
    try:
        days = set()
        if raw is not None:
            if len(str(raw)) != 8:
                return None
            days.add(datetime.strptime(str(raw), "%Y%m%d").date())
        if stamp is not None:
            days.add(datetime.fromtimestamp(stamp / 1000, timezone.utc).date())
        return next(iter(days)).isoformat() if len(days) == 1 else None
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def coverage_date(grid):
    from osgeo import ogr

    gt = grid["geotransform"]
    # ArcGIS export uses 96 DPI and 39.37 inches per projected metre.
    scale = gt[1] * EXPORT_DPI * 39.37
    service = metadata_json(ESRI_MAP, f="json")
    layers = [layer for layer in service.get("layers", [])
              if 5 <= layer["id"] <= 18 and layer["name"].endswith("Resolution Metadata")
              and (not layer["minScale"] or scale <= layer["minScale"])
              and scale > layer["maxScale"]]
    if len(layers) != 1:
        raise RuntimeError("METADATA_ERROR：当前导出比例尺没有唯一的 Esri 日期元数据图层。")
    endpoint = f"{ESRI_MAP}/{layers[0]['id']}/query"
    params = dict(geometry=",".join(map(str, grid["bounds"])), geometryType="esriGeometryEnvelope",
                  inSR=3857, spatialRel="esriSpatialRelIntersects")
    listing = metadata_json(endpoint, **params, f="json", returnIdsOnly="true")
    if "objectIds" not in listing:
        raise RuntimeError("METADATA_ERROR：服务未返回完整的区域记录清单。")
    ids = set(listing["objectIds"] or [])
    base = {"metadata_endpoint": endpoint, "scale": scale, "coverage_ids": sorted(ids)}
    if not ids:
        return dict(base, acquisition_date=None, reason="范围内无日期元数据")
    data = metadata_json(endpoint, **params, f="geojson", outSR=3857, returnGeometry="true",
                         outFields="OBJECTID,SRC_DATE,SRC_DATE2,NICE_NAME")
    features = data.get("features", [])
    if ({f["properties"]["OBJECTID"] for f in features} != ids or
            data.get("exceededTransferLimit") or (data.get("properties") or {}).get("exceededTransferLimit")):
        raise RuntimeError("METADATA_INCOMPLETE：返回的覆盖记录不完整，无法判定成像日。")
    days = {capture_day(f["properties"]) for f in features}
    if len(days) != 1 or None in days:
        return dict(base, acquisition_date=None, reason="相交记录含不同成像日或缺少可靠日期")
    crs = data.get("crs", {}).get("properties", {}).get("name", "")
    if crs.upper() not in ("EPSG:3857", "URN:OGC:DEF:CRS:EPSG::3857"):
        raise RuntimeError("METADATA_ERROR：覆盖几何的坐标系无法核验。")
    west, south, east, north = grid["bounds"]
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in [(west, south), (east, south), (east, north), (west, north), (west, south)]:
        ring.AddPoint_2D(x, y)
    remaining = ogr.Geometry(ogr.wkbPolygon)
    remaining.AddGeometry(ring)
    for feature in features:
        footprint = ogr.CreateGeometryFromJson(json.dumps(feature["geometry"]))
        if footprint is None or not footprint.IsValid():
            raise RuntimeError("METADATA_ERROR：覆盖几何无效。")
        remaining = remaining.Difference(footprint)
        if remaining is None:
            raise RuntimeError("METADATA_ERROR：覆盖范围运算失败。")
        if remaining.IsEmpty():
            break
    if not remaining.IsEmpty():
        return dict(base, acquisition_date=None, reason="可核验的范围未覆盖整个输出网格")
    signature = hashlib.sha256(json.dumps(sorted(features, key=lambda f: f["properties"]["OBJECTID"]),
                               sort_keys=True).encode()).hexdigest()
    return dict(base, acquisition_date=days.pop(), footprint_sha256=signature)
