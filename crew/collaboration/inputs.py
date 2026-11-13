"""Bounded, owner-scoped source snapshots; references alone are not read access."""
from __future__ import annotations

import hashlib
from typing import Any


def wiki_source_snapshot(store: Any, owner: str, kb_id: str, *, max_chars: int = 120000) -> dict:
    if not owner or store is None:
        raise ValueError('知识库读取服务不可用；尚未读取资料')
    if kb_id not in {kb.id for kb in store.list_kbs(owner)}:
        raise ValueError('所选知识库不存在或当前账户无权读取；尚未读取资料')
    sources, total, titles = [], 0, set()
    for raw in store.list_raws(owner, kb_id):
        if raw.is_duplicate or getattr(raw, 'superseded_by', None):
            continue
        if raw.parse_status != 'parsed':
            raise ValueError(f'知识库来源尚未解析：{raw.title}')
        if raw.title in titles:
            raise ValueError(f'知识库来源标题重复，无法无歧义引用：{raw.title}')
        titles.add(raw.title)
        page = store.get_source_page(raw.id, owner, kb_id)
        if page is None or not page.content.strip():
            raise ValueError(f'知识库来源页面缺失：{raw.title}')
        total += len(page.content)
        if total > max_chars or len(sources) >= 50:
            raise ValueError('所选知识库超过本轮只读快照上限（50 份/12万字符）；请建立更小的评审资料库')
        sources.append({'source_id': raw.id, 'page_id': page.id, 'title': raw.title,
                        'content': page.content, 'sha256': hashlib.sha256(page.content.encode()).hexdigest()})
    if not sources:
        raise ValueError('所选知识库没有可读来源；请先导入并解析项目材料')
    return {'kind': 'knowledge_base', 'id': kb_id, 'access': 'read_snapshot', 'sources': sources}
