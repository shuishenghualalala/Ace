"""浏览器录制功能的本地仿真站点。

**通用性验收请对着真实站点跑（B 站 / 知乎等）**——公网站点不受 `validate_ip`
限制，而且生产级页面的复杂度是自己写的 HTML 给不了的。这两个仿真站点只覆盖
真实站点做不到的部分：

- `ticket`（站点 A）：内网工单。**内网连不上，只能仿真。** 需登录、跨文档导航、
  审批按钮是真的 `<button type=submit>`、脏结构（无 label 输入框 / table 布局 /
  iframe / 按钮文案带换行）、提示词注入工单、字段漂移开关。
- `feed`（站点 B）：确定性陷阱夹具，**不是真实内容站的替身**。只为三件真实站点
  给不了的事而存在：确定性（站点改版会让回归测试静默失效）、离线 CI、注入靶子。
  提供同文档分类切换（`loaderId` 不变）、同文档「加载更多」、站内搜索（含无结果）、
  榜单顺序每次访问都变、重名链接。

用法（测试）::

    from tests.fixtures.mocksites import serve

    with serve() as (base_url, state):
        ...

用法（手工，对着内置浏览器验证）::

    python -m tests.fixtures.mocksites --port 8799

注意：内置浏览器默认拒绝一切私网与环回地址（见 crew/browser/security.py 的
`validate_ip`）。要让它访问这两个站点，测试配置里需把 `127.0.0.1` 加进
`allowed_private_hosts`——**不要加进默认配置**。
"""

from ._state import MockState, Response
from .server import build_server, serve

__all__ = ["MockState", "Response", "build_server", "serve"]
