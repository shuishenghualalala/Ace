from pathlib import Path

import pytest
import yaml

from crew.state.config import load_config


@pytest.fixture(autouse=True)
def _isolate_repo_env_files(monkeypatch):
    """隔离仓库里的本地开发 .env。

    本文件验证的是「显式设置的环境变量 Key 生效」，但 load_config 会用
    load_dotenv(override=True) 加载仓库根的 config/.env，本机开发时其中
    可能存有真实 Key，会覆盖测试显式设置的环境变量。
    """
    monkeypatch.setattr("crew.state.config._load_env_files", lambda: None)


def _write_config(tmp_path: Path, *, dev_mode: bool = False) -> Path:
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
                    "dev_account": "dev:dev",
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return config_path


@pytest.mark.parametrize(
    "owner, dev_mode, key",
    [
        ("local", False, "sk-local"),
        ("dev:dev", True, "sk-dev"),
    ],
    ids=["local-owner", "dev-owner"],
)
def test_owner_can_use_explicit_environment_model_key(tmp_path, monkeypatch, owner, dev_mode, key):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_MODEL_API_KEY", key)
    config = load_config(config_path=str(_write_config(tmp_path, dev_mode=dev_mode)))

    profile = config.owner_model_profiles(owner)["default"]

    assert profile.has_key is True
    assert profile.api_key == key


def test_non_local_owner_does_not_inherit_process_model_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_MODEL_API_KEY", "sk-local")
    config = load_config(config_path=str(_write_config(tmp_path)))

    assert config.owner_model_profiles("another-owner")["default"].has_key is False
