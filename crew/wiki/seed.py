"""内置「LLM Wiki 使用教程」知识库的初始化（seed）。

教程页面是随代码发布的静态 markdown 文件（``crew/wiki/seed/tutorial/``），
而每个用户的知识库存储在自己的 ``<owner_home>/wiki_lib/`` 目录里。
本模块负责在用户首次接触 Wiki 时，把这份预置内容复制进用户存储，
建成 kb_id 为 ``tutorial`` 的知识库。

特性：
- 一次复制：通过标记文件 ``<owner_home>/.tutorial_kb_seeded`` 保证只导入一次。
- 可编辑：复制后的页面属于用户知识库存储，可正常修改。
- 预置导读：``INTRO.md`` 是手写的 Home 导读，
  初始化时直接写入 KB 元数据（status=ready），新用户无需 LLM 调用即可看到。
- 懒加载：仅在读取知识库列表时执行，不进入 Crew 启动链路。
- 容错：任何失败只记日志，绝不影响 Wiki 列表接口。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from crew.state.logging import get_logger
from crew.wiki.store._serde import deserialize_page

log = get_logger("wiki.seed")

TUTORIAL_KB_ID = "tutorial"
TUTORIAL_KB_NAME = "LLM Wiki 使用教程"

_SEED_DIR = Path(__file__).parent / "seed" / "tutorial"
_PAGE_SUBDIRS = ("topics", "entities")
_MARKER_NAME = ".tutorial_kb_seeded"

# index.md 的分组顺序：按页面第一个标签归类
_INDEX_SECTIONS = ("入门", "理论", "界面功能", "对话交互", "案例")


def ensure_tutorial_kb(store: Any, owner_account_id: str = "") -> bool:
    """如果教程知识库不存在就复制一份；已存在则不覆盖用户修改。

    返回 True 表示本次执行了初始化。任何异常都被吞掉并记日志。
    """
    try:
        return _ensure(store, owner_account_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("教程知识库初始化失败: %s", exc)
        return False


def _ensure(store: Any, owner_account_id: str) -> bool:
    if not _SEED_DIR.exists():
        log.warning("教程种子目录不存在: %s", _SEED_DIR)
        return False
    kb_root = store._kb_root(owner_account_id)  # <owner_home>/wiki_lib
    marker = kb_root.parent / _MARKER_NAME
    kb_dir = kb_root / TUTORIAL_KB_ID
    if marker.exists():
        # 已复制过，不覆盖用户后续对教程页面的修改。
        return False
    if kb_dir.exists():
        # 目录已存在（例如用户手动建了同名库）：不覆盖，只补标记
        _write_marker(marker)
        return False

    store.create_kb(TUTORIAL_KB_ID, TUTORIAL_KB_NAME, owner_account_id=owner_account_id)

    pages = []
    for sub in _PAGE_SUBDIRS:
        sub_dir = _SEED_DIR / sub
        if not sub_dir.exists():
            continue
        for path in sorted(sub_dir.glob("*.md")):
            # 与 ingest 正常产出的页面路径保持一致（wiki/<type>/xxx.md），
            # 否则前端文件树（按 wiki/ 前缀过滤）会看不到这些页面。
            rel = f"wiki/{sub}/{path.name}"
            page = deserialize_page(path.read_text(encoding="utf-8"), rel)
            store.save_page(page, owner_account_id=owner_account_id, kb_id=TUTORIAL_KB_ID)
            pages.append(page)

    _write_schema(kb_dir)
    _write_index(kb_dir, pages)
    _seed_home_intro(store, owner_account_id)
    store.update_home(owner_account_id, TUTORIAL_KB_ID)
    _append_log(kb_dir, len(pages))
    _write_marker(marker)
    log.info("教程知识库已初始化: %s（%d 个页面）", kb_dir, len(pages))
    return True


def _seed_home_intro(store: Any, owner_account_id: str) -> None:
    """把手写的 Home 导读写入 KB 元数据，无需 LLM 即可展示。

    content_hash 按教程页面内容计算，与 WikiSummarizer 的刷新判定一致：
    教程页面未被修改时不会触发重生成，用户改动内容后才由后台刷新接管。
    """
    intro_file = _SEED_DIR / "INTRO.md"
    if not intro_file.exists():
        return
    from crew.wiki.schemas import HomeIntro
    from crew.wiki.summary import WikiSummarizer, _split_home_intro

    # 落盘后的页面内容经 serde 规范化，hash 必须按读回的页面计算，
    # 与 WikiSummarizer 刷新时看到的内容保持一致。
    stored_pages = store.list_all(owner_account_id=owner_account_id, kb_id=TUTORIAL_KB_ID, limit=10000)
    content_hash = WikiSummarizer._compute_content_hash(stored_pages, [])
    # INTRO.md 与 LLM 输出同构（导读 + ---推荐问题--- + 问题列表），复用同一解析。
    intro_text, questions = _split_home_intro(intro_file.read_text(encoding="utf-8"))
    store.set_home_intro(
        HomeIntro(
            text=intro_text,
            questions=questions,
            content_hash=content_hash,
            generated_at=time.time(),
            status="ready",
        ),
        owner_account_id,
        TUTORIAL_KB_ID,
    )


def _write_marker(marker: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")


def _write_schema(kb_dir: Path) -> None:
    schema = _SEED_DIR / "SCHEMA.md"
    if schema.exists():
        (kb_dir / "SCHEMA.md").write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")


def _write_index(kb_dir: Path, pages: list[Any]) -> None:
    """按标签分节生成 index.md，未匹配到分节的页面归入「其他」。"""
    grouped: dict[str, list[Any]] = {s: [] for s in _INDEX_SECTIONS}
    grouped["其他"] = []
    for page in pages:
        section = next((t for t in page.tags if t in grouped and t != "其他"), "其他")
        grouped[section].append(page)
    lines = [
        "# LLM Wiki 使用教程 · 索引",
        "",
        f"> 共 {len(pages)} 页。点击链接可查看对应页面。",
        "",
    ]
    for section in (*_INDEX_SECTIONS, "其他"):
        entries = grouped[section]
        if not entries:
            continue
        lines.append(f"## {section}")
        for page in entries:
            lines.append(f"- [[{page.title}]]")
        lines.append("")
    (kb_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")


def _append_log(kb_dir: Path, page_count: int) -> None:
    log_path = kb_dir / "log.md"
    entry = (
        f"## [{time.strftime('%Y-%m-%d')}] create | 内置教程知识库初始化\n"
        f"- 导入 {page_count} 个教程页面\n"
    )
    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry)
