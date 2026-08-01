"""provider usage 解析：确认 cache 字段被提取（Anthropic prompt caching）。"""

from crew.providers.anthropic_provider import _parse_response


def test_anthropic_parse_response_extracts_cache_tokens():
    data = {
        "content": [{"type": "text", "text": "hi"}],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 80,
        },
        "stop_reason": "end_turn",
    }
    resp = _parse_response(data)
    assert resp.usage["prompt_tokens"] == 100
    assert resp.usage["completion_tokens"] == 50
    assert resp.usage["cache_creation_input_tokens"] == 200
    assert resp.usage["cache_read_input_tokens"] == 80


def test_anthropic_parse_response_missing_cache_defaults_zero():
    data = {"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 10, "output_tokens": 5}}
    resp = _parse_response(data)
    assert resp.usage["cache_creation_input_tokens"] == 0
    assert resp.usage["cache_read_input_tokens"] == 0


def test_anthropic_parse_response_no_usage_block():
    resp = _parse_response({"content": [{"type": "text", "text": "hi"}]})
    assert resp.usage["prompt_tokens"] == 0
    assert resp.usage["cache_read_input_tokens"] == 0
