from types import SimpleNamespace

import pytest

from crew.collaboration.inputs import wiki_source_snapshot


class Wiki:
    def list_kbs(self, owner):
        return [SimpleNamespace(id='kb')] if owner == 'owner' else []
    def list_raws(self, owner, kb):
        assert owner == 'owner' and kb == 'kb'
        return [SimpleNamespace(id='s', title='A1.md', is_duplicate=False, parse_status='parsed')]
    def get_source_page(self, id, owner, kb):
        return SimpleNamespace(id='p', content='原始来源内容')


def test_read_snapshot_binds_owner_content_hash_and_limits():
    s = wiki_source_snapshot(Wiki(), 'owner', 'kb')
    assert s['sources'][0]['content'] == '原始来源内容'
    assert len(s['sources'][0]['sha256']) == 64
    with pytest.raises(ValueError, match='无权'):
        wiki_source_snapshot(Wiki(), 'other', 'kb')
    with pytest.raises(ValueError, match='上限'):
        wiki_source_snapshot(Wiki(), 'owner', 'kb', max_chars=1)
    with pytest.raises(ValueError, match='不可用'):
        wiki_source_snapshot(None, 'owner', 'kb')
