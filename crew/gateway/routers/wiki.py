"""Compatibility import for the Wiki route contribution.

The implementation belongs to :mod:`crew.wiki.routes`; this module remains
temporarily so third-party integrations importing the historical factory keep
working while the Gateway no longer assembles Wiki directly.
"""

from crew.wiki.routes import create_wiki_router

__all__ = ["create_wiki_router"]
