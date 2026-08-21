"""Crew Home 目录初始化与上下文文件加载测试。"""

import os
import sys
from pathlib import Path

import pytest

from crew.state import config as config_module
from crew.state import home as home_module
from crew.state.home import (
    agent_workspace_path,
    ensure_crew_home,
    external_session_workspace_path,
    export_crew_runtime_env,
    get_crew_home,
    get_owner_runtime_home,
    get_task_workspace_root,
    load_memory_md,
    load_soul_md,
    load_user_md,
    owner_path_segment,
    managed_runtime_env_overrides,
    managed_runtime_read_roots,
    refresh_owner_runtime_env,
    runtime_env_overrides,
    task_workspace_path,
)
from crew.state.config import load_config, resolve_writable_env_path


@pytest.fixture
def crew_home_dir(tmp_path, monkeypatch):
    """将 CREW_HOME 指向临时目录，避免污染真实项目。"""
    home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))
    return home


def test_get_crew_home_default():
    """默认情况下，crew_home 在项目根目录下 DEFAULT_HOME_DIRNAME/。"""
    # 清除环境变量，让它走默认路径
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv("CREW_HOME", raising=False)
    home = get_crew_home()
    from crew.state.home import DEFAULT_HOME_DIRNAME

    assert home.name == DEFAULT_HOME_DIRNAME
    # 默认目录跟随实际 checkout 根目录，不依赖仓库文件夹名称。
    assert home.parent == Path(__file__).resolve().parents[1]
    monkeypatch.undo()


def test_get_crew_home_env_override(crew_home_dir):
    """CREW_HOME 环境变量可以覆盖默认路径。"""
    result = get_crew_home()
    assert result == crew_home_dir


def test_ensure_crew_home_creates_structure(crew_home_dir):
    """ensure_crew_home 应创建完整目录结构和默认文件。"""
    home = ensure_crew_home()

    # 目录存在
    assert home.is_dir()
    assert (home / "memories").is_dir()
    assert (home / "agents" / "default").is_dir()
    assert (home / "teams").is_dir()
    assert not (home / "task_workspaces").exists()

    # 默认文件存在
    assert (home / "SOUL.md").is_file()
    assert (home / "memories" / "MEMORY.md").is_file()
    assert (home / "memories" / "USER.md").is_file()

    # SOUL.md 有内容
    soul_content = (home / "SOUL.md").read_text(encoding="utf-8")
    assert "Crew" in soul_content

    # USER.md 有模板内容
    user_content = (home / "memories" / "USER.md").read_text(encoding="utf-8")
    assert "姓名" in user_content


def test_ensure_crew_home_idempotent(crew_home_dir):
    """多次调用 ensure_crew_home 不会覆盖已有文件。"""
    ensure_crew_home()

    # 修改 SOUL.md
    soul_path = crew_home_dir / "SOUL.md"
    custom_text = "这是自定义的 Agent 身份"
    soul_path.write_text(custom_text, encoding="utf-8")

    # 再次调用
    ensure_crew_home()

    # SOUL.md 内容不变（未被覆盖）
    assert soul_path.read_text(encoding="utf-8") == custom_text


def test_load_soul_md_exists(crew_home_dir):
    """SOUL.md 存在时加载内容。"""
    ensure_crew_home()
    content = load_soul_md()
    assert content is not None
    assert "Crew" in content


@pytest.mark.parametrize(
    ("loader", "expect_none"),
    [
        pytest.param(load_soul_md, True, id="soul"),
        pytest.param(load_memory_md, False, id="memory"),
        pytest.param(load_user_md, False, id="user"),
    ],
)
def test_load_md_missing(tmp_path, monkeypatch, loader, expect_none):
    """SOUL.md/MEMORY.md/USER.md 不存在时按各自约定返回 None 或空串。"""
    empty_home = tmp_path / "empty_crew"
    empty_home.mkdir()
    monkeypatch.setenv("CREW_HOME", str(empty_home))
    result = loader()
    if expect_none:
        assert result is None
    else:
        assert result == ""


def test_load_soul_md_empty(crew_home_dir):
    """SOUL.md 为空文件时返回 None。"""
    ensure_crew_home()
    (crew_home_dir / "SOUL.md").write_text("", encoding="utf-8")
    assert load_soul_md() is None


def test_load_memory_md_exists(crew_home_dir):
    """MEMORY.md 存在且有内容时返回内容。"""
    ensure_crew_home()
    (crew_home_dir / "memories" / "MEMORY.md").write_text("用户偏好: 中文\n", encoding="utf-8")
    content = load_memory_md()
    assert "中文" in content


def test_load_memory_md_empty(crew_home_dir):
    """MEMORY.md 为空时返回空串。"""
    ensure_crew_home()
    # 默认创建的 MEMORY.md 是空的
    content = load_memory_md()
    assert content == ""


def test_load_user_md_exists(crew_home_dir):
    """USER.md 存在且有内容时返回内容。"""
    ensure_crew_home()
    content = load_user_md()
    assert "姓名" in content


def test_task_workspace_defaults_under_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    crew_home = tmp_path / "project-state"
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    root = get_task_workspace_root()
    # task_workspaces 默认跟随 get_crew_home()，而非硬编码 ~/.crew
    assert root == crew_home / "task_workspaces"

    # Builtin Layer 3 remains workspace-scoped; external sessions may opt into
    # the isolated helper below without changing this existing path.
    task_dir = task_workspace_path("space one")
    assert task_dir == root / "space_one"
    assert task_dir.is_dir()

    # agent_workspace_path 是预留给将来多 agent 的子目录（不是主 work_dir）
    agent_dir = agent_workspace_path("space one", "coder#1")
    assert agent_dir == task_dir / "agents" / "coder_1"
    assert agent_dir.is_dir()

    external_dir = external_session_workspace_path(
        "space one",
        "session::1",
        "agent#1",
    )
    assert external_dir == task_dir / "external_sessions" / "session_1" / "agent_1"
    assert external_dir.is_dir()


def test_task_workspace_root_env_override(tmp_path, monkeypatch):
    root = tmp_path / "crew-task-output"
    monkeypatch.setenv("CREW_TASK_WORKSPACE_ROOT", str(root))
    assert get_task_workspace_root() == root
    assert task_workspace_path("ws-a") == root / "ws-a"
    assert agent_workspace_path("ws-a", "main") == root / "ws-a" / "agents" / "main"


def test_owner_runtime_home_and_workspace_are_owner_scoped(tmp_path, monkeypatch):
    home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))

    owner_a_segment = owner_path_segment("owner:user-a")
    owner_b_segment = owner_path_segment("owner:user-b")
    owner_home = get_owner_runtime_home("owner:user-a")
    assert owner_home == home / "accounts" / owner_a_segment

    ws_a = task_workspace_path("default", owner_account_id="owner:user-a")
    ws_b = task_workspace_path("default", owner_account_id="owner:user-b")
    assert ws_a == owner_home / "task_workspaces" / "default"
    assert ws_b == home / "accounts" / owner_b_segment / "task_workspaces" / "default"
    assert ws_a != ws_b


def test_owner_runtime_env_file_is_owner_scoped(tmp_path, monkeypatch):
    home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))

    owner_home = home / "accounts" / owner_path_segment("owner:user-a")
    assert resolve_writable_env_path("owner:user-a") == owner_home / ".env"


def test_owner_path_segment_is_short_and_private():
    owner = "user_123_example.org"

    segment = owner_path_segment(owner)

    assert segment.startswith("acct_")
    assert len(segment) <= 21
    assert "E1000114386" not in segment
    assert "zhangxinglong" not in segment


def test_owner_path_segment_rejects_path_like_owner_id():
    with pytest.raises(ValueError, match="owner_account_id"):
        owner_path_segment(r"C:\Users\someone\.Crew\accounts\dev_dev")


def test_load_config_resolves_task_workspace_root_relative_to_crew_home(tmp_path, monkeypatch):
    home = tmp_path / ".crew-custom"
    monkeypatch.setenv("CREW_HOME", str(home))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "runtime:\n  task_workspace_root: outputs\n  db_path: crew_data/crew.db\n",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)

    assert cfg.task_workspace_root == str(home / "outputs")
    assert get_task_workspace_root() == home / "outputs"


def test_load_config_defaults_task_workspace_under_user_home(tmp_path, monkeypatch):
    home = tmp_path / "project-state"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CREW_HOME", str(home))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    cfg_path = tmp_path / "config-default.yaml"
    cfg_path.write_text("runtime:\n  db_path: crew_data/crew.db\n", encoding="utf-8")

    cfg = load_config(cfg_path)

    # task_workspaces 默认跟随 get_crew_home()，而非硬编码 ~/.crew
    expected = home / "task_workspaces"
    assert cfg.task_workspace_root == str(expected)
    assert get_task_workspace_root() == expected


def test_export_crew_runtime_env_uses_crew_home_env_file(tmp_path, monkeypatch):
    home = tmp_path / "project" / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))
    monkeypatch.delenv("DOTENV_CONFIG_PATH", raising=False)

    values = export_crew_runtime_env()

    assert os.environ["CREW_ENV_FILE"] == str(home / ".env")
    assert values["CREW_ENV_FILE"] == str(home / ".env")
    assert values["DOTENV_CONFIG_PATH"] == str(home / ".env")


def test_export_crew_runtime_env_owner_scope(tmp_path, monkeypatch):
    home = tmp_path / "project" / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))

    values = export_crew_runtime_env(owner_account_id="owner:user-a")

    owner_home = home / "accounts" / owner_path_segment("owner:user-a")
    assert values["CREW_HOME"] == str(home)
    assert values["CREW_OWNER_HOME"] == str(owner_home)
    assert values["CREW_RUNTIME_HOME"] == str(owner_home)
    assert values["CREW_SKILLS_DIR"] == str(owner_home / "skills")
    assert values["CREW_ENV_FILE"] == str(owner_home / ".env")
    assert os.environ["CREW_HOME"] == str(home)


def test_runtime_env_overrides_sets_python_utf8_io(tmp_path, monkeypatch):
    home = tmp_path / "project" / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))

    values = runtime_env_overrides(owner_account_id="owner:user-a")

    assert values["PYTHONIOENCODING"] == "utf-8"
    if sys.platform == "win32":
        assert values["PYTHONUTF8"] == "1"
    else:
        assert "PYTHONUTF8" not in values


def test_managed_runtime_keeps_system_commands_and_development_runtime_visible(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "crew-home"))
    node_bin = tmp_path / "node-prefix" / "bin"
    node_bin.mkdir(parents=True)
    node_root = node_bin.parent
    py_bin = tmp_path / "venv" / "bin"
    py_bin.mkdir(parents=True)
    py_root = py_bin.parent
    monkeypatch.setattr(home_module, "_bundled_runtime_paths", lambda: [])
    monkeypatch.setattr(
        home_module,
        "_development_runtime_toolchain",
        lambda: ([str(py_bin), str(node_bin)], (py_root, node_root)),
    )
    monkeypatch.setattr(home_module, "_managed_system_path_dirs", lambda: ["/usr/bin", "/bin"])

    values = managed_runtime_env_overrides(owner_account_id="owner:user-a")

    assert values["PATH"].split(os.pathsep) == [
        str(py_bin),
        str(node_bin),
        "/usr/bin",
        "/bin",
    ]
    assert managed_runtime_read_roots() == (
        py_root.resolve(),
        node_root.resolve(),
    )


def test_development_python_toolchain_exposes_venv_root(tmp_path, monkeypatch):
    """开发态 venv 解释器：venv 根目录可读，模拟打包态 runtimes/python。"""
    venv_root = tmp_path / ".venv"
    venv_bin = venv_root / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").write_text("", encoding="utf-8")
    (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    monkeypatch.setattr(home_module.sys, "frozen", False, raising=False)
    monkeypatch.setattr(
        home_module.sys, "executable", str(venv_bin / "python3")
    )

    path_dirs, roots = home_module._development_python_toolchain()

    assert path_dirs == [str(venv_bin.resolve())]
    # venv 根目录必须放行（含 site-packages）；真实解释器根若与 venv 同根则去重后唯一。
    assert venv_root.resolve() in roots


def test_development_python_toolchain_falls_back_to_bin_for_system_python(
    tmp_path, monkeypatch
):
    """非 venv 的系统 python：只放行 bin 目录，不放大到 /usr。"""
    usr_bin = tmp_path / "usr" / "bin"
    usr_bin.mkdir(parents=True)
    (usr_bin / "python3").write_text("", encoding="utf-8")
    monkeypatch.setattr(home_module.sys, "frozen", False, raising=False)
    monkeypatch.setattr(home_module.sys, "executable", str(usr_bin / "python3"))

    path_dirs, roots = home_module._development_python_toolchain()

    assert path_dirs == [str(usr_bin.resolve())]
    assert roots == (usr_bin.resolve(),)
    assert roots != (tmp_path / "usr").resolve()


def test_development_python_toolchain_exposes_symlinked_interpreter_root(
    tmp_path, monkeypatch
):
    """uv/pyenv 场景：.venv/bin/python 是指向全局解释器的符号链接，
    venv 根与真实解释器根（含 lib/ 动态库）两个目录都要放行。"""
    # 真实解释器树：uv python，bin/python3 + lib/
    real_root = tmp_path / "uv-python"
    real_bin = real_root / "bin"
    real_lib = real_root / "lib"
    real_bin.mkdir(parents=True)
    real_lib.mkdir(parents=True)
    (real_bin / "python3.11").write_text("", encoding="utf-8")
    # venv 树：.venv/bin/python3 -> 真实解释器，含 pyvenv.cfg
    venv_root = tmp_path / ".venv"
    venv_bin = venv_root / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").symlink_to(real_bin / "python3.11")
    (venv_root / "pyvenv.cfg").write_text("home = /uv-python/bin\n", encoding="utf-8")
    monkeypatch.setattr(home_module.sys, "frozen", False, raising=False)
    monkeypatch.setattr(
        home_module.sys, "executable", str(venv_bin / "python3")
    )

    path_dirs, roots = home_module._development_python_toolchain()

    assert path_dirs == [str(venv_bin.resolve())]
    assert venv_root.resolve() in roots
    assert real_root.resolve() in roots


def test_development_runtime_toolchain_merges_python_and_node(tmp_path, monkeypatch):
    """合并 toolchain 把 python 与 node 的 PATH/readable_roots 保序去重合并。"""
    venv_root = tmp_path / ".venv"
    venv_bin = venv_root / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").write_text("", encoding="utf-8")
    (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    monkeypatch.setattr(home_module.sys, "frozen", False, raising=False)
    monkeypatch.setattr(
        home_module.sys, "executable", str(venv_bin / "python3")
    )
    node_bin = tmp_path / "node-prefix" / "bin"
    node_bin.mkdir(parents=True)
    (node_bin / "node").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        home_module.shutil,
        "which",
        lambda name: str(node_bin / name) if name == "node" else None,
    )

    path_dirs, roots = home_module._development_runtime_toolchain()

    assert path_dirs == [str(venv_bin.resolve()), str(node_bin.resolve())]
    assert venv_root.resolve() in roots
    assert node_bin.resolve() in roots


def test_development_node_toolchain_does_not_expose_complete_install_prefix(
    tmp_path, monkeypatch
):
    prefix = tmp_path / "toolchain"
    bin_dir = prefix / "bin"
    node_root = prefix / "versions" / "node-v22"
    node_bin = node_root / "bin"
    npm_root = prefix / "packages" / "npm"
    npm_bin = npm_root / "bin"
    bin_dir.mkdir(parents=True)
    node_bin.mkdir(parents=True)
    npm_bin.mkdir(parents=True)
    (node_bin / "node").write_text("", encoding="utf-8")
    (npm_bin / "npm-cli.js").write_text("", encoding="utf-8")
    (npm_bin / "npx-cli.js").write_text("", encoding="utf-8")
    (bin_dir / "node").symlink_to(node_bin / "node")
    (bin_dir / "npm").symlink_to(npm_bin / "npm-cli.js")
    (bin_dir / "npx").symlink_to(npm_bin / "npx-cli.js")
    monkeypatch.setattr(
        home_module.shutil,
        "which",
        lambda name: str(bin_dir / name),
    )
    monkeypatch.setattr(home_module.sys, "frozen", False, raising=False)

    path_dirs, roots = home_module._development_node_toolchain()

    assert path_dirs == [str(bin_dir.resolve())]
    assert bin_dir.resolve() in roots
    assert node_root.resolve() in roots
    assert npm_root.resolve() in roots
    assert prefix.resolve() not in roots


def test_packaged_node_npm_runtime_stays_first_on_managed_path(tmp_path, monkeypatch):
    gateway = tmp_path / "crew-gateway"
    runtimes = gateway / "_internal" / "runtimes"
    python_bin = runtimes / "python" / "bin"
    node_bin = runtimes / "node" / "bin"
    python_bin.mkdir(parents=True)
    node_bin.mkdir(parents=True)
    (python_bin / "python3").write_text("", encoding="utf-8")
    (node_bin / "node").write_text("", encoding="utf-8")
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(home_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(home_module.sys, "executable", str(gateway / "crew-gateway"))
    monkeypatch.setattr(home_module, "_managed_system_path_dirs", lambda: ["/usr/bin", "/bin"])

    values = managed_runtime_env_overrides(owner_account_id="owner:user-a")

    assert values["PATH"].split(os.pathsep) == [
        str(python_bin),
        str(node_bin),
        "/usr/bin",
        "/bin",
    ]
    assert managed_runtime_read_roots() == (
        (runtimes / "python").resolve(),
        (runtimes / "node").resolve(),
    )


def test_owner_runtime_env_does_not_nest_accounts_on_reload(tmp_path, monkeypatch):
    home = tmp_path / "project" / ".crew"
    owner = "dev:dev"
    monkeypatch.setenv("CREW_HOME", str(home))

    values = runtime_env_overrides(owner_account_id=owner)
    monkeypatch.setenv("CREW_HOME", values["CREW_HOME"])

    owner_home = get_owner_runtime_home(owner, create=False)
    segment = owner_path_segment(owner)

    assert owner_home == home / "accounts" / segment
    assert f"accounts{os.sep}{segment}{os.sep}accounts{os.sep}{segment}" not in str(owner_home)


def test_export_crew_runtime_env_clears_stale_owner_account_id(tmp_path, monkeypatch):
    home = tmp_path / "project" / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))

    export_crew_runtime_env(owner_account_id="owner:user-a")
    assert os.environ["CREW_OWNER_ACCOUNT_ID"] == "owner:user-a"

    export_crew_runtime_env()

    assert "CREW_OWNER_ACCOUNT_ID" not in os.environ


def test_load_config_loads_env_from_configured_crew_home(tmp_path, monkeypatch):
    home = tmp_path / "custom-crew"
    home.mkdir()
    (home / ".env").write_text(
        "CREW_TEST_HOME_ENV_KEY=sk-from-crew-home\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "runtime:\n"
        f"  crew_home: {home}\n"
        "  db_path: crew_data/crew.db\n"
        "llm:\n"
        "  models:\n"
        "    test:\n"
        "      api_key_env: CREW_TEST_HOME_ENV_KEY\n"
        "      model: fake-model\n"
        "  active: test\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CREW_HOME", raising=False)
    monkeypatch.delenv("CREW_TEST_HOME_ENV_KEY", raising=False)

    cfg = load_config(cfg_path)

    assert os.environ["CREW_ENV_FILE"] == str(home / ".env")
    assert os.environ["CREW_TEST_HOME_ENV_KEY"] == "sk-from-crew-home"
    assert cfg.active_model.api_key == "sk-from-crew-home"


def test_load_config_reads_model_key_from_system_config_env(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config_dir = root / "config"
    crew_home = root / ".crew"
    config_dir.mkdir(parents=True)
    crew_home.mkdir()
    (config_dir / ".env").write_text(
        "CREW_LAYER_TEST_KEY=sk-from-system-config\n",
        encoding="utf-8",
    )
    cfg_path = config_dir / "config.yaml"
    cfg_path.write_text(
        "runtime:\n"
        f"  crew_home: {crew_home}\n"
        "llm:\n"
        "  models:\n"
        "    test:\n"
        "      api_key_env: CREW_LAYER_TEST_KEY\n"
        "      model: fake-model\n"
        "  active: test\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "ROOT", root)
    monkeypatch.delenv("CREW_HOME", raising=False)
    monkeypatch.delenv("CREW_LAYER_TEST_KEY", raising=False)

    cfg = load_config(cfg_path)

    assert cfg.active_model.api_key == "sk-from-system-config"
    assert os.environ["CREW_ENV_FILE"] == str(crew_home / ".env")


def test_load_config_initializes_local_yaml_from_publishable_example(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    example = config_dir / "config.yaml.example"
    example.write_text(
        "llm:\n  active: default\n  models:\n    default:\n      model: example-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "ROOT", root)
    monkeypatch.delenv("CREW_HOME", raising=False)

    cfg = load_config()

    local = config_dir / "config.yaml"
    assert local.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")
    assert cfg.config_path == str(local)
    assert cfg.active_model.model == "example-model"


def test_load_config_never_overwrites_existing_local_yaml(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml.example").write_text(
        "llm:\n  active: default\n  models:\n    default:\n      model: example-model\n",
        encoding="utf-8",
    )
    local = config_dir / "config.yaml"
    local_content = "llm:\n  active: local\n  models:\n    local:\n      model: local-model\n"
    local.write_text(local_content, encoding="utf-8")
    monkeypatch.setattr(config_module, "ROOT", root)
    monkeypatch.delenv("CREW_HOME", raising=False)

    cfg = load_config()

    assert local.read_text(encoding="utf-8") == local_content
    assert cfg.config_path == str(local)
    assert cfg.active_model.model == "local-model"


def test_frozen_config_initializes_crew_home_from_example(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    example = config_dir / "config.yaml.example"
    example.write_text(
        "llm:\n  active: default\n  models:\n    default:\n      model: packaged-model\n",
        encoding="utf-8",
    )
    (config_dir / ".env.example").write_text("CREW_MODEL_API_KEY=\n", encoding="utf-8")
    user_home = tmp_path / "user-home"
    monkeypatch.setattr(config_module, "ROOT", root)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("CREW_HOME", str(user_home))

    cfg = load_config()

    local = user_home / "config.yaml"
    assert local.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")
    assert (user_home / ".env").is_file()
    assert cfg.config_path == str(local)
    assert cfg.active_model.model == "packaged-model"


def test_load_config_user_crew_env_overrides_system_config_env(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config_dir = root / "config"
    crew_home = root / ".crew"
    config_dir.mkdir(parents=True)
    crew_home.mkdir()
    (config_dir / ".env").write_text(
        "CREW_LAYER_TEST_KEY=sk-from-system-config\n",
        encoding="utf-8",
    )
    (crew_home / ".env").write_text(
        "CREW_LAYER_TEST_KEY=sk-from-user-crew\n",
        encoding="utf-8",
    )
    cfg_path = config_dir / "config.yaml"
    cfg_path.write_text(
        "runtime:\n"
        f"  crew_home: {crew_home}\n"
        "llm:\n"
        "  models:\n"
        "    test:\n"
        "      api_key_env: CREW_LAYER_TEST_KEY\n"
        "      model: fake-model\n"
        "  active: test\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "ROOT", root)
    monkeypatch.delenv("CREW_HOME", raising=False)
    monkeypatch.delenv("CREW_LAYER_TEST_KEY", raising=False)

    cfg = load_config(cfg_path)

    assert cfg.active_model.api_key == "sk-from-user-crew"
    assert os.environ["CREW_ENV_FILE"] == str(crew_home / ".env")


def test_refresh_owner_runtime_env_loads_system_and_owner_env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    crew_home = tmp_path / ".Crew"
    owner = "owner:user-a"
    owner_home = crew_home / "accounts" / owner_path_segment(owner)
    owner_home.mkdir(parents=True)
    (config_dir / ".env").write_text(
        "CREW_SYSTEM_ONLY=system-v1\nCREW_SHARED_KEY=system-v1\n",
        encoding="utf-8",
    )
    (owner_home / ".env").write_text(
        "CREW_OWNER_ONLY=owner-v1\nCREW_SHARED_KEY=owner-v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(home_module, "ROOT", root)
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    monkeypatch.delenv("CREW_SYSTEM_ONLY", raising=False)
    monkeypatch.delenv("CREW_OWNER_ONLY", raising=False)
    monkeypatch.delenv("CREW_SHARED_KEY", raising=False)

    refresh_owner_runtime_env(owner)

    assert os.environ["CREW_SYSTEM_ONLY"] == "system-v1"
    assert os.environ["CREW_OWNER_ONLY"] == "owner-v1"
    assert os.environ["CREW_SHARED_KEY"] == "owner-v1"
    assert os.environ["CREW_ENV_FILE"] == str(owner_home / ".env")


def test_runtime_env_cannot_override_security_or_process_bootstrap(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    crew_home = tmp_path / ".Crew"
    owner = "owner:user-a"
    owner_home = crew_home / "accounts" / owner_path_segment(owner)
    owner_home.mkdir(parents=True)
    (config_dir / ".env").write_text(
        "ACE_STRICT_SECURITY=0\n"
        "ACE_DESKTOP_SECURITY_RUNTIME=C:\\\\attacker.exe\n"
        "PYTHONPATH=C:\\\\attacker\n"
        "PATH=C:\\\\attacker\n"
        "CREW_HOME=C:\\\\attacker\n"
        "CREW_ALLOWED_LITERAL=${HOST_ONLY_SECRET}\n",
        encoding="utf-8",
    )
    (owner_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(home_module, "ROOT", root)
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    monkeypatch.setenv("ACE_STRICT_SECURITY", "1")
    monkeypatch.delenv("ACE_DESKTOP_SECURITY_RUNTIME", raising=False)
    monkeypatch.setenv("PYTHONPATH", "trusted-pythonpath")
    monkeypatch.setenv("PATH", "trusted-path")
    monkeypatch.setenv("HOST_ONLY_SECRET", "must-not-expand")

    refresh_owner_runtime_env(owner)

    assert os.environ["ACE_STRICT_SECURITY"] == "1"
    assert "ACE_DESKTOP_SECURITY_RUNTIME" not in os.environ
    assert os.environ["PYTHONPATH"] != r"C:\attacker"
    assert os.environ["PATH"] != r"C:\attacker"
    assert os.environ["CREW_HOME"] == str(crew_home)
    assert os.environ["CREW_ALLOWED_LITERAL"] == "${HOST_ONLY_SECRET}"


@pytest.mark.parametrize("scope", ["system", "owner"])
def test_refresh_owner_runtime_env_hot_reloads_env(tmp_path, monkeypatch, scope):
    root = tmp_path / "repo"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    crew_home = tmp_path / ".Crew"
    owner = "owner:user-a"
    key = f"CREW_HOT_{scope.upper()}"
    if scope == "owner":
        env_dir = crew_home / "accounts" / owner_path_segment(owner)
        env_dir.mkdir(parents=True)
        env_file = env_dir / ".env"
    else:
        env_file = config_dir / ".env"
    env_file.write_text(f"{key}={scope}-v1\n", encoding="utf-8")
    monkeypatch.setattr(home_module, "ROOT", root)
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    monkeypatch.delenv(key, raising=False)

    refresh_owner_runtime_env(owner)
    assert os.environ[key] == f"{scope}-v1"

    env_file.write_text(f"{key}={scope}-v2\n", encoding="utf-8")
    refresh_owner_runtime_env(owner)

    assert os.environ[key] == f"{scope}-v2"


def test_refresh_owner_runtime_env_reuses_unchanged_env_cache(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    crew_home = tmp_path / ".Crew"
    owner = "owner:user-a"
    owner_home = crew_home / "accounts" / owner_path_segment(owner)
    owner_home.mkdir(parents=True)
    (config_dir / ".env").write_text("CREW_CACHE_SYSTEM=system\n", encoding="utf-8")
    (owner_home / ".env").write_text("CREW_CACHE_OWNER=owner\n", encoding="utf-8")
    monkeypatch.setattr(home_module, "ROOT", root)
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    original_dotenv_values = home_module.dotenv_values
    calls = {"count": 0}

    def counting_dotenv_values(*args, **kwargs):
        calls["count"] += 1
        return original_dotenv_values(*args, **kwargs)

    monkeypatch.setattr(home_module, "dotenv_values", counting_dotenv_values)

    refresh_owner_runtime_env(owner)
    assert calls["count"] == 2

    refresh_owner_runtime_env(owner)
    assert calls["count"] == 2


def test_refresh_owner_runtime_env_perf_log_is_redacted(tmp_path, monkeypatch, caplog):
    root = tmp_path / "repo"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    crew_home = tmp_path / ".Crew"
    owner = "owner:user-a"
    monkeypatch.setattr(home_module, "ROOT", root)
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    owner_home = crew_home / "accounts" / owner_path_segment(owner)
    owner_home.mkdir(parents=True)
    (config_dir / ".env").write_text("CREW_SECRET_LOG_TEST=system-secret\n", encoding="utf-8")
    (owner_home / ".env").write_text("CREW_OWNER_SECRET_LOG_TEST=owner-secret\n", encoding="utf-8")
    with caplog.at_level("INFO", logger="crew.state.home"):
        refresh_owner_runtime_env(owner)

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "runtime_env_refresh" in text
    assert "env_cache=" in text
    assert "system-secret" not in text
    assert "owner-secret" not in text
