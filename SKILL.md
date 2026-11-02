---
name: auto-imagery-download
description: 按 DEM/参考栅格，或按经纬度范围和输出分辨率，下载 Esri、EOX 或 Bing RGB 底图影像并生成 EPSG:3857 GeoTIFF。适用于已有栅格配图或无 DEM 的区域影像获取。
---

# 按 DEM 或经纬度范围下载影像

统一入口为 `scripts/download_imagery.py`，提供两个互斥输入选项：

1. **有 DEM/参考栅格**：传入 `--reference-raster`，直接沿用其范围、像元大小、行列数和网格位置。参考文件须为北向上 EPSG:3857；其他 CRS 需要先重投影为另一个文件。不修改输入文件。
2. **没有 DEM**：传入 `--bbox 西经度 南纬度 东经度 北纬度` 和 `--resolution 米/像元`，自动建立 EPSG:3857 虚拟参考网格并下载。输入为 WGS84 十进制度，西经/南纬用负数；经纬度顺序不能颠倒。

两种模式都需要数据源 `--source esri|eox|bing`、成果路径 `--out` 和缓存目录 `--work-dir`。按用户已经提供的信息选择模式；如果缺少数据源、范围或 bbox 模式的分辨率，只询问缺失项，不要求没有 DEM 的用户提供 DEM。参考栅格模式禁止再指定 `--resolution`，以保证原网格不变。Bing 还必须指定 `--zoom`；EOX 默认使用第 13 级。

经纬度模式保持指定的方形像元大小，向外扩展不足一个像元以覆盖完整范围；通常扩展东、南边界，世界边缘附近改为西、北边界。报告实际输出范围及行列数，不声称边界与输入小数坐标完全一致。`--resolution` 是 EPSG:3857 地图坐标中的米，不是各纬度处恒定的地面距离，也不表示源影像原生分辨率。纬度须在约 ±85.0511287798066° 内，且西 < 东、南 < 北；跨越 ±180° 的区域先拆成两块。

先添加 `--dry-run` 检查实际范围、行列数、未压缩 RGB 字节数和执行命令；它不联网、不创建文件。确认参数正确后使用相同命令去掉 `--dry-run` 执行，不额外引入确认步骤。bbox 预览只需要 Python；正式下载和参考栅格预览需要该 Python 环境安装 GDAL Python 绑定（`osgeo`）。根据当前机器选择可用环境，不照搬其他用户的解释器路径。

以下命令在本 skill 目录下执行；`python` 应为已启用 GDAL 的 Python。经纬度仅为用法示例。

```powershell
# 选项一：有 DEM，直接按其网格下载
python scripts/download_imagery.py --source esri `
  --reference-raster "D:\data\area_dem_3857_30m.tif" `
  --out "D:\data\area_esri.tif" --work-dir "D:\task\imagery_cache"

# 选项二：没有 DEM，输入西、南、东、北和目标网格大小
python scripts/download_imagery.py --source esri `
  --bbox 103.80 1.25 103.90 1.35 --resolution 30 `
  --out "D:\data\area_esri_bbox.tif" --work-dir "D:\task\imagery_cache" `
  --dry-run
```

EOX 使用同样的两种输入选项，将 `--source` 改为 `eox`，可加 `--layer s2cloudless-2025_3857 --zoom 13 --raw-vrt "D:\data\raw\area_eox.vrt"`；不指定 `--raw-vrt` 时原始瓦片 VRT 保存在工作目录。Bing 将 `--source` 改为 `bing` 并加 `--zoom 15`（示例级别，按需求选择）。每次使用不同的输出文件名。

统一入口按来源、网格和来源参数隔离缓存，避免不同区域错误复用 Esri 分块。bbox 模式在缓存中生成的 `bbox_reference.vrt` 只描述网格，不是 DEM，也不包含高程或遥感像元。三种来源仍调用下面的现有下载脚本；原有 `--reference-raster` 命令继续可用，最终 GeoTIFF、`.tfw` 和 EOX 原始 VRT 都拒绝覆盖。

## Esri World Imagery

1. 检查参考栅格为北向上、EPSG:3857；记录范围、像元大小、行列数和 CRS。
2. 将成果写到用户指定目录，临时分块写到任务工作目录或 `--work-dir`，不要混入成果目录。
3. 默认使用 2048 像素分块。服务声明单次可达 4096，但高纹理区域较容易超时；除非用户明确要求并接受重试风险，不要调到 4096。
4. 脚本调用 `World_Imagery/MapServer/export`，逐块传入 `bbox`、`bboxSR=3857`、`imageSR=3857`、实际块尺寸与 `f=image`，下载 JPG 后组装为 GeoTIFF。
5. 检查结果为三波段 `Byte RGB`，范围、行列数、仿射变换和 CRS 与参考栅格一致，并有同名 `.tfw`。默认不创建金字塔。

```powershell
python scripts/download_esri_world_imagery.py `
  --reference-raster "D:\data\area_dem_3857_30m.tif" `
  --out "D:\data\area_EsriWorldImagery3857_30m_Byte.tif" `
  --work-dir "D:\task\esri_imagery_work"
```

## Bing 航空影像

使用用户提供的瓦片模板：`http://ecn.t3.tiles.virtualearth.net/tiles/a{q}.jpeg?g=1`。`q` 是 QuadKey；脚本根据 EPSG:3857 参考范围、指定缩放级别和 256×256 瓦片矩阵计算所需瓦片，只顺序请求覆盖范围内的瓦片，再按参考栅格网格输出 RGB GeoTIFF。缩放级别控制源瓦片网格，输出网格仍以参考栅格为准，不代表影像原生分辨率。

```powershell
python scripts/download_bing_imagery.py `
  --reference-raster "D:\data\area_dem_3857_30m.tif" `
  --out "D:\data\area_Bing_3857_30m.tif" `
  --work-dir "D:\task\bing_imagery_work" `
  --zoom 15
```

脚本会在 `--work-dir` 中保留原始 JPEG 缓存和 VRT，以便复用或重建。只请求覆盖参考范围的瓦片，顺序下载并遵守服务端访问限制。Bing QuadKey 由瓦片 XYZ 坐标交织计算；细节见 [Microsoft Bing Maps Tile System](https://learn.microsoft.com/bingmaps/articles/bing-maps-tile-system/)。

## Google 卫星瓦片 URL

用户给出的旧式 URL 模板为：`http://www.google.cn/maps/vt?lyrs=s@189&gl=cn&x={x}&y={y}&z={z}`。可用单张瓦片检查 URL 是否响应 JPEG，但不要将该旧式地址用于批量瓦片缓存或离线 GeoTIFF 导出。Google 官方卫星瓦片接口使用 Map Tiles API，需要 API key 和会话令牌；其政策也限制预取、缓存和离线用途。参见 [Satellite tiles](https://developers.google.com/maps/documentation/tile/satellite) 与 [Map Tiles API Policies](https://developers.google.com/maps/documentation/tile/policies)。

## EOX Sentinel-2 cloudless WMTS

能力文档：<https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml>。先根据用户指定年份和能力文档选图层；当前 2025 年 EPSG:3857 图层为 `s2cloudless-2025_3857`，格式为 `image/jpeg`，提供 `GoogleMapsCompatible` 与 `g` 两个 Web Mercator 瓦片矩阵集。默认优先使用 `GoogleMapsCompatible`。脚本默认下载第 13 级原始瓦片；若用户指定其他级别，使用其明确值。

`s2cloudless` 是 EOX 发布的 Sentinel-2 无云合成底图，不是可逐景下载的 Sentinel-2 原始波段产品。WMTS 瓦片是 JPEG 显示影像。第 13 级在赤道处约为 19.11 米/像元地图分辨率，地面分辨率随纬度变化；不能称为 30 米原生影像。脚本保留原始 JPEG 瓦片，并生成未重采样的瓦片级 `.vrt`；另外按参考 GeoTIFF 的范围和行列数双线性重采样，写出最终对齐 GeoTIFF。两类成果分开保存。

```powershell
python scripts/download_eox_wmts.py `
  --reference-raster "D:\data\area_dem_3857_30m.tif" `
  --raw-vrt "D:\data\raw\s2cloudless_2025_z13.vrt" `
  --out "D:\data\aligned\s2cloudless_2025_3857_30m.tif" `
  --work-dir "D:\task\eox_wmts_tiles"
```

默认图层为 `s2cloudless-2025_3857`，默认瓦片矩阵为 `GoogleMapsCompatible`，默认级别为 `13`。使用其他年份时，先从能力文档确认对应的 `_3857` 图层及其矩阵集，再通过 `--layer` 指定；不要把 EPSG:4326 的 `WGS84` 矩阵集直接用于 EPSG:3857 瓦片请求。

脚本顺序请求瓦片并使用重试，缓存目录可供中断后续跑。保留 `--work-dir` 中的 JPEG 瓦片，因为 `.vrt` 通过相对路径引用它们；移动原始 VRT 时一并保留瓦片缓存或重新生成 VRT。最终栅格检查三波段 `Byte RGB`、EPSG:3857、像元网格、范围和行列数与参考栅格一致，并有同名 `.tfw`。默认不创建金字塔。

## 约束与风险

- Esri World Imagery 走其正式导出接口；不要用海量 XYZ 瓦片绕开服务限制，保持顺序请求和脚本默认重试。
- EOX WMTS 只下载覆盖参考范围所需的瓦片；缩放级别越高，请求数越多。尊重服务端访问限制，顺序请求。
- EOX 原始 `.vrt` 表示所选 WMTS 级别的瓦片网格；只有最终对齐 GeoTIFF 才对应参考栅格分辨率。保留 JPEG 缓存以便重建 VRT，不要将其描述为无损 Sentinel-2 原始数据。
- 输出 30 m 只表示栅格使用 30 m 网格，不代表 Esri、EOX 或 Bing 源影像处处具有 30 m 原生分辨率。
- 成果会保留源影像中的云、镶嵌差异、NoData、接缝或空白瓦片；下载后检查预览，不能把服务端影像瑕疵归因于本地拼接。
- 最终影像使用 `DEFLATE + PREDICTOR=2`、`TILED=YES`、`BIGTIFF=IF_SAFER`，并写出 `.tfw`。
