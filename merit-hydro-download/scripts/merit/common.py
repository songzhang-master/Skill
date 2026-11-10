"""Vector validation, identifiers, upstream tracing and file digests."""
import hashlib
import math
from pathlib import Path


def qc(issue_id, severity, issue, impact, remediation=""):
    return dict(issue_id=issue_id, severity=severity, issue=issue,
                impact=impact, remediation=remediation)


def identifier(value):
    """MERIT ID 统一为十进制整数字符串；0 表示无相邻河段。"""
    number = float(value)
    if not math.isfinite(number) or not number.is_integer() or number < 0:
        raise ValueError(f"无效河段 ID：{value}")
    return str(int(number))


def upstream_ids(targets, downstream):
    """按 NextDownID 建反向邻接表，追溯所有上游（包括目标自身）。"""
    reverse = {}
    for reach, next_id in downstream.items():
        reverse.setdefault(next_id, []).append(reach)
    selected, pending = set(), list(targets)
    while pending:
        reach = pending.pop()
        if reach in selected:
            continue
        if reach not in downstream:
            raise ValueError(f"源河网中缺少河段 {reach}")
        selected.add(reach)
        pending.extend(reverse.get(reach, []))
    return selected


def descriptor(path, **metadata):
    path = Path(path).resolve(strict=True)
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return {"path": str(path), "sha256": hasher.hexdigest(), **metadata}


def read_vector(path, required=()):
    import geopandas as gpd
    frame = gpd.read_file(path)
    if frame.empty or frame.crs is None:
        raise ValueError(f"空矢量或未声明 CRS：{path}")
    if frame.geometry.isna().any() or frame.geometry.is_empty.any() or not frame.geometry.is_valid.all():
        raise ValueError(f"无效或空几何：{path}；不自动修改原数据")
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"缺少字段：{sorted(missing)}")
    return frame


def river_frame(path):
    frame = read_vector(path, ("COMID", "NextDownID"))
    frame["COMID"] = frame["COMID"].map(identifier)
    frame["NextDownID"] = frame["NextDownID"].map(identifier)
    if frame["COMID"].duplicated().any() or (frame["COMID"] == "0").any():
        raise ValueError("COMID 必须唯一且非零")
    if not frame.geom_type.isin(["LineString", "MultiLineString"]).all():
        raise ValueError("河网必须是线要素")
    return frame
