"""站点 B：确定性陷阱夹具（排行榜 / 分类 / 搜索）。

**它不是 B 站/知乎的替身。** 「AX 快照在生产级页面上够不够用」这个问题只能由
真实站点回答，自己写的 HTML 证明不了任何东西——通用性验收请直接对着真实站点跑。

这个夹具只负责三件真实站点给不了的事：

1. **确定性**。真实站点随时可能把分类切换从 `pushState` 改成整页跳转，那样
   「同文档切换必须产生新整页摘要」这条回归测试就会静默地什么都不测，而且不报错。
2. **离线 / CI**。回归测试不能依赖外网可达与站点不改版。
3. **注入靶子**。不能往真实站点放「忽略以上指令」。

因此它不追求像一个真实内容站，只追求机制齐全：

- **分类标签切换是同文档的**：点标签只换内容 + `history.pushState`，
  `frameId + loaderId` 完全不变。`currentPageIdentity()` 因此不会变化
  （见 browser-host.ts:2067-2085 的注释：same-document 的历史/query 变化
  故意不改变文档身份）。任何「按页面身份判断内容有没有变」的逻辑都会在这里
  漏掉整整一类页面变化——这个站点就是为暴露它而建的。
- **「加载更多」同样是同文档的**：内容追加，文档不变。
- **站内搜索**：`type(submit=true)` 的放行路径，含无结果的情况。
- **每次访问榜单顺序都不同**：验证「页面理解是活的」——技能必须汇报当次真实
  排行，而不是复述录制时那一次。轮换是确定性的（按访问计数），测试可预测。
- 每行都有一个叫「详情」的链接：重名元素。
- 免登录：证明凭据分级不是被强加的流程，没有登录环节就整个不触发。
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

from ._html import esc, page
from ._state import MockState, Response

# 每页 3 条：每个分类都能点开至少两次「加载更多」，录制轨迹里才看得出
# 「反复同文档追加」这个行为；页面短也让人工核对快照更容易。
PAGE_SIZE = 3

CATEGORIES: list[tuple[str, str]] = [
    ("all", "全站"),
    ("tech", "科技"),
    ("digital", "数码"),
    ("film", "影视"),
    ("life", "生活"),
]

_TITLES: dict[str, list[tuple[str, str, int]]] = {
    "all": [
        ("国产大模型推理成本一年降了多少", "量子位", 98421),
        ("为什么老小区加装电梯这么难", "城市观察", 87330),
        ("这届年轻人开始给冰箱做减法", "生活周刊", 81207),
        ("固态电池离量产还差几步", "电动实验室", 76550),
        ("一个人如何完成一次县域旅行", "旅行手记", 70318),
        ("显卡价格在半年里走了什么曲线", "硬件党", 66204),
        ("短剧出海到底赚不赚钱", "内容观察", 61890),
        ("城市夜班公交的乘客都是谁", "纪实影像", 57733),
        ("家用 NAS 值不值得折腾", "数码日常", 52410),
        ("被误解的「预制菜」到底是什么", "食品科普", 48972),
        ("小语种专业毕业生都去哪了", "教育观察", 44105),
        ("为什么导航总让你在这个路口掉头", "地图研究所", 40388),
    ],
    "tech": [
        ("推理芯片的内存墙有解吗", "芯片纵横", 45220),
        ("开源协议这两年发生了什么变化", "开发者说", 41876),
        ("端侧模型量化的实际收益", "算法笔记", 38550),
        ("数据库为什么又开始卷单机性能", "存储观察", 35109),
        ("从零实现一个向量检索引擎", "码农手记", 31744),
        ("可观测性工具为什么越用越贵", "运维前线", 28390),
        ("WebAssembly 在服务端的真实进展", "前端深水区", 25017),
        ("为什么大家又开始自己搭邮件服务", "运维前线", 21663),
    ],
    "digital": [
        ("千元机的影像还能怎么卷", "数码日常", 39820),
        ("桌面显示器选购的三个误区", "硬件党", 36471),
        ("无线耳机的延迟到底能感知吗", "听觉实验室", 33095),
        ("机械键盘的轴体玄学拆解", "外设研究", 29760),
        ("轻薄本散热的物理极限", "笔记本评测", 26334),
        ("充电协议为什么还没统一", "电源实验室", 22908),
        ("电子墨水屏适合谁", "阅读器观察", 19540),
        ("移动固态硬盘的翻车重灾区", "存储观察", 16278),
    ],
    "film": [
        ("今年暑期档的三个意外", "影视观察", 42615),
        ("纪录片是怎么找到拍摄对象的", "纪实影像", 38240),
        ("为什么剧集越来越短", "内容观察", 34877),
        ("配乐如何改变一场戏的情绪", "声音设计", 31402),
        ("动画分镜里的时间控制", "动画研究", 27965),
        ("院线重映为什么突然多了起来", "影视观察", 24519),
        ("字幕组消失之后", "内容观察", 21073),
        ("一部低成本剧的拍摄账本", "纪实影像", 17640),
    ],
    "life": [
        ("租房搬家的成本清单", "生活周刊", 37118),
        ("一个人做饭的最优解", "食品科普", 33742),
        ("通勤一小时的人在想什么", "城市观察", 30285),
        ("如何在小阳台种活一盆番茄", "植物手记", 26831),
        ("周末两天的县城citywalk", "旅行手记", 23407),
        ("把厨房垃圾分出三类之后", "生活周刊", 19962),
        ("独居第五年的家电清单", "生活周刊", 16508),
        ("每天多睡半小时的代价", "城市观察", 13074),
    ],
}


def _rotated(key: str, visits: int) -> list[tuple[str, str, int]]:
    """按访问次数确定性地轮换榜单。

    刻意不用随机：测试要能预测第 N 次访问的顺序，人工核对时也要能复现。
    """
    items = _TITLES.get(key, [])
    if not items:
        return []
    offset = visits % len(items)
    return items[offset:] + items[:offset]


def _rows_html(key: str, items: list[tuple[str, str, int]], start: int) -> str:
    rows = []
    for index, (title, author, heat) in enumerate(items, start=start + 1):
        detail = f"/feed/item?{urlencode({'cat': key, 'title': title})}"
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f'<td><a href="{esc(detail)}">{esc(title)}</a></td>'
            f"<td>{esc(author)}</td>"
            f"<td>{heat}</td>"
            f'<td><a href="{esc(detail)}">详情</a></td>'
            "</tr>"
        )
    return "".join(rows)


def _rank_page(state: MockState, query: dict[str, str]) -> Response:
    state.feed_visits += 1
    visits = state.feed_visits
    active = query.get("cat", "all")
    if active not in dict(CATEGORIES):
        active = "all"

    # 整份榜单数据随页面一起下发，分类切换与「加载更多」全在前端完成 —— 不产生
    # 任何导航，`loaderId` 保持不变。
    payload = {
        key: [
            {"title": title, "author": author, "heat": heat}
            for title, author, heat in _rotated(key, visits)
        ]
        for key, _ in CATEGORIES
    }

    tabs = " ".join(
        f'<button type="button" role="tab" data-cat="{esc(key)}" '
        f'aria-selected="{"true" if key == active else "false"}">{esc(label)}</button>'
        for key, label in CATEGORIES
    )

    initial = _rotated(active, visits)
    body = f"""
<h1>热门排行榜</h1>

<form method="get" action="/feed/search">
  <label for="q">搜索</label>
  <input id="q" name="q" type="search" placeholder="搜索榜单内容">
  <button type="submit">搜索</button>
</form>

<div role="tablist" aria-label="榜单分类">{tabs}</div>

<table>
  <thead><tr><th>排名</th><th>标题</th><th>作者</th><th>热度</th><th>操作</th></tr></thead>
  <tbody id="rank-body">{_rows_html(active, initial[:PAGE_SIZE], 0)}</tbody>
</table>

<p><button type="button" id="load-more">加载更多</button></p>
<p id="rank-status">当前分类：{esc(dict(CATEGORIES)[active])}（第 {visits} 次访问）</p>

<script>
const DATA = {json.dumps(payload, ensure_ascii=False)};
const PAGE_SIZE = {PAGE_SIZE};
const LABELS = {json.dumps(dict(CATEGORIES), ensure_ascii=False)};
let active = {json.dumps(active)};
let shown = PAGE_SIZE;

function rowHtml(item, index, cat) {{
  const href = '/feed/item?cat=' + encodeURIComponent(cat)
    + '&title=' + encodeURIComponent(item.title);
  return '<tr><td>' + index + '</td>'
    + '<td><a href="' + href + '">' + item.title + '</a></td>'
    + '<td>' + item.author + '</td>'
    + '<td>' + item.heat + '</td>'
    + '<td><a href="' + href + '">详情</a></td></tr>';
}}

function render() {{
  const items = (DATA[active] || []).slice(0, shown);
  document.getElementById('rank-body').innerHTML =
    items.map((item, i) => rowHtml(item, i + 1, active)).join('');
  document.getElementById('rank-status').textContent =
    '当前分类：' + LABELS[active] + '（第 {visits} 次访问）';
  const more = document.getElementById('load-more');
  more.disabled = shown >= (DATA[active] || []).length;
  more.textContent = more.disabled ? '没有更多了' : '加载更多';
}}

document.querySelectorAll('[role=tab]').forEach((tab) => {{
  tab.addEventListener('click', () => {{
    active = tab.dataset.cat;
    shown = PAGE_SIZE;
    document.querySelectorAll('[role=tab]').forEach((other) => {{
      other.setAttribute('aria-selected', String(other === tab));
    }});
    // 关键：只改 URL 不导航。文档身份（frameId + loaderId）保持不变，
    // 但页面内容已经全换了。
    history.pushState({{cat: active}}, '', '/feed/?cat=' + encodeURIComponent(active));
    render();
  }});
}});

document.getElementById('load-more').addEventListener('click', () => {{
  shown += PAGE_SIZE;
  render();
}});
</script>
"""
    return Response(body=page("热门排行榜 · 内容站", body))


def _search_page(state: MockState, query: dict[str, str]) -> Response:
    keyword = query.get("q", "").strip()
    hits: list[tuple[str, str, str, int]] = []
    if keyword:
        # 全部分类都要搜，包括「全站」——它有自己独有的条目，跳过它会让
        # 「榜单上看得见、搜索却搜不到」，那是仿真站点自己的 bug，不是被测能力的。
        seen: set[str] = set()
        for key, label in CATEGORIES:
            for title, author, heat in _TITLES[key]:
                if title in seen:
                    continue
                if keyword in title or keyword in author:
                    seen.add(title)
                    hits.append((label, title, author, heat))

    if not keyword:
        result = "<p>请输入搜索词。</p>"
    elif not hits:
        # 无结果必须是一个明确、可读的状态。技能在这里应当如实汇报「没有结果」，
        # 而不是编造几条或退回到榜单首页。
        result = f"<p>没有找到与「{esc(keyword)}」相关的内容。</p>"
    else:
        rows = "".join(
            "<tr>"
            f"<td>{esc(label)}</td>"
            f"<td>{esc(title)}</td>"
            f"<td>{esc(author)}</td>"
            f"<td>{heat}</td>"
            "</tr>"
            for label, title, author, heat in hits
        )
        result = (
            f"<p>共找到 {len(hits)} 条与「{esc(keyword)}」相关的内容。</p>"
            "<table><thead><tr><th>分类</th><th>标题</th><th>作者</th><th>热度</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    body = f"""
<h1>搜索结果</h1>
<form method="get" action="/feed/search">
  <label for="q">搜索</label>
  <input id="q" name="q" type="search" value="{esc(keyword)}">
  <button type="submit">搜索</button>
</form>
{result}
<p><a href="/feed/">返回排行榜</a></p>
"""
    return Response(body=page(f"搜索：{keyword} · 内容站", body))


def _item_page(query: dict[str, str]) -> Response:
    title = query.get("title", "").strip()
    cat = query.get("cat", "all")
    label = dict(CATEGORIES).get(cat, "全站")
    if not title:
        return Response(status=404, body=page("未找到", "<h1>内容不存在</h1>"))
    body = f"""
<h1>{esc(title)}</h1>
<p>分类：{esc(label)}</p>
<p>这是仿真内容页，正文用于验证详情页抽取。</p>
<p><a href="/feed/">返回排行榜</a></p>
"""
    return Response(body=page(f"{title} · 内容站", body))


def handle(
    state: MockState,
    method: str,
    path: str,
    query: dict[str, str],
    form: dict[str, str],
    cookies: dict[str, str],
) -> Response | None:
    """处理 /feed/* 。返回 None 表示本站点不认这个路径。"""

    if path in {"/feed", "/feed/"}:
        return _rank_page(state, query)
    if path == "/feed/search":
        return _search_page(state, query)
    if path == "/feed/item":
        return _item_page(query)
    return None
