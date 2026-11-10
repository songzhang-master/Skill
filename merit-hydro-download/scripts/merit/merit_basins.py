"""MERIT-Basins vector acquisition. Source catalog is replaceable by deployment.
No Google Earth Engine, login, SDK, or model reasoning is needed for selection.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from .common import descriptor, river_frame, upstream_ids
from .merit_download import validate_bbox

SKILL_DIR = Path(__file__).resolve().parents[2]
INDEX_BASE='https://raw.githubusercontent.com/mheberger/upstream-delineator/c40d78f0b0dca83b0e74485825785c22ff142ba3/data/shp/basins_level2/merit_hydro_vect_level2'
CATALOG=SKILL_DIR / 'references' / 'sources.json'


def fetch(url,target,limit=2*1024**3):
    """Stream to partial, validate size; cache hashes detect partial/corrupt files."""
    from urllib.parse import urlparse
    if urlparse(url).scheme!='https':raise ValueError('下载源必须为 HTTPS')
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    stamp=target.with_suffix(target.suffix+'.sha256')
    if target.is_file() and stamp.is_file() and descriptor(target)['sha256']==stamp.read_text().strip():return
    partial=target.with_suffix(target.suffix+'.partial')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url,timeout=90) as response, partial.open('wb') as output:
                if 'text/html' in response.headers.get('Content-Type',''):
                    raise ValueError('数据源返回网页而非文件，可能要求登录或达到下载限额；请由部署者更新镜像')
                size=0
                while block:=response.read(1024*1024):
                    size+=len(block)
                    if size>limit:raise ValueError('数据包超过本次下载大小上限')
                    output.write(block)
            if not size:raise ValueError('下载文件为空')
            partial.replace(target)
            stamp.write_text(descriptor(target)['sha256'],encoding='ascii')
            return
        except (urllib.error.URLError,TimeoutError,OSError):
            if attempt==2:raise ConnectionError('MERIT 文件下载失败，有限重试已结束') from None
            time.sleep(2**attempt)


def select_archives(index,bbox,catalog):
    from shapely.geometry import box
    basins=sorted({str(int(v)) for v in index[index.intersects(box(*bbox))]['BASIN']})
    if not basins:raise ValueError('范围未与 MERIT-Basins 陆地流域索引相交')
    selected={}
    for basin in basins:
        code=basin if basin in catalog['archives'] else basin[0]
        if code not in catalog['archives']:raise ValueError('目录没有覆盖流域 '+basin+' 的下载源')
        selected[code]=catalog['archives'][code]
    # A continent archive already contains its child basins; avoid duplicate COMIDs.
    selected={code:item for code,item in selected.items() if len(code)==1 or code[0] not in selected}
    return basins,selected


def extract_rivers(archive,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        members=[m for m in z.infolist() if not m.is_dir() and re.fullmatch(r'riv_pfaf_\d+_MERIT_Hydro_v07_Basins_v01\.(?:shp|shx|dbf|prj|cpg)',Path(m.filename.replace('\\','/')).name,re.I)]
        if not members or sum(m.file_size for m in members)>8*1024**3:raise ValueError('压缩包未含预期河网文件或解压体积超限')
        names=set()
        for member in members:
            name=Path(member.filename.replace('\\','/')).name
            if name in names:raise ValueError('压缩包存在重复河网组件名')
            names.add(name)
            # Flatten only allowlisted names. Archive directory paths are never executed/extracted.
            with z.open(member) as source,(directory/name).open('wb') as output:
                while chunk:=source.read(1024*1024):output.write(chunk)
    rivers=sorted(directory.glob('*.shp'))
    for p in rivers:
        for ext in ('.shx','.dbf','.prj'):
            if not p.with_suffix(ext).is_file():raise ValueError('河网压缩包缺少配套文件 '+ext)
    return rivers


def clip_reaches(frame, area):
    """Keep exactly one line or multipart line per COMID with length inside the bbox."""
    from shapely.geometry import MultiLineString

    def parts(geom):
        if geom.geom_type == 'LineString' and geom.length > 0:
            yield geom
        elif geom.geom_type in ('MultiLineString', 'GeometryCollection'):
            for part in geom.geoms:
                yield from parts(part)

    hits = frame.loc[frame.intersects(area)].copy()
    clipped = []
    indices = []
    for index, geometry in hits.geometry.intersection(area).items():
        lines = list(parts(geometry))
        if lines:
            indices.append(index)
            clipped.append(lines[0] if len(lines) == 1 else MultiLineString(lines))
    if not indices:
        raise ValueError('下载分区内没有落入指定范围的非零长度河段（仅接触一个点不计）。')
    hits = hits.loc[indices].copy()
    hits.geometry = clipped
    geod = hits.crs.get_geod()
    hits['clip_km'] = [geod.geometry_length(line) / 1000 for line in clipped]
    return hits


def export_bbox_shapefile(clipped, area, output_dir):
    """Publish the complete, validated layer and its portable ZIP as a group."""
    import geopandas as gpd

    layer = clipped.copy()
    for field in ('COMID', 'NextDownID'):
        layer[field] = layer[field].astype('int64')
    with tempfile.TemporaryDirectory(dir=output_dir) as staging:
        group = Path(staging) / 'layer'
        group.mkdir()
        shp = group / 'MERIT_rivers.shp'
        layer.to_file(shp, driver='ESRI Shapefile', index=False, encoding='UTF-8')
        pieces = [shp.with_suffix(ext) for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')]
        if not all(p.is_file() and p.stat().st_size > 0 for p in pieces):
            raise ValueError('区域 Shapefile 组件不完整。')
        actual = gpd.read_file(shp).set_index('COMID').sort_index()
        expected = layer.set_index('COMID').sort_index()
        if (actual.crs is None or actual.crs.to_epsg() != 4326 or
                not actual.index.equals(expected.index) or
                actual.NextDownID.tolist() != expected.NextDownID.tolist() or
                not actual.geometry.covered_by(area).all() or
                not actual.geometry.geom_equals(expected.geometry).all()):
            raise ValueError('区域 Shapefile 的坐标系、线几何或拓扑编号核验失败。')
        bundle = shp.with_suffix('.zip')
        with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for piece in pieces:
                with piece.open('rb') as source, archive.open(piece.name, 'w', force_zip64=True) as dest:
                    shutil.copyfileobj(source, dest, length=1024*1024)
        # A single rename exposes the five components together. Identical reruns can reuse them.
        destination = output_dir / ('bbox_rivers_' + descriptor(bundle)['sha256'][:16])
        members = pieces + [bundle]
        try:
            group.rename(destination)
        except OSError:
            if not destination.is_dir():
                raise
            for member in members:
                if descriptor(destination / member.name)['sha256'] != descriptor(member)['sha256']:
                    raise ValueError(f'已有区域 Shapefile 组件与本次导出不一致：{member.name}')
        return destination / shp.name, destination / bundle.name, [descriptor(destination / p.name) for p in members]


class MeritBasinsTools:
    def __init__(self, output_dir, cache_dir=None):
        self.output_dir = Path(output_dir)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else self.output_dir / "cache"
    def acquire(self,bbox,threshold_km2=1.,project=''):
        validate_bbox(bbox)
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import box,mapping
        catalog=json.loads(Path(os.environ.get('MERIT_SOURCE_CATALOG',str(CATALOG))).read_text(encoding='utf-8'))
        if catalog['version']!='MERIT_Hydro_v07_Basins_v01':raise ValueError('不混用不同 MERIT 版本')
        cache=self.cache_dir/'merit-basins'; indexdir=self.cache_dir/'merit-index'
        for suffix in ('.shp','.shx','.dbf','.prj'):
            fetch(INDEX_BASE+suffix,indexdir/('basins'+suffix),100*1024**2)
        index=gpd.read_file(indexdir/'basins.shp').to_crs(4326)
        basins,selected=select_archives(index,bbox,catalog)
        frames=[];sources=[]
        for code,item in selected.items():
            key=hashlib.sha256(item['url'].encode()).hexdigest()[:16]
            archive=cache/key/'source.zip'
            fetch(item['url'],archive)
            for path in extract_rivers(archive,cache/key/'rivers'):
                frames.append(river_frame(path).to_crs(4326))
            sources.append({'region':code,'url':item['url'],'archive':descriptor(archive)})
        merged=gpd.GeoDataFrame(pd.concat(frames,ignore_index=True),crs=4326)
        if merged.COMID.duplicated().any():raise ValueError('来源分区出现重复 COMID，需核对分区版本')
        area=box(*bbox)
        clipped = clip_reaches(merged, area)
        selected_ids = set(clipped.COMID)
        graph=dict(zip(merged.COMID,merged.NextDownID))
        selection_only = getattr(self, 'selection_only', False)
        wanted = selected_ids if selection_only else upstream_ids(selected_ids,graph)
        outside=set(merged.COMID) - selected_ids
        exits=[{'reach_id':rid,'downstream_id':graph[rid], 'basis':'downstream_exists_in_source_and_outside_bbox'}
               for rid in sorted(wanted) if graph[rid] not in wanted and graph[rid] in outside]
        self.output_dir.mkdir(parents=True,exist_ok=True)
        target=self.output_dir/'source_rivers.geojson'
        if selection_only:
            merged = merged[merged.COMID.isin(selected_ids)]
        merged.to_file(target,driver='GeoJSON')
        boundary=self.output_dir/'boundary.geojson'
        boundary.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','properties':{},'geometry':mapping(box(*bbox))}]}),encoding='utf-8')
        shp, bundle, shape_files = export_bbox_shapefile(clipped, area, self.output_dir)
        result={'status':'ready','source':'MERIT-Basins '+catalog['version'],'source_page':catalog['source_page'],
            'bbox':bbox,'basins':basins,'download_regions':list(selected),'archives':sources,
            'river_file':str(target.resolve()),'boundary_file':str(boundary.resolve()),'outlet_ids':[x['reach_id'] for x in exits],'boundary_exits':exits,
            'id_kind':'official_merit_basins_comid','upstream_scope':'bbox_only' if selection_only else 'source_basins_pending_topology_check',
            'threshold_km2':25,'scope':'bbox_selection' if selection_only else 'bbox_selection_with_upstream_completion','qc':[],
            'bbox_shapefile':str(shp.resolve()),'bbox_shapefile_zip':str(bundle.resolve()),
            'bbox_reach_count':len(clipped),'files':[descriptor(target),descriptor(boundary),*shape_files],
            'index_source':INDEX_BASE,'index':descriptor(indexdir/'basins.shp')}
        (self.output_dir/'download_manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result
