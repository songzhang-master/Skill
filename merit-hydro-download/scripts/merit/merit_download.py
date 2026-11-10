"""Deterministic MERIT Hydro download and D8 vector derivation (not MERIT-Basins)."""
import json
import math
import os
from pathlib import Path
from typing import Protocol
from .common import descriptor, qc

DATASET = "MERIT/Hydro/v1_0_1"
BANDS = ["dir", "upa", "elv", "wth"]
D8 = {1:(0,1),2:(1,1),4:(1,0),8:(1,-1),16:(0,-1),32:(-1,-1),64:(-1,0),128:(-1,1)}

class MeritDownloadAdapter(Protocol):
    def acquire(self, bbox: list[float], threshold_km2: float, project: str) -> dict: ...


def validate_bbox(bbox):
    if len(bbox)!=4 or not all(math.isfinite(v) for v in bbox):
        raise ValueError("bbox 需要四个有限数值：西、南、东、北")
    w,s,e,n=bbox
    if not (-180<=w<e<=180 and -60<=s<n<=90):
        raise ValueError("范围应在 MERIT 覆盖区内；跨日期变更线的范围需拆分")


def derive_network(raster, output, threshold_km2):
    import numpy as np
    import rasterio
    import geopandas as gpd
    from shapely.geometry import LineString
    if not math.isfinite(threshold_km2) or threshold_km2<=0:
        raise ValueError("河道提取面积阈值必须大于 0 km²")
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with rasterio.open(raster) as src:
        if src.crs.to_epsg()!=4326 or src.transform.b or src.transform.d:
            raise ValueError("D8 必须使用北向上的原生 EPSG:4326 网格")
        direction=src.read(1,masked=True); area=src.read(2,masked=True)
        active=(~np.ma.getmaskarray(direction)) & (~np.ma.getmaskarray(area)) & (area.filled(-1)>=threshold_km2)
        height,width=active.shape
        if np.count_nonzero(active)>250000: raise ValueError("河道单元超过 25 万，请缩小范围或提高面积阈值")
        rows=[]; outlets=[]; omitted=0
        # Global grid-center IDs are derived identifiers, never official COMID.
        def identifier(r,c):
            x,y=src.transform*(c+.5,r+.5)
            return str(int(round((90-y)*1200-.5))*432000+int(round((x+180)*1200-.5))+1)
        for r,c in zip(*np.where(active)):
            code=int(direction[r,c])
            if code in (0,-1):
                omitted+=1; continue  # terminal pixels are not fabricated as line reaches
            if code not in D8: raise ValueError("未知 D8 流向编码")
            dr,dc=D8[code]; rr,cc=r+dr,c+dc
            inside=0<=rr<height and 0<=cc<width
            next_active=inside and bool(active[rr,cc]) and int(direction[rr,cc]) in D8
            rid=identifier(r,c)
            nxt=identifier(rr,cc) if next_active or not inside else "0"
            if not inside: outlets.append(rid)
            rows.append({"COMID":rid,"NextDownID":nxt,"uparea":float(area[r,c]),
                "id_kind":"derived_merit_grid_cell", "end_kind":"linked" if next_active else "extent_exit" if not inside else "terminal_or_threshold",
                "geometry":LineString([src.transform*(c+.5,r+.5),src.transform*(cc+.5,rr+.5)])})
        if not rows: raise ValueError("该范围及面积阈值下没有可输出的河道线段")
        target=output/"derived_rivers.geojson"
        gpd.GeoDataFrame(rows,crs=src.crs).to_file(target,driver="GeoJSON")
    return {"river_file":str(target.resolve()),"outlet_ids":outlets,"terminal_pixels":omitted,
            "files":[descriptor(target)],"id_kind":"derived_merit_grid_cell",
            "upstream_scope":"downloaded_bbox_only", "threshold_km2":threshold_km2}


class MeritDownloadTools:
    def __init__(self, output_dir): self.output_dir=Path(output_dir)
    def acquire(self,bbox,threshold_km2,project):
        validate_bbox(bbox)
        import io
        import rasterio
        import numpy as np
        from affine import Affine
        from rasterio.windows import Window
        from shapely.geometry import box, mapping
        from .ee_rest import EarthEngineREST
        client=EarthEngineREST(project)
        metadata=json.loads(client.request())
        bands={b['id']:b for b in metadata['bands']}
        native=bands['dir']['grid']; af=native['affineTransform']
        transform=Affine(af['scaleX'],af.get('shearX',0),af['translateX'],af.get('shearY',0),af['scaleY'],af['translateY'])
        crs=native.get('crsCode')
        if crs!='EPSG:4326' or transform.b or transform.d or not math.isclose(transform.a,1/1200) or not math.isclose(transform.e,-1/1200):
            raise ValueError('数据源网格不符合已验证的 D8 下载契约')
        w,s,e,n=bbox
        c0,r0=(~transform)*(w,n);c1,r1=(~transform)*(e,s)
        c0,r0=math.floor(c0),math.floor(r0); c1,r1=math.ceil(c1),math.ceil(r1)
        width,height=c1-c0,r1-r0
        if width*height>5_000_000: raise ValueError('范围超过 500 万原生像元，请拆分；不会静默降低流向分辨率')
        root=self.output_dir;root.mkdir(parents=True,exist_ok=True)
        raster=root/'merit_hydro.tif';tmp=root/'merit_hydro.partial.tif'
        grid=transform*Affine.translation(c0,r0)
        with rasterio.open(tmp,'w',driver='GTiff',width=width,height=height,count=4,dtype='float64',crs=crs,transform=grid,nodata=-9999,compress='deflate') as dst:
            for r in range(0,height,512):
                for c in range(0,width,512):
                    tw,th=min(512,width-c),min(512,height-r)
                    tg=grid*Affine.translation(c,r)
                    payload={'fileFormat':'NUMPY_NDARRAY','bandIds':BANDS,'grid':{
                        'dimensions':{'width':tw,'height':th},'crsCode':crs,
                        'affineTransform':{'scaleX':tg.a,'shearX':tg.b,'translateX':tg.c,'shearY':tg.d,'scaleY':tg.e,'translateY':tg.f}}}
                    raw=client.request(':getPixels',payload)
                    values=np.load(io.BytesIO(raw),allow_pickle=False)
                    if values.shape!=(th,tw) or set(values.dtype.names or [])!=set(BANDS):
                        raise ValueError('下载瓦片的尺寸或波段不符')
                    data=np.stack([values[b].astype('float64') for b in BANDS])
                    # getPixels encodes missing/masked pixels as zero. upa<=0 is excluded.
                    data[:,data[1]<=0]=-9999
                    dst.write(data,window=Window(c,r,tw,th))
            dst.descriptions=tuple(BANDS)
        tmp.replace(raster)
        result=derive_network(raster,root,threshold_km2)
        boundary=root/'boundary.geojson'
        boundary.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','properties':{},'geometry':mapping(box(*bbox))}]}),encoding='utf-8')
        result.update(boundary_file=str(boundary.resolve()),source=DATASET,scope='requested_bbox',status='ready',
            raster=descriptor(raster),bbox=bbox,band_units={'dir':'D8','upa':'km2','elv':'m','wth':'m'},
            qc=[qc('merit:extent','MAJOR','河网由 MERIT Hydro 栅格推导，编号不是官方 MERIT-Basins COMID；上游追溯仅覆盖下载范围。','范围外上游完整性未验证，不能称为完整流域')])
        result['files'] += [descriptor(raster),descriptor(boundary)]
        (root/'download_manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result
