"""Bounded declarative desktop visuals. No files, HTML, or executable expressions."""
from __future__ import annotations

import json
import math
import re
from typing import Any


def validate_visual(spec: Any) -> dict:
    def require(ok, message):
        if not ok:
            raise ValueError(f"交互描述无效：{message}")

    def obj(value, keys, required=()):
        require(isinstance(value, dict), "应为对象")
        require(set(value) <= set(keys) and set(required) <= set(value), "字段不支持或缺失")

    def label(value, maximum=200):
        require(isinstance(value, str) and 0 < len(value) <= maximum, "文本长度不符")

    def number(value):
        require(type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e12, "数值超出范围")

    def identifier(value):
        require(isinstance(value, str) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,39}", value)
                and value not in {"constructor", "prototype", "__proto__"}, "标识符无效")

    require(len(json.dumps(spec, ensure_ascii=False, allow_nan=False).encode()) <= 64_000, "内容超过 64 KB")
    obj(spec, ["title", "description", "parameters", "chart", "action"], ["title"])
    label(spec["title"])
    if "description" in spec:
        label(spec["description"], 1000)
    params = spec.get("parameters", [])
    require(isinstance(params, list) and len(params) <= 8, "参数最多 8 个")
    ids, numeric = set(), set()
    for p in params:
        obj(p, ["id", "label", "type", "value", "min", "max", "step", "unit", "options"],
            ["id", "label", "type", "value"])
        identifier(p["id"]); label(p["label"])
        require(p["id"] not in ids, "参数 ID 重复"); ids.add(p["id"])
        require(p["type"] in ["range", "number", "text", "select"], "控件不支持")
        if "unit" in p:
            label(p["unit"], 30)
        if p["type"] in ["range", "number"]:
            require(all(k in p for k in ("min", "max", "step")) and "options" not in p, "数值参数需要 min/max/step")
            for k in ("value", "min", "max", "step"):
                number(p[k])
            require(p["min"] < p["max"] and p["min"] <= p["value"] <= p["max"] and p["step"] > 0, "数值参数范围无效")
            numeric.add(p["id"])
        else:
            label(p["value"], 1000)
            require(not any(k in p for k in ("min", "max", "step")), "文本参数不支持数值边界")
            if p["type"] == "select":
                options = p.get("options")
                require(isinstance(options, list) and 1 <= len(options) <= 20, "选项数量不符")
                for option in options:
                    label(option)
                require(p["value"] in options and len(set(options)) == len(options), "选项无效")
            else:
                require("options" not in p, "文本输入不支持 options")

    def expression(e, depth=0):
        require(depth <= 12, "表达式过深")
        if type(e) in (int, float):
            number(e); return
        require(isinstance(e, dict), "表达式必须为数值或声明对象")
        if set(e) == {"param"}:
            require(isinstance(e["param"], str) and e["param"] in numeric, "引用未知或非数值参数"); return
        if set(e) == {"x"} and e["x"] is True:
            return
        obj(e, ["op", "args"], ["op", "args"])
        require(e["op"] in ["add", "sub", "mul", "div", "pow", "min", "max"], "运算不支持")
        require(isinstance(e["args"], list) and len(e["args"]) == 2, "运算需要两个参数")
        for arg in e["args"]:
            expression(arg, depth + 1)

    if "chart" in spec:
        chart = spec["chart"]
        obj(chart, ["type", "x", "xLabel", "yLabel", "series", "showTotals"], ["type", "x", "xLabel", "yLabel", "series"])
        require(chart["type"] in ("line", "bar"), "仅支持 line/bar")
        label(chart["xLabel"]); label(chart["yLabel"])
        xs = chart["x"]
        require(isinstance(xs, list) and 1 <= len(xs) <= 120, "数据点须为 1–120 个")
        for x in xs:
            number(x)
        require(all(a < b for a, b in zip(xs, xs[1:])), "x 必须递增")
        require(isinstance(chart["series"], list) and 1 <= len(chart["series"]) <= 4, "系列须为 1–4 个")
        if "showTotals" in chart:
            require(type(chart["showTotals"]) is bool, "showTotals 须为布尔值")
        series_ids = set()
        for s in chart["series"]:
            obj(s, ["id", "label", "values", "expression", "cumulative", "initial"], ["id", "label"])
            identifier(s["id"]); label(s["label"])
            require(s["id"] not in series_ids, "系列 ID 重复"); series_ids.add(s["id"])
            require(("values" in s) != ("expression" in s), "指定 values 或 expression 之一")
            if "values" in s:
                require(isinstance(s["values"], list) and len(s["values"]) == len(xs), "数据长度与 x 不符")
                for v in s["values"]:
                    number(v)
            else:
                expression(s["expression"])
            if "cumulative" in s:
                require(type(s["cumulative"]) is bool, "cumulative 须为布尔值")
            if "initial" in s:
                require(s.get("cumulative") is True, "initial 仅用于累计序列")
                expression(s["initial"])
    if "action" in spec:
        obj(spec["action"], ["label", "instruction"], ["label", "instruction"])
        label(spec["action"]["label"], 60); label(spec["action"]["instruction"], 1000)
        require(bool(params), "动作需要参数")
    require("chart" in spec or (bool(params) and "action" in spec), "需要图表或可提交表单")
    return spec
