# MERIT-Basins 区域河网下载提示词

复制下面整段提示词，按需修改经纬度范围和输出目录。

```text
请使用 D:\PhD\Biodiversity_VLM\Agent\skill\merit-hydro-download 下的技能，实际运行 scripts/download.py，下载指定范围的 MERIT-Basins 矢量河网。

经纬度范围：[120.00, 30.00, 120.05, 30.03]
坐标顺序：西、南、东、北，坐标系为 WGS84（EPSG:4326）。
输出目录：D:\遥感数据\MERIT河网\研究区01

请交付两份成果：

1. 区域河网 Shapefile。按上述边界裁剪河段，输出 .shp、.shx、.dbf、.prj、.cpg，并将这五个文件打成一个 ZIP。
2. 河段属性 river_attributes.json。从本次裁剪后的 Shapefile 读取属性，保存为 UTF-8 JSON 数组，每个 COMID 一条记录。保留全部属性字段，包括 COMID、NextDownID、原始整段长度 lengthkm 和范围内长度 clip_km，省略几何坐标。

如果范围内没有非零长度的河段，说明原因并停止。如果成功，核对 Shapefile 与属性 JSON 的河段数和 COMID 集合一致，再告诉我命中的分区、河段数，并提供 .shp、完整 ZIP 和属性 JSON 的文件链接。
```
