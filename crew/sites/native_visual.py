"""Validate the v3 rich-reply contract shared with the desktop renderer."""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

_SCHEMA = json.loads(Path(__file__).with_name('rich_reply.schema.json').read_text())
_VALIDATOR = Draft7Validator(_SCHEMA)


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(f'交互描述无效：{message}')


def _date(value: Any) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def valid_parameter(p: dict, value: Any, *, submitted: bool = False) -> bool:
    kind = p['type']
    if kind in ('range', 'number'):
        if not (type(value) in (int, float) and math.isfinite(value)
                and p.get('min', 0) <= value <= p.get('max', 0)):
            return False
        step = p.get('step', 0)
        if step <= 0:
            return False
        ticks = (value - p['min']) / step
        return abs(ticks - round(ticks)) <= 1e-7 * max(1, abs(ticks))
    if kind == 'checkbox':
        return type(value) is bool and (not submitted or not p.get('required') or value)
    if kind == 'multiselect':
        return (isinstance(value, list) and all(isinstance(v, str) for v in value)
                and len(set(value)) == len(value) and all(v in p.get('options', []) for v in value)
                and (not submitted or not p.get('required') or bool(value)))
    if not isinstance(value, str) or len(value) > 1000:
        return False
    if not value:
        return not submitted or not p.get('required')
    if kind in ('select', 'radio'):
        return value in p.get('options', [])
    return kind != 'date' or _date(value)


def validate_visual(spec: Any) -> dict:
    encoded = json.dumps(spec, ensure_ascii=False, allow_nan=False)
    _require(len(encoded.encode()) <= 128_000, '内容超过 128 KB')
    # Bound recursion before invoking a recursive schema validator.
    def bounds(v, depth=0):
        _require(depth <= 32, '结构过深')
        if isinstance(v, dict):
            for child in v.values():
                bounds(child, depth + 1)
        elif isinstance(v, list):
            for child in v:
                bounds(child, depth + 1)
    bounds(spec)
    error = next(_VALIDATOR.iter_errors(spec), None)
    _require(error is None, f'字段或类型不符：{error.message[:180] if error else ""}')
    params = {p['id']: p for p in spec.get('parameters', [])}
    _require(len(params) == len(spec.get('parameters', [])), '参数 ID 重复')
    numeric = {key for key, p in params.items() if p['type'] in ('range', 'number')}
    for p in params.values():
        if p['type'] in ('range', 'number'):
            _require(all(k in p for k in ('min', 'max', 'step')) and p['min'] < p['max']
                     and 'options' not in p, '数值参数需要 min/max/step')
        else:
            _require(not any(k in p for k in ('min', 'max', 'step')), '非数值参数不接受数值边界')
        if p['type'] in ('select', 'radio', 'multiselect'):
            _require(bool(p.get('options')), '选择控件需要 options')
        else:
            _require('options' not in p, '此控件不支持 options')
        _require(valid_parameter(p, p['value']), '默认参数值无效')

    def expression(e, columns=frozenset(), depth=0):
        _require(depth <= 12, '表达式过深')
        if type(e) in (int, float):
            return
        if 'param' in e:
            _require(e['param'] in numeric, '表达式引用非数值参数')
        elif 'column' in e:
            _require(e['column'] in columns, '表达式只能引用原始数值列')
        else:
            for arg in e['args']:
                expression(arg, columns, depth + 1)

    datasets = {d['id']: d for d in spec.get('datasets', [])}
    _require(len(datasets) == len(spec.get('datasets', [])), '数据集 ID 重复')
    for d in datasets.values():
        columns = d['columns']
        _require(len({c['id'] for c in columns}) == len(columns), '列 ID 重复')
        raw = [c for c in columns if 'expression' not in c]
        numeric_columns = {c['id'] for c in raw if c['type'] == 'number'}
        for c in columns:
            if 'expression' in c:
                _require(c['type'] == 'number', '计算列必须是数值列')
                expression(c['expression'], numeric_columns)
            if c.get('cumulative'):
                _require(c['type'] == 'number', '累计列必须是数值列')
            if 'initial' in c:
                _require(c.get('cumulative') is True, 'initial 需要 cumulative')
                expression(c['initial'], numeric_columns)
        for row in d['rows']:
            _require(len(row) == len(raw), '每行只填写原始列，列数必须一致')
            for c, v in zip(raw, row):
                _require((type(v) in (int, float) if c['type'] == 'number' else
                          _date(v) if c['type'] == 'date' else isinstance(v, str)), '数据类型与列不符')
    ids, fields = set(), set()
    count = 0

    def nodes(items, depth=0):
        nonlocal count
        _require(depth <= 6, '组件嵌套超过 6 层')
        for n in items:
            count += 1
            _require(count <= 120 and n['id'] not in ids, '组件过多或 ID 重复')
            ids.add(n['id'])
            if 'children' in n:
                nodes(n['children'], depth + 1)
            if n['type'] == 'field':
                _require(n['parameter'] in params and n['parameter'] not in fields, '字段引用无效或重复')
                fields.add(n['parameter'])
            elif n['type'] == 'metric':
                expression(n['expression'])
            elif n['type'] in ('chart', 'table'):
                _require(n['dataset'] in datasets, '引用未知数据集')
                if n['type'] == 'chart':
                    d = datasets[n['dataset']]
                    cols = {c['id']: c for c in d['columns']}
                    _require(n['x'] in cols, '图表 x 列不存在')
                    _require(len({s['column'] for s in n['series']}) == len(n['series']), '图表系列重复')
                    for series in n['series']:
                        _require(series['column'] in cols and cols[series['column']]['type'] == 'number', '图表系列必须是数值列')
                    if n['chartType'] == 'scatter':
                        _require(cols[n['x']]['type'] == 'number', '散点图 x 必须为数值')
                    if n['chartType'] in ('pie', 'donut'):
                        _require(len(n['series']) == 1, '饼图仅接受一个系列')
            elif n['type'] == 'button' and n['action']['type'] == 'set':
                for key, value in n['action']['values'].items():
                    _require(key in params and valid_parameter(params[key], value), '动作参数无效')
    nodes(spec['children'])
    _require(fields == set(params), '每个参数都必须有一个 field 组件')
    return spec
