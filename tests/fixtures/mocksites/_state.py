"""仿真站点的进程内状态。

每个 server 实例持有独立一份，测试之间不串味。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class MockState:
    """两个站点共享的可变状态。"""

    # ticket 站：已登录会话的 cookie 值 -> 工号
    sessions: dict[str, str] = field(default_factory=dict)
    # ticket 站：打开后详情页字段顺序改变，用来测回放对页面漂移的容错
    drift: bool = False
    # feed 站：每次访问排行榜自增，用来确定性地轮换榜单顺序
    #（确定性而非随机：测试要能预测，人工核对时也能复现）
    feed_visits: int = 0
    # 登录时提交过的密码 / 验证码，仅用于断言「录制轨迹里不该出现这些值」。
    # 仿真站点当然不校验密码——它存在的意义就是让测试拿到一个已知的哨兵值。
    submitted_secrets: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.sessions.clear()
        self.drift = False
        self.feed_visits = 0
        self.submitted_secrets.clear()
