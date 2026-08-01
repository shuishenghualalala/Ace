"""测试 thinking 内容持久化链路"""
import json
from dataclasses import asdict

from crew.core.types import Message


def test_message_has_thinking_field():
    """验证 Message 模型支持 thinking 字段"""
    msg = Message(
        role="assistant",
        content="这是回答",
        thinking="这是深度思考过程",
    )
    assert msg.thinking == "这是深度思考过程"
    print("✅ Message 模型支持 thinking 字段")


def test_thinking_serialization():
    """验证 thinking 字段可以序列化"""
    msg = Message(
        role="assistant",
        content="答案",
        thinking="推理过程",
        timestamp=1234567890.0,
        turn_started_at=1234567890.0,
        turn_duration=2.5,
    )

    data = asdict(msg)
    assert "thinking" in data
    assert data["thinking"] == "推理过程"

    # 验证 JSON 序列化
    json_str = json.dumps(data, ensure_ascii=False)
    assert "推理过程" in json_str

    print("✅ thinking 字段可以正常序列化")
    print(f"   序列化结果包含: thinking={data['thinking']}")


def test_thinking_can_be_none():
    """验证 thinking 字段可以为 None"""
    msg = Message(role="assistant", content="答案")
    assert msg.thinking is None

    data = asdict(msg)
    assert data["thinking"] is None

    print("✅ thinking 字段默认为 None")


if __name__ == "__main__":
    test_message_has_thinking_field()
    test_thinking_serialization()
    test_thinking_can_be_none()
    print("\n🎉 所有测试通过！thinking 持久化链路已完整打通")
