"""Feature lifecycle primitives shared by first-party and external features."""

from crew.features.dependencies import (
    FeatureActivationPlan,
    FeatureDependencyBlock,
    FeatureDependencyGraph,
    FeatureDependencyResolution,
    FeatureServiceDependencies,
)
from crew.features.runtime import (
    FeatureActivationError,
    FeatureCleanupError,
    FeatureCleanupIssue,
    FeatureConfigRevisions,
    FeatureGeneration,
    FeatureScope,
    FeatureState,
    FeatureTransaction,
    RegistrationState,
    RegistrationToken,
    StaleFeatureGenerationError,
)
from crew.features.services import (
    ServiceBinding,
    ServiceConflictError,
    ServiceKey,
    ServiceNotFoundError,
    ServiceRegistry,
    ServiceScopeKind,
    ServiceScopePath,
)

__all__ = [
    "FeatureActivationError",
    "FeatureActivationPlan",
    "FeatureCleanupError",
    "FeatureCleanupIssue",
    "FeatureConfigRevisions",
    "FeatureDependencyBlock",
    "FeatureDependencyGraph",
    "FeatureDependencyResolution",
    "FeatureGeneration",
    "FeatureScope",
    "FeatureServiceDependencies",
    "FeatureState",
    "FeatureTransaction",
    "RegistrationState",
    "RegistrationToken",
    "ServiceBinding",
    "ServiceConflictError",
    "ServiceKey",
    "ServiceNotFoundError",
    "ServiceRegistry",
    "ServiceScopeKind",
    "ServiceScopePath",
    "StaleFeatureGenerationError",
]
