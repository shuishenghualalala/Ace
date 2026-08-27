"""Companion domain: nearby identity, conversations, Agent seats and transport outbox."""

from .service import CompanionService
from .store import CompanionStore
from .tools import register_companion_tools

__all__ = ["CompanionService", "CompanionStore", "register_companion_tools"]
