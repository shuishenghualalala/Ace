"""OpenAIProvider vision 能力降级测试。

验证纯文本模型（vision=False）收到含 image_url 的 content_parts 时，
不会把 image_url 块发给上游 LLM，从而避免 400。
"""

from __future__ import annotations

from crew.core.types import Message
from crew.providers.openai_provider import _messages_for_openai, _provider_error


def _vision_message() -> Message:
    return Message(
        role="user",
        content="",
        content_parts=[
            {"type": "text", "text": "截图里有什么？"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
            },
        ],
    )


def test_vision_true_keeps_image_url():
    msgs = _messages_for_openai([_vision_message()], vision=True)
    assert len(msgs) == 1
    assert msgs[0]["content"] == [
        {"type": "text", "text": "截图里有什么？"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
        },
    ]


def test_vision_false_filters_image_url():
    msgs = _messages_for_openai([_vision_message()], vision=False)
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, str)
    assert "截图里有什么？" in content
    assert "image_url" not in content
    assert "data:image/png;base64" not in content
    assert "当前模型不支持视觉输入" in content


def test_vision_false_image_only_gets_placeholder():
    msgs = _messages_for_openai(
        [
            Message(
                role="user",
                content="",
                content_parts=[
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                    }
                ],
            )
        ],
        vision=False,
    )
    content = msgs[0]["content"]
    assert isinstance(content, str)
    assert "图片" in content


def test_vision_false_regular_text_untouched():
    msgs = _messages_for_openai([Message.user("hello")], vision=False)
    assert msgs[0]["content"] == "hello"


def test_image_rejection_is_classified_as_unsupported_vision():
    class BadRequest(Exception):
        status_code = 400

    error = _provider_error(
        "LLM 流式调用失败",
        BadRequest("Model do not support image input. param: image_url"),
        _messages_for_openai([_vision_message()], vision=True),
    )

    assert error.category == "unsupported_capability"
    assert error.capability == "vision"
    assert error.retryable is False


def test_unrelated_bad_request_is_not_classified_as_vision_error():
    class BadRequest(Exception):
        status_code = 400

    error = _provider_error(
        "LLM 流式调用失败",
        BadRequest("temperature must be between 0 and 1"),
        _messages_for_openai([_vision_message()], vision=True),
    )

    assert error.category == "provider"
    assert error.capability is None
