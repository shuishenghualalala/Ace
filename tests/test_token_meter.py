"""ADR-0042 D6：TokenMeter per-session 增量计量测试。"""

from __future__ import annotations

from crew.agent.compact import estimate_tokens
from crew.agent.compact.meter import TokenMeter
from crew.agent.compact.pipeline import ContextCompactor
from crew.core.types import Message
from crew.state.session_store import SQLiteSessionStore
from tests.test_compact import FakeProvider


def _long_history(n: int) -> list[Message]:
    return [Message.user(f"消息{i}" + "x" * 200) for i in range(n)]


class TestThreeValueSemantics:
    def test_empty_view_is_none(self):
        meter = TokenMeter()
        m = meter.measure("s1", "owner", [])
        assert m.kind == "none"
        assert m.tokens == 0

    def test_no_anchor_is_estimated(self):
        meter = TokenMeter()
        messages = _long_history(5)
        m = meter.measure("s1", "owner", messages)
        assert m.kind == "estimated"
        assert m.tokens == estimate_tokens(messages)

    def test_anchor_is_exact_and_matches_usage(self):
        meter = TokenMeter()
        messages = _long_history(5)
        view_estimate = estimate_tokens(messages)
        meter.record_usage(
            "s1",
            "owner",
            prompt_tokens=4321,
            source="provider",
            fingerprint="fp-1",
            view_estimate=view_estimate,
        )
        m = meter.measure("s1", "owner", messages)
        assert m.kind == "exact"
        assert m.tokens == 4321  # 锚点视图无增量：与真实 usage 一致

    def test_delta_accumulates_after_anchor(self):
        meter = TokenMeter()
        messages = _long_history(5)
        meter.record_usage(
            "s1",
            "owner",
            prompt_tokens=1000,
            source="provider",
            fingerprint="fp-1",
            view_estimate=estimate_tokens(messages),
        )
        grown = messages + _long_history(3)
        m = meter.measure("s1", "owner", grown)
        assert m.kind == "exact"
        assert m.tokens == 1000 + estimate_tokens(grown) - estimate_tokens(messages)

    def test_delta_clamped_at_zero_after_shrink(self):
        """压缩使视图收缩时计量不回落（max(0) 钳制），等下一次真实 usage 重锚定。"""
        meter = TokenMeter()
        grown = _long_history(10)
        meter.record_usage(
            "s1",
            "owner",
            prompt_tokens=9000,
            source="provider",
            fingerprint="fp-1",
            view_estimate=estimate_tokens(grown),
        )
        shrunk = _long_history(2)
        m = meter.measure("s1", "owner", shrunk)
        assert m.kind == "exact"
        assert m.tokens == 9000

    def test_suspect_usage_rebases_anchor(self):
        """信封一致的低 usage（视图被压缩）仍成为新锚点，仅标记链断裂。"""
        meter = TokenMeter()
        grown = _long_history(10)
        continuous = meter.record_usage(
            "s1",
            "owner",
            prompt_tokens=9000,
            source="provider",
            fingerprint="fp-1",
            view_estimate=estimate_tokens(grown),
        )
        assert continuous is False  # 首个锚点，无旧链
        shrunk = _long_history(2)
        continuous = meter.record_usage(
            "s1",
            "owner",
            prompt_tokens=1500,
            source="provider",
            fingerprint="fp-1",
            view_estimate=estimate_tokens(shrunk),
        )
        assert continuous is False  # usage < 旧锚点估价：链断裂、重锚定
        m = meter.measure("s1", "owner", shrunk)
        assert m.kind == "exact"
        assert m.tokens == 1500

    def test_growing_usage_with_same_envelope_is_continuous(self):
        meter = TokenMeter()
        messages = _long_history(5)
        meter.record_usage(
            "s1",
            "owner",
            prompt_tokens=1000,
            source="provider",
            fingerprint="fp-1",
            view_estimate=estimate_tokens(messages),
        )
        grown = messages + _long_history(2)
        continuous = meter.record_usage(
            "s1",
            "owner",
            prompt_tokens=1000 + (estimate_tokens(grown) - estimate_tokens(messages)),
            source="provider",
            fingerprint="fp-1",
            view_estimate=estimate_tokens(grown),
        )
        assert continuous is True


class TestCheckpointRehydration:
    def test_loader_reanchors_after_restart(self):
        captured: dict = {}

        def loader(session_id: str, owner: str):
            captured["args"] = (session_id, owner)
            return {
                "prompt_tokens": 7777,
                "source": "provider",
                "fingerprint": "fp-1",
                "baseline_estimate": 8000,
                "recorded_at": 1.0,
            }

        meter = TokenMeter(loader)
        grown = _long_history(8)
        m = meter.measure("s1", "owner", grown)
        assert m.kind == "exact"
        assert m.tokens == 7777 + max(0, estimate_tokens(grown) - 8000)
        assert captured["args"] == ("s1", "owner")

    def test_store_checkpoint_roundtrip(self, tmp_path):
        db = str(tmp_path / "crew.db")
        store = SQLiteSessionStore(db)
        try:
            store.save("s1", [Message.user("q")], owner_account_id="A:uid-a")
            store.record_meter_checkpoint(
                "s1",
                owner_account_id="A:uid-a",
                prompt_tokens=5555,
                source="provider",
                fingerprint="OpenAIProvider:gpt-x",
                baseline_estimate=6000,
            )
            payload = store.load_meter_checkpoint("s1", "A:uid-a")
            assert payload is not None
            assert payload["prompt_tokens"] == 5555
            assert payload["fingerprint"] == "OpenAIProvider:gpt-x"
            assert payload["baseline_estimate"] == 6000
        finally:
            store.close()

    def test_meter_reanchors_from_store_after_restart(self, tmp_path):
        db = str(tmp_path / "crew.db")
        store = SQLiteSessionStore(db)
        try:
            history = [Message.user("q"), Message.assistant("a")]
            store.save("s1", history, owner_account_id="A:uid-a")
            store.record_meter_checkpoint(
                "s1",
                owner_account_id="A:uid-a",
                prompt_tokens=2048,
                source="provider",
                fingerprint="fp",
                baseline_estimate=estimate_tokens(history),
            )
        finally:
            store.close()

        store2 = SQLiteSessionStore(db)
        try:
            meter = TokenMeter(store2.load_meter_checkpoint)
            history = store2.load("s1", owner_account_id="A:uid-a")
            m = meter.measure("s1", "A:uid-a", history)
            assert m.kind == "exact"
            assert m.tokens == 2048
        finally:
            store2.close()


class TestPipelineConsumption:
    async def test_water_level_without_anchor_matches_estimate(self):
        """无锚点时水位判断与旧 estimate_tokens 行为完全一致（不触发摘要）。"""
        provider = FakeProvider()
        compactor = ContextCompactor(provider, token_budget=10_000_000)
        messages = _long_history(5)
        result = await compactor.maybe_compact(messages, session_id="s1", owner_account_id="o")
        assert result == messages
        assert provider.calls == []

    async def test_water_level_with_anchor_usage_skips_summary(self):
        """锚点真实 usage 远低于预算时 exact 计量直接放行，零 LLM。"""
        provider = FakeProvider()
        compactor = ContextCompactor(
            provider, token_budget=10_000_000, meter_checkpoint_loader=lambda sid, o: None
        )
        messages = _long_history(50)
        compactor.record_meter_usage(
            "s1",
            "o",
            prompt_tokens=100,
            source="provider",
            fingerprint="fp",
            view_estimate=estimate_tokens(messages),
        )
        result = await compactor.maybe_compact(messages, session_id="s1", owner_account_id="o")
        assert provider.calls == []
        assert [m.content for m in result] == [m.content for m in messages]

    async def test_water_level_with_anchor_usage_triggers_summary(self):
        """锚点真实 usage 超预算时按 exact 计量触发 L3 摘要。"""
        provider = FakeProvider()
        compactor = ContextCompactor(
            provider,
            token_budget=500,
            keep_recent=2,
            meter_checkpoint_loader=lambda sid, o: None,
        )
        messages = _long_history(30)
        compactor.record_meter_usage(
            "s1",
            "o",
            prompt_tokens=60_000,
            source="provider",
            fingerprint="fp",
            view_estimate=estimate_tokens(messages),
        )
        result = await compactor.maybe_compact(messages, session_id="s1", owner_account_id="o")
        assert provider.calls, "锚点 usage 超预算应触发摘要"
        assert len(result) < len(messages)
