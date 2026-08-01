"""resolve_session_context_window：会话绑定模型的 context_window 解析。

该 helper 供 /api/session/{id}/context 用量显示与 compactor 阈值共用，
确保两者读到「会话绑定模型」的窗口，而非全局 cfg.context_window。
"""
from unittest.mock import MagicMock

from crew.app import CrewApp
from crew.state.config import Config, ModelProfile


def _profile(mid: str, cw: int | None, *, loaded: bool = True) -> ModelProfile:
    return ModelProfile(id=mid, name=mid, model=mid, context_window=cw, loaded=loaded)


def _make_app(
    profiles: dict[str, ModelProfile],
    stored_config: dict | None,
    global_cw: int | None = 128000,
) -> CrewApp:
    """绕过 CrewApp.__init__，只注入 resolve_session_context_window 需要的依赖。"""
    app = CrewApp.__new__(CrewApp)
    cfg = Config()
    cfg.model_profiles = profiles
    cfg.active_model_id = next(iter(profiles), "default")
    cfg.context_window = global_cw
    app.config = cfg
    store = MagicMock()
    store.get_agent_config.return_value = stored_config
    app.session_store = store
    # owner_model_profiles 直接返回注入的 profiles，绕过 owner overlay 合并逻辑
    app.owner_model_profiles = lambda owner_account_id="": profiles  # noqa: E731
    return app


def test_bound_model_window_returned():
    profiles = {"m1m": _profile("m1m", 1000000), "m256": _profile("m256", 256000)}
    app = _make_app(profiles, {"model_profile_id": "m1m"})
    assert app.resolve_session_context_window("sess") == 1000000


def test_no_binding_uses_active_model_window():
    # 无绑定时 read_binding 回退到 active 模型，返回其窗口（profile 存在则不读全局）
    profiles = {"m1m": _profile("m1m", 1000000), "m256": _profile("m256", 256000)}
    app = _make_app(profiles, {}, global_cw=200000)
    assert app.resolve_session_context_window("sess") == 1000000


def test_profile_missing_window_falls_back_to_global():
    profiles = {"m": _profile("m", None)}
    app = _make_app(profiles, {"model_profile_id": "m"}, global_cw=128000)
    assert app.resolve_session_context_window("sess") == 128000


def test_empty_catalog_falls_back_to_global():
    # catalog 空 → profile None → 回退全局 cfg.context_window
    profiles = {}
    app = _make_app(profiles, {}, global_cw=200000)
    assert app.resolve_session_context_window("sess") == 200000


def test_empty_catalog_no_global_falls_back_to_default():
    profiles = {}
    app = _make_app(profiles, {}, global_cw=None)
    assert app.resolve_session_context_window("sess") == 128000
