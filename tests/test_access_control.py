"""访问控制配置解析测试。"""

from pathlib import Path


from crew.state.access_control import AccessControlConfig


def test_default_user_type_is_internal():
    ac = AccessControlConfig()
    resolved = ac.resolve_for()
    assert resolved["user_type"] == "internal"
    assert resolved["prompt_profile"] == "internal"
    assert Path(resolved["prompt_profile_path"]).as_posix().endswith(
        "config/prompts/profiles/internal.md"
    )


def test_resolve_external_user_type():
    ac = AccessControlConfig(user_type="external")
    resolved = ac.resolve_for()
    assert resolved["user_type"] == "external"
    assert resolved["prompt_profile"] == "external"


def test_resolve_explicit_user_type_overrides_config():
    ac = AccessControlConfig(user_type="internal")
    resolved = ac.resolve_for("external")
    assert resolved["user_type"] == "external"


def test_invalid_user_type_fallbacks_to_config_default():
    ac = AccessControlConfig(user_type="internal")
    resolved = ac.resolve_for("unknown")
    assert resolved["user_type"] == "internal"

    ac2 = AccessControlConfig(user_type="external")
    resolved2 = ac2.resolve_for("unknown")
    assert resolved2["user_type"] == "external"


def test_external_toolsets_and_plugins_filter():
    ac = AccessControlConfig(
        user_type="external",
        external={
            "enabled_toolsets": ["terminal", "file"],
            "disabled_plugins": ["feishu"],
        },
    )
    resolved = ac.resolve_for()
    assert resolved["enabled_toolsets"] == ["terminal", "file"]
    assert resolved["disabled_plugins"] == ["feishu"]
    assert resolved["enabled_plugins"] is None


def test_internal_star_means_all():
    ac = AccessControlConfig(
        user_type="internal",
        internal={
            "enabled_toolsets": ["*"],
            "enabled_plugins": ["*"],
            "enabled_skills": ["*"],
        },
    )
    resolved = ac.resolve_for()
    assert resolved["enabled_toolsets"] == ["*"]
    assert resolved["enabled_plugins"] == ["*"]
    assert resolved["enabled_skills"] == ["*"]


def test_empty_enabled_lists_are_preserved():
    ac = AccessControlConfig(
        user_type="external",
        external={
            "enabled_toolsets": [],
            "enabled_plugins": [],
            "enabled_skills": [],
        },
    )
    resolved = ac.resolve_for()
    assert resolved["enabled_toolsets"] == []
    assert resolved["enabled_plugins"] == []
    assert resolved["enabled_skills"] == []


def test_profile_path_points_to_existing_file():
    ac = AccessControlConfig(user_type="external")
    resolved = ac.resolve_for()
    path = Path(resolved["prompt_profile_path"])
    assert path.exists(), f"profile 文件不存在: {path}"


def test_custom_prompt_profile():
    ac = AccessControlConfig(
        user_type="external",
        external={"prompt_profile": "custom"},
    )
    resolved = ac.resolve_for()
    assert resolved["prompt_profile"] == "custom"
    assert Path(resolved["prompt_profile_path"]).as_posix().endswith(
        "config/prompts/profiles/custom.md"
    )


# --------------------------------------------------------------------------- #
# user_type_for_owner（决策⑧：本机 owner 强制 internal）
# --------------------------------------------------------------------------- #


def test_local_owner_is_forced_internal_when_internal_accounts_configured():
    """决策⑧：白名单枚举的是远程账号，本机 local 不在名单内也不得降权 external。"""
    ac = AccessControlConfig(internal_accounts=["A:uid-a"])
    assert ac.user_type_for_owner("local") == "internal"


def test_local_owner_forced_internal_overrides_requested_external():
    """'强制'优先于非严格安全模式下会话显式请求的角色兼容路径。"""
    ac = AccessControlConfig(internal_accounts=["A:uid-a"])
    assert ac.user_type_for_owner("local", requested="external") == "internal"


def test_listed_remote_account_is_internal():
    ac = AccessControlConfig(internal_accounts=["A:uid-a"])
    assert ac.user_type_for_owner("A:uid-a") == "internal"


def test_unlisted_remote_account_is_external():
    ac = AccessControlConfig(internal_accounts=["A:uid-a"])
    assert ac.user_type_for_owner("B:uid-b") == "external"


def test_without_internal_accounts_falls_back_to_config_default():
    ac = AccessControlConfig(user_type="external")
    assert ac.user_type_for_owner("A:uid-a") == "external"
