"""Stage, verify and publish a completed download; keep source tiles for retries."""
import json
import math
import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from pathlib import Path


@contextmanager
def cache_lock(work):
    work.mkdir(parents=True, exist_ok=True)
    with (work / "download.lock").open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("CACHE_BUSY：相同任务正在使用缓存，请待其结束后重试。") from exc
        yield  # Closing the handle releases the OS lock, including after a process exit.


def verify(path, grid):
    from osgeo import gdal, osr
    ds = gdal.Open(str(path))
    expected = osr.SpatialReference()
    expected.ImportFromEPSG(3857)
    actual = ds.GetSpatialRef()
    if actual is None:
        raise ValueError("成果缺少坐标系。")
    for srs in (actual, expected):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    size = [ds.RasterXSize, ds.RasterYSize]
    gt = ds.GetGeoTransform()
    if (not actual.IsSame(expected) or size != grid["size"] or
            not all(math.isclose(a, b, rel_tol=0, abs_tol=1e-6) for a, b in zip(gt, grid["geotransform"]))):
        raise ValueError("成果坐标系、行列数或空间位置与预检不符。")
    bands = [ds.GetRasterBand(i + 1) for i in range(ds.RasterCount)]
    if len(bands) != 3 or any(b.DataType != gdal.GDT_Byte for b in bands):
        raise ValueError("成果需要三个 Byte 波段。")
    colors = [b.GetColorInterpretation() for b in bands]
    if colors != [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand]:
        raise ValueError("成果缺少明确的红、绿、蓝波段标记。")
    world = [float(x) for x in path.with_suffix(".tfw").read_text().split()]
    expected_world = [gt[1], gt[4], gt[2], gt[5], gt[0]+gt[1]/2, gt[3]+gt[5]/2]
    if len(world) != 6 or not all(math.isclose(a,b,rel_tol=0,abs_tol=1e-6) for a,b in zip(world,expected_world)):
        raise ValueError("世界文件与 GeoTIFF 网格不一致。")
    samples = [b.ReadRaster(buf_xsize=min(size[0], 64), buf_ysize=min(size[1], 64)) for b in bands]
    if not all(samples):
        raise ValueError("成果像元无法读取。")
    uniform = all(len(set(s)) == 1 for s in samples)
    return {"grid_verified": True, "world_file_verified": True, "bands": 3, "color_interpretation": ["red", "green", "blue"], "dtype": "Byte",
            "sample_warning": "抽样像元颜色一致，请检查空白或服务占位图。" if uniform else None}


def publish_new(src, dest):
    if os.name == "nt":
        os.rename(src, dest)  # Fails if another task created the destination.
    else:
        os.link(src, dest)


def download_and_verify(plan, vrt):
    work, final = Path(plan["work_dir"]), Path(plan["output"])
    with cache_lock(work), ExitStack() as cleanup:
        if vrt is not None:
            ref = Path(plan["reference_raster"])
            if ref.exists():
                if ref.read_text(encoding="utf-8") != vrt:
                    raise ValueError(f"缓存网格与任务不一致：{ref}")
            else:
                ref.write_text(vrt, encoding="utf-8")
        final.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(cleanup.enter_context(tempfile.TemporaryDirectory(dir=final.parent, prefix=".imagery-")))
        tif = stage / "imagery.tif"
        command = list(plan["command"])
        command[command.index("--out") + 1] = str(tif)
        parts = []
        if plan["raw_vrt"]:
            raw = Path(plan["raw_vrt"])
            raw.parent.mkdir(parents=True, exist_ok=True)
            # Keep relative tile references valid after renaming the staged VRT.
            raw_tmp = raw.with_name(f".imagery-{uuid.uuid4().hex}.vrt")
            cleanup.callback(raw_tmp.unlink, missing_ok=True)
            cleanup.callback(raw_tmp.with_suffix(".vrt.part").unlink, missing_ok=True)
            command[command.index("--raw-vrt") + 1] = str(raw_tmp)
            parts.append((raw_tmp, raw))
        status = subprocess.run(command, check=False).returncode
        if status:
            print(json.dumps({"status": "DOWNLOAD_FAILED", "source": plan["source"],
                              "exit_code": status, "work_dir": str(work)}, ensure_ascii=False))
            return status
        report = {k: v for k, v in plan.items() if k != "command"}
        report["acquisition_date"] = plan.get("acquisition_date")
        if report["acquisition_date"] is not None:
            from imagery_dates import coverage_date
            if coverage_date(plan["grid"]) != plan["date_evidence"]:
                raise RuntimeError("METADATA_CHANGED：下载期间日期或范围记录发生变化，已停止发布。")
        report["product_type"] = "annual composite" if plan["layer"] else "basemap mosaic"
        report["completed_utc"] = datetime.now(timezone.utc).isoformat()
        report["quality"] = verify(tif, plan["grid"])
        receipt = stage / "receipt.json"
        receipt.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        parts.extend([(tif.with_suffix(".tfw"), final.with_suffix(".tfw")),
                      (receipt, Path(plan["receipt"])), (tif, final)])
        published = []
        try:
            for src, dest in parts:
                publish_new(src, dest)
                published.append(dest)
        except OSError:
            for dest in reversed(published):
                dest.unlink(missing_ok=True)
            raise
        print(json.dumps({"status": "SUCCESS", "output": str(final), "receipt": plan["receipt"],
                          "quality": report["quality"], "requested_date": plan["requested_date"],
                          "acquisition_date": report["acquisition_date"],
                          "lookback_days": plan.get("lookback_days")}, ensure_ascii=False))
        return 0
