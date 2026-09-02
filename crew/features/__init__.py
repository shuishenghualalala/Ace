"""Feature lifecycle primitives shared by first-party and external features."""

from crew.features.dependencies import (
    FeatureActivationPlan,
    FeatureDependencyBlock,
    FeatureDependencyGraph,
    FeatureDependencyResolution,
    FeatureServiceDependencies,
)
from crew.features.manager import (
    FeatureDefinition,
    FeatureInstallContext,
    FeatureRecord,
    FeatureRuntime,
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
    RegistrationPhase,
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
    "FeatureDefinition",
    "FeatureGeneration",
    "FeatureInstallContext",
    "FeatureRecord",
    "FeatureRuntime",
    "FeatureScope",
    "FeatureServiceDependencies",
    "FeatureState",
    "FeatureTransaction",
    "RegistrationPhase",
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
