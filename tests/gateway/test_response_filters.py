"""测试响应过滤器。"""


from crew.gateway.response_filters import (
    ResponseFilterChain,
    normalize_line_breaks,
    strip_thinking_tags,
    truncate_long_response,
)


def test_response_filter_chain_single():
    """测试单个过滤器。"""
    chain = ResponseFilterChain()

    def uppercase_filter(text: str, context: dict) -> str:
        return text.upper()

    chain.register("uppercase", uppercase_filter)

    result = chain.apply("hello world", {})
    assert result == "HELLO WORLD"


def test_response_filter_chain_multiple():
    """测试多个过滤器链式执行。"""
    chain = ResponseFilterChain()

    def add_prefix(text: str, context: dict) -> str:
        return f"[PREFIX] {text}"

    def add_suffix(text: str, context: dict) -> str:
        return f"{text} [SUFFIX]"

    chain.register("prefix", add_prefix)
    chain.register("suffix", add_suffix)

    result = chain.apply("test", {})
    assert result == "[PREFIX] test [SUFFIX]"


def test_response_filter_chain_unregister():
    """测试移除过滤器。"""
    chain = ResponseFilterChain()

    def test_filter(text: str, context: dict) -> str:
        return text.upper()

    chain.register("test", test_filter)
    result = chain.apply("hello", {})
    assert result == "HELLO"

    chain.unregister("test")
    result = chain.apply("hello", {})
    assert result == "hello"  # 未转换


def test_strip_thinking_tags():
    """测试移除 <thinking> 标签。"""
    text = """Here is my answer.
<thinking>
This is internal reasoning that should be hidden.
</thinking>
The final answer is 42."""

    result = strip_thinking_tags(text, {"channel": "feishu"})
    assert "<thinking>" not in result
    assert "internal reasoning" not in result
    assert "Here is my answer." in result
    assert "The final answer is 42." in result


def test_strip_thinking_tags_multiple():
    """测试移除多个 <thinking> 块。"""
    text = """First part.
<thinking>block1</thinking>
Middle part.
<THINKING>block2</THINKING>
Last part."""

    result = strip_thinking_tags(text, {"channel": "feishu"})
    assert "<thinking>" not in result.lower()
    assert "block1" not in result
    assert "block2" not in result
    assert "First part." in result
    assert "Middle part." in result
    assert "Last part." in result


def test_truncate_long_response():
    """测试截断超长响应。"""
    text = "x" * 5000
    result = truncate_long_response(text, {"max_length": 100})

    assert len(result) <= 100
    assert "[响应过长，已截断]" in result


def test_truncate_long_response_short_text():
    """测试短文本不截断。"""
    text = "short text"
    result = truncate_long_response(text, {"max_length": 100})
    assert result == text


def test_normalize_line_breaks():
    """测试规范化换行。"""
    text = "paragraph1\n\n\n\n\nparagraph2"
    result = normalize_line_breaks(text, {})
    assert result == "paragraph1\n\nparagraph2"


def test_normalize_line_breaks_preserves_double():
    """测试保留双换行。"""
    text = "paragraph1\n\nparagraph2"
    result = normalize_line_breaks(text, {})
    assert result == text


def test_strip_thinking_tags_gated_by_channel():
    """strip_thinking_tags 仅对 IM 文本渠道剥离；桌面/Web/MCP 保留思考过程。"""
    text = "before <thinking>secret reasoning</thinking> after"

    # IM 渠道：剥离内联 thinking
    for im in ("feishu", "dingtalk", "wecom"):
        result = strip_thinking_tags(text, {"channel": im})
        assert "secret reasoning" not in result
        assert "before" in result and "after" in result

    # 桌面/Web/MCP/无 channel：原样返回（富 UI 单独渲染思考块）
    for keep in ("web", "desktop", "mcp", ""):
        assert strip_thinking_tags(text, {"channel": keep}) == text
    assert strip_thinking_tags(text, {}) == text


def test_apply_text_filters_strips_thinking_only_for_im():
    """全局过滤链对 IM 渠道剥离 thinking、对 Web 不剥离；密钥过滤对所有渠道生效。"""
    from crew.gateway.response_filters import apply_text_filters

    text = "<thinking>r</thinking> sk-abcdefgh1234"
    im_out = apply_text_filters(text, {"channel": "feishu"})
    web_out = apply_text_filters(text, {"channel": "web"})
    # 两端都脱敏密钥
    assert "sk-abcdefgh1234" not in im_out
    assert "sk-abcdefgh1234" not in web_out
    # 仅 IM 剥离 thinking
    assert "<thinking>" not in im_out
    assert "<thinking>" in web_out


def test_redact_secrets_failure_returns_safe_placeholder():
    """redact_secrets 自身异常时，链必须返回安全占位，绝不能把含密钥的原文泄露出去。"""
    from crew.gateway.response_filters import (
        REDACT_FAILURE_PLACEHOLDER,
        ResponseFilterChain,
        strip_thinking_tags,
    )

    def boom(text, context):
        raise RuntimeError("redact impl bug")

    # 用独立的新链，避免污染全局 response_filter_chain 单例（不走 monkeypatch 全局属性）
    chain = ResponseFilterChain()
    chain._filters = [("redact_secrets", boom), ("strip_thinking_tags", strip_thinking_tags)]

    out = chain.apply("password=hunter2hunter2", {})
    assert out == REDACT_FAILURE_PLACEHOLDER
    assert "hunter2" not in out


def test_non_safety_filter_failure_keeps_current():
    """非安全关键过滤器失败仍保持「继续用当前文本」的宽容行为，且不中断后续过滤器。"""
    chain = ResponseFilterChain()

    def failing(text, context):
        raise ValueError("boom")

    def shout(text, context):
        return text.upper()

    chain.register("non_critical", failing)
    chain.register("shout", shout)
    # failing 失败后继续用 current（原文本），后续过滤器仍执行
    assert chain.apply("hello world", {}) == "HELLO WORLD"
