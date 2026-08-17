/**
 * composer-mention 纯逻辑单测：触发判定 + chip token 区间。
 * 这些是整套补全里最易出细微偏差的部分（边界、邮箱误触、前导空白计数），
 * 用固定输入断言，避免依赖 DOM。
 */
// @vitest-environment happy-dom

import { describe, expect, it } from 'vitest';
import { buildChippedNodes, compactMentionText, computePinyin, detectTrigger, getUserAgentMentions, iterChipTokens, mentionTextForSkill, matchSkill, renderChip, serializeMentionInput } from '../../src/ui/features/composer-mention';

describe('detectTrigger', () => {
  it('行首 @ 触发', () => {
    expect(detectTrigger('@', 1)).toEqual({ trigger: '@', start: 0 });
    expect(detectTrigger('@file:sr', 9)).toEqual({ trigger: '@', start: 0 });
  });

  it('空格后的 @ 触发', () => {
    expect(detectTrigger('看下 @file:sr', 11)).toEqual({ trigger: '@', start: 3 });
  });

  it('邮箱 foo@bar 不触发（@ 前非边界）', () => {
    expect(detectTrigger('foo@bar', 7)).toBeNull();
    expect(detectTrigger('foo@bar.com', 11)).toBeNull();
  });

  it('未解析的裸 @typing 仍触发补全（药丸另算）', () => {
    // 触发判定只看边界，是否画药丸由 iterChipTokens 按 @file: 前缀决定
    expect(detectTrigger('裸 @typing', 10)).toEqual({ trigger: '@', start: 2 });
  });

  it('行首 / 触发', () => {
    expect(detectTrigger('/rev', 4)).toEqual({ trigger: '/', start: 0 });
  });

  it('空格后的 / 触发', () => {
    expect(detectTrigger('用 /review 改', 9)).toEqual({ trigger: '/', start: 2 });
  });

  it('路径 a/b 不触发（/ 前非边界）', () => {
    expect(detectTrigger('a/b', 3)).toBeNull();
    expect(detectTrigger('src/index', 9)).toBeNull();
  });

  it('// 与 /* 不触发（/ 后非法 slug 起始）', () => {
    expect(detectTrigger('//', 2)).toBeNull();
    expect(detectTrigger('/*x', 3)).toBeNull();
  });

  it('光标前是普通文本时不触发', () => {
    expect(detectTrigger('你好世界', 4)).toBeNull();
    expect(detectTrigger('', 0)).toBeNull();
  });

  it('光标在触发符与查询中间（只打了 @）', () => {
    expect(detectTrigger('文字 @', 3)).toBeNull(); // caret 在 @ 之前的空格处
    expect(detectTrigger('文字 @', 4)).toEqual({ trigger: '@', start: 3 });
  });

  it('/ 后接中文触发（回归：打"博"不应丢失浮层，需按中文名搜技能）', () => {
    expect(detectTrigger('/博', 2)).toEqual({ trigger: '/', start: 0 });
    expect(detectTrigger('/网页搜索', 5)).toEqual({ trigger: '/', start: 0 });
    expect(detectTrigger('用 /网页 改下', 5)).toEqual({ trigger: '/', start: 2 });
  });
});

describe('matchSkill（多维 + 容错匹配）', () => {
  const searchSkill = { slug: 'web-search', name: 'web-search', description: 'Generic web search', display_name: '网页搜索', description_zh: '通用网络搜索', source: 'builtin' as const };

  it('空查询命中（展示全部）', () => {
    expect(matchSkill(searchSkill, '')).toBe(99);
    expect(matchSkill(searchSkill, '   ')).toBe(99);
  });

  it('slug / 中文名 整词相等 → 最高分 6', () => {
    expect(matchSkill(searchSkill, '网页搜索')).toBe(6);
  });

  it('前缀命中 → 5', () => {
    expect(matchSkill(searchSkill, '网页')).toBe(5);
    expect(matchSkill(searchSkill, 'web')).toBe(5);
  });

  it('英文 slug 子串命中（"显示中文名也要支持英文名匹配"）→ 4', () => {
    expect(matchSkill(searchSkill, 'eb')).toBe(4);
    expect(matchSkill(searchSkill, 'search')).toBe(4);
  });

  it('description 子串命中 → 3', () => {
    expect(matchSkill(searchSkill, '网络')).toBe(3);
  });

  it('子序列（缩写）命中 → 2', () => {
    expect(matchSkill(searchSkill, 'wbs')).toBe(2); // web-search 的子序列缩写
  });

  it('漏字/缩写走子序列层（更强）→ 2', () => {
    expect(matchSkill(searchSkill, '网页索')).toBe(2); // 漏"搜"，仍是子序列
  });

  it('打错一个字（替换，非子序列非子串）→ 编辑距离层 1', () => {
    expect(matchSkill(searchSkill, '网页梭')).toBe(1); // "搜"→"梭"
    expect(matchSkill(searchSkill, '网页搜索索')).toBe(1); // 多字
  });

  it('毫不相干 → 不匹配 0', () => {
    expect(matchSkill(searchSkill, '天气预报')).toBe(0);
    expect(matchSkill(searchSkill, 'xyz')).toBe(0);
  });

  it('2 字查询不触发模糊层：相近但不匹配的技能不应误命中', () => {
    const document = { slug: 'document', name: 'document', description: '', display_name: '文档写作', description_zh: '', source: 'builtin' as const };
    expect(matchSkill(document, '网页')).toBe(0);
  });

  it('中文名与英文名都可作为查询入口，且中文名优先级不低于英文', () => {
    expect(matchSkill(searchSkill, '网页搜索')).toBeGreaterThanOrEqual(matchSkill(searchSkill, 'web'));
  });
});

describe('拼音匹配（输入 wangye / wyss 搜到中文名）', () => {
  const searchSkill = { slug: 'web-search', name: 'web-search', description: 'Generic web search', display_name: '网页搜索', description_zh: '通用网络搜索', source: 'builtin' as const };
  const py = computePinyin(searchSkill.display_name);

  it('computePinyin: 首字母串 + 全拼', () => {
    expect(py.initials).toBe('wyss');
    expect(py.full).toContain('wangye'); // 网页
    expect(py.full).toContain('sousuo'); // 搜索
  });

  it('全拼子串命中（sousuo → 搜索）→ 3', () => {
    expect(matchSkill(searchSkill, 'sousuo', py)).toBe(3);
  });

  it('首字母整词 / 前缀命中（wyss / wys）→ 3', () => {
    expect(matchSkill(searchSkill, 'wyss', py)).toBe(3);
    expect(matchSkill(searchSkill, 'wys', py)).toBe(3);
  });
});

describe('iterChipTokens', () => {
  // 第二参数是完整 chip 文本集合（/中文名 与 /slug）
  const slashTokens = new Set(['/review', '/tdd', '/网页搜索']);

  it('已解析的 @file: 染色，区间正确（含前导空格偏移）', () => {
    const v = '看下 @file:src/a.ts 的逻辑';
    const tokens = iterChipTokens(v, slashTokens);
    expect(tokens).toHaveLength(1);
    expect(tokens[0]).toEqual({ start: 3, end: 3 + '@file:src/a.ts'.length, kind: 'at' });
    expect(v.slice(tokens[0]!.start, tokens[0]!.end)).toBe('@file:src/a.ts');
  });

  it('@folder: / @image: 同样染色', () => {
    expect(iterChipTokens('@folder:src', slashTokens)).toEqual([{ start: 0, end: 11, kind: 'at' }]);
    expect(iterChipTokens('@image:a.png', slashTokens)).toEqual([{ start: 0, end: 12, kind: 'at' }]);
  });

  it('裸 @typing 不染色（缺 file/folder/image 前缀）', () => {
    expect(iterChipTokens('裸 @typing 文本', slashTokens)).toEqual([]);
  });

  it('已知 /slug 染色；未知 /unknown 不染色', () => {
    const tokens = iterChipTokens('用 /review 和 /unknown', slashTokens);
    expect(tokens).toHaveLength(1);
    expect(tokens[0]).toEqual({ start: 2, end: 2 + '/review'.length, kind: 'slash' });
  });

  it('/slug 必须在尾边界结束，避免 /reviewer 误染 /review', () => {
    expect(iterChipTokens('用 /reviewer 改', slashTokens)).toEqual([]);
  });

  it('同起点 token 取最长命中，避免短 token 抢长 token', () => {
    const tokens = iterChipTokens('/reviewer', new Set(['/review', '/reviewer']));
    expect(tokens).toEqual([{ start: 0, end: '/reviewer'.length, kind: 'slash' }]);
  });

  it('换行后的 @file: 也染色', () => {
    const v = '第一行\n@file:src/a.ts';
    expect(iterChipTokens(v, slashTokens)).toEqual([
      { start: 4, end: 4 + '@file:src/a.ts'.length, kind: 'at' },
    ]);
  });

  it('/中文名 chip 也染色（边界后命中）', () => {
    const tokens = iterChipTokens('用 /网页搜索 搜一下', slashTokens);
    expect(tokens).toHaveLength(1);
    expect(tokens[0]).toEqual({ start: 2, end: 2 + '/网页搜索'.length, kind: 'slash' });
  });

  it('/ 前非边界不染色（路径式 a/review）', () => {
    expect(iterChipTokens('路径 a/review 里的', slashTokens)).toEqual([]);
  });

  it('多个 chip 混排，按位置排序', () => {
    const v = '@file:a.ts 然后 /review';
    const tokens = iterChipTokens(v, slashTokens);
    expect(tokens.map((t) => t.kind)).toEqual(['at', 'slash']);
    expect(tokens.map((t) => v.slice(t.start, t.end))).toEqual(['@file:a.ts', '/review']);
  });

  it('整段删的命中判定：恰好结束于光标的 token', () => {
    const v = '看下 @file:a.ts';
    const end = v.length;
    const hit = iterChipTokens(v, slashTokens).find((t) => t.end === end);
    expect(hit).toBeDefined();
    expect(hit!.start).toBe(3);
  });

  it('光标落在 token 中间不算「结束」', () => {
    const v = '@file:a.ts';
    // 光标在 a.ts 中间（index 7）
    const hit = iterChipTokens(v, slashTokens).find((t) => t.end === 7);
    expect(hit).toBeUndefined();
  });
});

describe('mentionTextForSkill', () => {
  const base = { name: 'review', slug: 'review', description: '', source: 'builtin' as const };

  it('display_name 安全唯一时插入 /中文名', () => {
    expect(mentionTextForSkill({ ...base, display_name: '代码审查' }, [{ ...base, display_name: '代码审查' }])).toBe('/代码审查');
  });

  it('display_name 含空白时回退 /slug，避免后端 slash command 被空格截断', () => {
    expect(mentionTextForSkill({ ...base, display_name: '代码 审查' }, [{ ...base, display_name: '代码 审查' }])).toBe('/review');
  });

  it('display_name 重名时回退 /slug，避免激活第一个同名 skill', () => {
    const skills = [
      { ...base, slug: 'review', display_name: '助手' },
      { ...base, slug: 'tdd', display_name: '助手' },
    ];
    expect(mentionTextForSkill(skills[0]!, skills)).toBe('/review');
    expect(mentionTextForSkill(skills[1]!, skills)).toBe('/tdd');
  });
});

describe('Team agent mentions', () => {
  it('shows the member name while preserving the canonical member id for sending', () => {
    const token = compactMentionText({
      text: '@team-kk',
      display: 'kk',
      meta: '全栈开发',
      sig: 'agent',
      userMention: { kind: 'team_member', member_id: 'team-kk' },
    });

    expect(token).toBe('@kk');
    expect(getUserAgentMentions(`请 ${token} 写方案`)).toEqual([
      { kind: 'team_member', member_id: 'team-kk' },
    ]);
    expect(serializeMentionInput(`请 ${token} 写方案`)).toBe('请 @team-kk 写方案');
    expect(getUserAgentMentions('请写方案')).toEqual([]);
  });

  it('keeps a spaced display name readable without changing the member id', () => {
    const token = compactMentionText({
      text: '@crew-builtin',
      display: 'Crew 内置智能体',
      meta: 'Leader',
      sig: 'agent',
      userMention: { kind: 'team_member', member_id: 'crew-builtin' },
    });

    expect(token).toBe('@Crew 内置智能体');
    expect(serializeMentionInput(`询问 ${token}`)).toBe('询问 @crew-builtin');
  });
});

describe('renderChip（只显示名字，文件引用使用类型图标）', () => {
  it('/ skill chip：保留标记宽度，只显示中文名', () => {
    expect(renderChip('/网页搜索', 'slash')).toEqual({ mark: '/', body: '网页搜索' });
  });
  it('@ file chip：保留类型标记信息，只显示路径', () => {
    expect(renderChip('@file:src/a.ts', 'at')).toEqual({ mark: '@file:', body: 'src/a.ts' });
    expect(renderChip('@folder:src', 'at')).toEqual({ mark: '@folder:', body: 'src' });
    expect(renderChip('@image:a.png', 'at')).toEqual({ mark: '@image:', body: 'a.png' });
  });

  it('文件引用覆盖层不暴露内部 folder 标记文字', () => {
    const nodes = buildChippedNodes('@folder:Ace');
    const chip = nodes[0] as HTMLElement;

    expect(chip.querySelector('.mention-chip__mark')?.textContent).toBe('');
    expect(chip.querySelector('.mention-chip__mark .mw-icon')).not.toBeNull();
    expect(chip.textContent).toBe('Ace');
  });

  it('选中文件引用使用短 token，发送时还原完整 token', () => {
    const item = { text: '@folder:Ace/desktop', display: 'desktop', meta: 'Ace/desktop', sig: 'folder' as const };
    const visible = compactMentionText(item);
    expect(visible).toBe('@Ace/desktop');
    expect(serializeMentionInput(`查看 ${visible} 怎么`)).toBe('查看 @folder:Ace/desktop 怎么');
  });
});
