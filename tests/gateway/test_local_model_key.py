from pathlib import Path

import pytest
import yaml

from crew.app import build_provider_for_profile
from crew.core.errors import CrewErrorKind, ProviderError
from crew.state.config import load_config, owner_overlay_config_path, resolve_writable_env_path
from crew.state.credentials import delete_stored_key, store_key


@pytest.fixture(autouse=True)
def _isolate_repo_env_files(monkeypatch):
    """隔离仓库里的本地开发 .env。

    本文件验证的是「显式设置的环境变量 Key 生效」，但 load_config 会用
    load_dotenv(override=True) 加载仓库根的 config/.env，本机开发时其中
    可能存有真实 Key，会覆盖测试显式设置的环境变量。
    """
    monkeypatch.setattr("crew.state.config._load_env_files", lambda: None)
    monkeypatch.delenv("CREW_GATEWAY_DEV", raising=False)


def _write_config(tmp_path: Path, *, dev_mode: bool = False, dev_account: str = "dev:dev") -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "active": "default",
                    "models": {
                        "default": {
                            "name": "Default",
                            "api_key_env": "CREW_MODEL_API_KEY",
                            "provider": "openai",
                            "base_url": "https://xxx/v1",
                            "model": "test-model",
                        }
                    },
                },
                "runtime": {"db_path": str(tmp_path / "crew.db")},
                "gateway": {
                    "host": "127.0.0.1",
                    "port": 8000,
                    "dev_mode": dev_mode,
                    "dev_account": dev_account,
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return config_path


@pytest.mark.parametrize(
    "owner, dev_mode",
    [
        ("local", False),
        ("dev:isolated-owner", True),
    ],
    ids=["local-owner", "dev-owner"],
)
async def test_owner_can_use_explicit_environment_model_key(tmp_path, monkeypatch, owner, dev_mode):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_MODEL_API_KEY", "test-environment-key")
    monkeypatch.delenv("CREW_API_KEY", raising=False)
    config = load_config(config_path=str(_write_config(
        tmp_path, dev_mode=dev_mode, dev_account="dev:isolated-owner",
    )))

    profile = config.owner_model_profiles(owner)["default"]
    provider = build_provider_for_profile(profile, owner_account_id=owner, config=config)
    try:
        assert profile.has_key is True
        assert provider._auth_headers() == {"Authorization": "Bearer test-environment-key"}
        monkeypatch.setenv("CREW_MODEL_API_KEY", "rotated-environment-key")
        assert provider._auth_headers() == {"Authorization": "Bearer rotated-environment-key"}
        monkeypatch.delenv("CREW_MODEL_API_KEY")
        assert config.owner_model_profiles(owner)["default"].has_key is False
        with pytest.raises(ProviderError) as error:
            provider._auth_headers()
        assert error.value.kind is CrewErrorKind.AUTH
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "owner, dev_mode",
    [("another-owner", False), ("dev:another-owner", True), ("dev:dev", False)],
)
def test_non_local_owner_does_not_inherit_process_model_key(tmp_path, monkeypatch, owner, dev_mode):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_MODEL_API_KEY", "sk-local")
    config = load_config(config_path=str(_write_config(tmp_path, dev_mode=dev_mode)))

    assert config.owner_model_profiles(owner)["default"].has_key is False


@pytest.mark.parametrize("owner", ["local", "dev:dev", "another-owner"])
async def test_owner_private_provider_uses_rotated_owner_only_key(tmp_path, monkeypatch, owner):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_MODEL_API_KEY", "test-shared-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-vendor-environment-key")
    config = load_config(config_path=str(_write_config(tmp_path, dev_mode=True)))

    overlay = owner_overlay_config_path(owner)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(yaml.safe_dump({"llm": {"models": {"default": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_key_env": "CREW_MODEL_API_KEY",
        "builtin": True,
    }}}}), encoding="utf-8")
    local_env = resolve_writable_env_path(owner)
    local_env.write_text("CREW_MODEL_API_KEY=test-owner-env\n", encoding="utf-8")
    store_key(owner, "default", "test-owner-store")
    store_key("different-owner", "default", "test-another-owner-store")

    profile = config.owner_model_profiles(owner)["default"]
    assert not profile.builtin
    provider = build_provider_for_profile(profile, owner_account_id=owner, config=config)
    try:
        assert provider._auth_headers() == {"Authorization": "Bearer test-owner-store"}
        delete_stored_key(owner, "default")
        assert provider._auth_headers() == {"Authorization": "Bearer test-owner-env"}
        local_env.unlink()
        assert not config.owner_model_profiles(owner)["default"].has_key
        with pytest.raises(ProviderError) as error:
            provider._auth_headers()
        assert error.value.kind is CrewErrorKind.AUTH
    finally:
        await provider.aclose()
