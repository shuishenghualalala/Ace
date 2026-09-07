"""Compatibility import for the Work route contribution.

The implementation belongs to :mod:`crew.work.routes`; this module remains
temporarily so third-party integrations importing the historical factory keep
working while the Gateway no longer assembles Work directly.
"""

from crew.work.routes import create_work_router

__all__ = ["create_work_router"]
