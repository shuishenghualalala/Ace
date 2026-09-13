"""Channels product package: platform adapters, delivery, and session routing."""

from crew.channels.feature import (
    CHANNELS_FEATURE_ID,
    CHANNELS_SERVICE_KEY,
    ChannelsFeatureBundle,
    ChannelsService,
    build_channels_feature,
)

__all__ = [
    "CHANNELS_FEATURE_ID",
    "CHANNELS_SERVICE_KEY",
    "ChannelsFeatureBundle",
    "ChannelsService",
    "build_channels_feature",
]
