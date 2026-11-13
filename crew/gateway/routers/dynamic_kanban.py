"""Compatibility import for the Dynamic Kanban route contribution.

The implementation belongs to :mod:`crew.dynamickanban.routes`; this module
remains temporarily so third-party integrations importing the historical
factory keep working while the Gateway no longer assembles it directly.
"""

from crew.dynamickanban.routes import create_dynamic_kanban_router

__all__ = ["create_dynamic_kanban_router"]
