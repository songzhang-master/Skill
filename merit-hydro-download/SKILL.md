---
name: merit-hydro-download
description: 按 WGS84 经纬度范围下载 MERIT-Basins 河网，默认交付已裁剪的区域线 Shapefile 及完整组件 ZIP，保留 COMID 和 NextDownID，同时提供源分区河网用于上游补全与拓扑检查。
---

# 按范围获取 MERIT 河网

用户只需提供 bbox：[西经度、南纬度、东经度、北纬度]。主控确定本次处理任务，下载步骤按固定程序执行，不让模型每次搜索和重写脚本，不要求终端用户安装软件或登录 Google。

## 默认流程：直接下载矢量河网

1. 使用固定版本的二级流域空间索引，定位与 bbox 相交的分区。
2. 查询 `references/sources.json` 中已核实的官方发布文件目录。优先二级分区；该分区未列出时使用同版本的对应一级分区。目录记录来源页、版本和日期。
3. 下载并校验压缩包，保留原始字段；不改造 COMID，不从栅格重新定义河段。缓存按 URL 区分并检查 SHA-256，损坏文件重新下载。SHA-256 用于一致性校验，不冒充发布方数字签名。
4. 对于 reach_features，仅保留与范围相交的源河段，按边界裁出线几何；只接触一点时不算河段。没有线段则立即报错，不下载影像。对于单独的 river 任务，将完整分区河网和 bbox 边界交给现有 merit.py：先选择相交的完整河段，再按原始拓扑追溯上游、核查断链与环。不能先截掉范围外的源河网再声称完成上游补全。
5. 默认生成范围内的 WGS84 线 Shapefile。逐条裁剪源河段，保留边界内有长度的线段；同一 COMID 被裁成多段时保存为一条多部件记录。保留源属性及下游编号，新增 `clip_km` 记录范围内的 WGS84 椭球长度（千米）；`lengthkm` 仍是原始整段长度。
6. 重新打开 Shapefile 核验几何、边界、坐标系和拓扑编号。将同名的 `.shp`、`.shx`、`.dbf`、`.prj`、`.cpg` 一并打包成 ZIP。成功时交付 ZIP 及可直接打开的 SHP 路径，报告命中的分区和区域河段数。记录来源、版本和文件摘要。只在拓扑检查支持时报告上游关系可靠。

官方发布页： https://www.reachhydro.org/home/params/merit-basins
默认版本：MERIT_Hydro_v07_Basins_v01，保持与已有测试包一致，不混入 bugfix1。
当前已核实的公开分发入口是官方 Google Drive 文件链接。这是普通文件下载，不需要 Earth Engine 项目或 OAuth。下载渠道不作为产品约束；部署者可用 MERIT_SOURCE_CATALOG 指向相同结构的 JSON，将 url 替换为已核实、同版本的 HTTPS 镜像。禁止把 OSM、HydroRIVERS 或栅格推导数据静默冒充 MERIT-Basins。

空间定位索引来自官方页面所致谢的工具作者维护的仓库，固定提交：
https://github.com/mheberger/upstream-delineator/tree/c40d78f0b0dca83b0e74485825785c22ff142ba3/data/shp/basins_level2
索引共 61 个二级流域，仅用于选取下载包。最终按真正河段几何裁剪，不按索引图直接当成果。

```powershell
.venv-data-agent/Scripts/python.exe -m data_agent_v2 --bbox 108.7 22.4 109.1 22.7
```

独立运行时，将整个 `merit-hydro-download` 文件夹复制到目标机器，在该文件夹打开终端。运行环境需要 Python 3.10 或以上及 `requirements.txt` 中的依赖；已配好环境可直接执行第二行。下载只需要经纬度，顺序为最西经度、最南纬度、最东经度、最北纬度。
```powershell
python -m pip install -r requirements.txt
python scripts/download.py --bbox 108.7 22.4 109.1 22.7 --output merit_output
```

独立入口为 `scripts/download.py`。默认矢量下载实现在 `scripts/merit/merit_basins.py`，河段字段校验与文件摘要在 `scripts/merit/common.py`。数据 agent 的 `merit_download.acquire` 接口复用同一实现。跨分区时合并原始记录并检查重复 COMID。ZIP 只解压匹配的河网组件。

成功后读取输出目录的 `download_manifest.json`。`bbox_shapefile` 指向裁剪后的 `MERIT_rivers.shp`，`bbox_shapefile_zip` 指向包含全部五个组件的 ZIP，`bbox_reach_count` 是区域内的河段数。解压整个 ZIP 后在 GIS 中打开 `.shp`，各组件应放在同一目录。`river_file` 指向用于后续拓扑分析的源河网，`boundary_file` 指向用户范围。源河网可以包含范围外的河段。独立运行默认把缓存写在输出目录的 `cache/`，也可以用 `--cache-dir` 指定已有缓存。

## 可选：原始水文栅格

显式指定 `--source hydro_raster` 时，`scripts/merit/merit_download.py` 调用同目录的 `ee_rest.py`，使用部署方的 Earth Engine 授权读取原生水文栅格并推导 D8 网格线段。此模式生成派生编号，上游核验范围限于 bbox。
资料介绍：https://developers.google.com/earth-engine/datasets/catalog/MERIT_Hydro_v1_0_1

## 来源及使用

保留发布页及其许可说明，引用 Lin et al. (2019), DOI:10.1029/2019WR025287 和 Yamazaki et al. (2019), DOI:10.1029/2019WR024873。不同发布渠道或产品许可以对应来源为准，不将 Hydro 栅格许可自动套到所有镜像产品。
