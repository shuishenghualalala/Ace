"""Feature lifecycle primitives shared by first-party and external features."""

from crew.features.runtime import (
    FeatureActivationError,
    FeatureCleanupError,
    FeatureCleanupIssue,
    FeatureGeneration,
    FeatureScope,
    FeatureState,
    FeatureTransaction,
    RegistrationState,
    RegistrationToken,
)

__all__ = [
    "FeatureActivationError",
    "FeatureCleanupError",
    "FeatureCleanupIssue",
    "FeatureGeneration",
    "FeatureScope",
    "FeatureState",
    "FeatureTransaction",
    "RegistrationState",
    "RegistrationToken",
]
