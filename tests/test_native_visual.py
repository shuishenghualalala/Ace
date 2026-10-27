import copy
import json
from pathlib import Path

import pytest

from crew.sites.native_visual import validate_visual

FIXTURES = Path(__file__).parent / 'fixtures'
SPEC = json.loads((FIXTURES / 'native-visual.json').read_text())
INVALID = json.loads((FIXTURES / 'native-visual-invalid.json').read_text())


def test_shared_native_visual_contract():
    assert validate_visual(SPEC) == SPEC


@pytest.mark.parametrize('case', INVALID, ids=lambda case: case['name'])
def test_shared_invalid_native_visual_contract(case):
    spec = copy.deepcopy(SPEC)
    target = spec
    for key in case['path'][:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    key = case['path'][-1]
    if isinstance(target, list):
        target[int(key)] = case['value']
    else:
        target[key] = case['value']
    with pytest.raises(ValueError):
        validate_visual(spec)


@pytest.mark.parametrize('expression', [{'param': []}, float('inf')])
def test_native_visual_rejects_non_json_expressions(expression):
    spec = copy.deepcopy(SPEC)
    spec['chart']['series'][0]['expression'] = expression
    with pytest.raises(ValueError):
        validate_visual(spec)


@pytest.mark.parametrize('field,value', [('parameters', [{}] * 9), ('action', {'label': 'go', 'instruction': ''})])
def test_invalid_native_visual_fields(field, value):
    with pytest.raises(ValueError):
        validate_visual({**SPEC, field: value})
