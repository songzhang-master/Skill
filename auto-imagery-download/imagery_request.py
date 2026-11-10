"""Read the fixed UTF-8 task form. Both input methods use the same CLI validator."""
import json
import math
import re
from datetime import date
from pathlib import Path


def date_mode(value):
    if value == "不限":
        return "unrestricted"
    if re.fullmatch(r"[0-9]{4}", value) and int(value) >= 1:
        return "annual"
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"无效日历日期：{value}") from exc
        return "daily"
    raise ValueError("成像日期应为 不限、四位年份或 YYYY-MM-DD。")


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"参数重复：{key}")
        result[key] = value
    return result


def request_arguments(file):
    obj = json.loads(Path(file).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_fields)
    if not isinstance(obj, dict):
        raise ValueError("任务参数应为 JSON 对象。")
    options = {"来源": "--source", "成像日期": "--date", "输出目录": "--output-dir",
               "参考栅格": "--reference-raster", "分辨率": "--resolution", "缩放级别": "--zoom",
               "最大像元数": "--max-pixels", "最大分块数": "--max-blocks"}
    unknown = set(obj) - set(options) - {"范围"}
    if unknown:
        raise ValueError("未知参数：" + "、".join(sorted(unknown)))
    if "成像日期" in obj:
        value = obj["成像日期"]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("参数表的成像日期必须是具体的一天，格式为 YYYY-MM-DD。")
        date_mode(value)
    required = ["成像日期", "输出目录"]
    if "参考栅格" not in obj:
        required += ["范围", "分辨率"]
    if obj.get("来源") == "bing":
        required += ["缩放级别"]
    missing = [k for k in required if k not in obj or obj[k] in ("", None)]
    if missing:
        raise ValueError("缺少参数：" + "、".join(missing))
    args = []
    if "范围" in obj:
        bbox = obj["范围"]
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(type(v) in (float, int) for v in bbox):
            raise ValueError("范围必须是四个数值，顺序为西、南、东、北。")
        args.extend(["--bbox", *map(str, bbox)])
    for key, option in options.items():
        if key not in obj:
            continue
        value = obj[key]
        if key == "分辨率":
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("分辨率应为大于零的有限数值，单位为投影米/像元。")
        elif key in ("缩放级别", "最大像元数", "最大分块数"):
            if type(value) is not int or (key != "缩放级别" and value <= 0):
                raise ValueError(f"{key} 应为有效整数。")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} 应为非空文本。")
        if key in ("输出目录", "参考栅格") and not Path(value).is_absolute():
            raise ValueError(f"{key} 应使用本机绝对路径。")
        args.extend([option, str(value)])
    return args
