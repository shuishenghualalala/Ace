import copy
import json
from pathlib import Path

import pytest

from crew.sites.native_visual import validate_visual

SPEC = json.loads((Path(__file__).parent / 'fixtures/native-visual.json').read_text())


def test_shared_native_visual_contract():
    assert validate_visual(SPEC) == SPEC
    for expression in ['income*x', {'param': 'missing'}, {'param': []}, {'op': 'eval', 'args': [1, 2]}, float('inf')]:
        spec = copy.deepcopy(SPEC)
        spec['chart']['series'][0]['expression'] = expression
        with pytest.raises(ValueError):
            validate_visual(spec)


@pytest.mark.parametrize('field,value', [('html', '<script>bad()</script>'), ('parameters', [{}] * 9), ('action', {'label': 'go', 'instruction': ''})])
def test_invalid_native_visual_fields(field, value):
    with pytest.raises(ValueError):
        validate_visual({**SPEC, field: value})


def test_native_visual_rejects_duplicates_and_unsorted_points():
    spec = copy.deepcopy(SPEC)
    spec['chart']['x'][1] = 1
    with pytest.raises(ValueError):
        validate_visual(spec)
    spec = copy.deepcopy(SPEC)
    spec['parameters'][1]['id'] = 'cost'
    with pytest.raises(ValueError):
        validate_visual(spec)
