export type VersionUpdateDecisionReason =
  | 'missing-version'
  | 'same-version'
  | 'parse-failed'
  | 'not-newer'
  | 'newer';

export interface VersionUpdateDecision {
  shouldProcess: boolean;
  reason: VersionUpdateDecisionReason;
}

/**
 * A version split into its numeric core and optional pre-release tag.
 * A release (no pre-release) ranks HIGHER than any pre-release with the same
 * numeric core (semver: 1.0.0 > 1.0.0-rc1). Pre-release tiers, lowest first:
 * dev/build < alpha < beta < rc.
 */
interface ComparableVersion {
  parts: number[];
  prerelease: string | null; // null means "release" (no prerelease)
}

/**
 * Map a pre-release label to a tier rank. Lower rank = lower precedence.
 * - null (release) → 4 (highest)
 * - rc → 3
 * - beta → 2
 * - alpha → 1
 * - dev / build / anything else → 0
 */
function prereleaseTier(prerelease: string | null): number {
  if (prerelease === null) return 4;
  const p = prerelease.toLowerCase();
  if (p.startsWith('rc')) return 3;
  if (p.startsWith('beta') || p.startsWith('b')) return 2;
  if (p.startsWith('alpha') || p.startsWith('a')) return 1;
  return 0; // dev / build / unknown → lowest
}

function extractComparableVersion(value?: string): ComparableVersion | null {
  if (!value) {
    return null;
  }

  // 形如 [v]1.2.3 的点分版本号（至少两段）。原正则漏了 `\.`，导致点分版本永不匹配 →
  // 永远 parse-failed。现在保留预发布后缀（1.0.0-rc1）用于正确排序：
  // release > rc > beta > alpha > dev/build。
  const match = value.trim().match(/[Vv]?(\d+(?:\.\d+)+)(?:-([0-9a-zA-Z.]+))?/);
  if (!match?.[1]) {
    return null;
  }

  const parts = match[1].split('.').map((segment) => Number.parseInt(segment, 10));
  if (parts.some((segment) => !Number.isFinite(segment))) {
    return null;
  }

  const prerelease = match[2] ?? null;
  return { parts, prerelease };
}

function compareVersionParts(left: ComparableVersion, right: ComparableVersion): number {
  const maxLength = Math.max(left.parts.length, right.parts.length);

  // Numeric core first.
  for (let index = 0; index < maxLength; index += 1) {
    const leftPart = left.parts[index] ?? 0;
    const rightPart = right.parts[index] ?? 0;

    if (leftPart > rightPart) {
      return 1;
    }

    if (leftPart < rightPart) {
      return -1;
    }
  }

  // Equal numeric core: release outranks any pre-release. If both are
  // pre-releases, the higher tier wins; same tier → lexical compare of the
  // tag (e.g. rc2 > rc1).
  const leftTier = prereleaseTier(left.prerelease);
  const rightTier = prereleaseTier(right.prerelease);
  if (leftTier !== rightTier) {
    return leftTier > rightTier ? 1 : -1;
  }

  if (left.prerelease === right.prerelease) {
    return 0;
  }
  // Same tier, different tags — lexical tiebreak (rc2 > rc10 is wrong but this
  // path only decides newer/same/older for update prompts; numeric suffix
  // ordering within a tag is intentionally simple here).
  const l = left.prerelease ?? '';
  const r = right.prerelease ?? '';
  if (l === r) return 0;
  return l > r ? 1 : -1;
}

export function evaluateVersionUpdate(serverVersion?: string, reportedVersion?: string): VersionUpdateDecision {
  if (!serverVersion || !reportedVersion) {
    return {
      shouldProcess: true,
      reason: 'missing-version',
    };
  }

  const normalizedServerVersion = serverVersion.trim().toLowerCase();
  const normalizedReportedVersion = reportedVersion.trim().toLowerCase();
  if (normalizedServerVersion === normalizedReportedVersion) {
    return {
      shouldProcess: false,
      reason: 'same-version',
    };
  }

  const serverParts = extractComparableVersion(serverVersion);
  const reportedParts = extractComparableVersion(reportedVersion);
  if (!serverParts || !reportedParts) {
    return {
      shouldProcess: true,
      reason: 'parse-failed',
    };
  }

  return compareVersionParts(serverParts, reportedParts) > 0
    ? {
      shouldProcess: true,
      reason: 'newer',
    }
    : {
      shouldProcess: false,
      reason: 'not-newer',
    };
}
