"""多智能体 Team：Leader + Teammate + 异构成员单进程协同。"""

from crew.team.bus import TeamBus
from crew.team.models import MemberSession, TeamArtifact, TeamMemberSpec, TeamMessage, TeamSession


def __getattr__(name: str):
    if name == "InProcessTeamManager":
        from crew.team.team_manager import InProcessTeamManager

        return InProcessTeamManager
    raise AttributeError(name)

__all__ = [
    "InProcessTeamManager",
    "TeamBus",
    "TeamSession",
    "MemberSession",
    "TeamMemberSpec",
    "TeamMessage",
    "TeamArtifact",
]
