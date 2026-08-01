/**
 * evaluateVersionUpdate 单测。
 *
 * 重点回归：点分版本号（如 "1.2.3"）必须能正确比较。
 * 原实现 extractComparableVersion 的正则 `\d+(?:\d+)+` 漏了 `\.`，
 * 导致任何点分版本都匹配不上 → 永远走 parse-failed 分支。
 */
import { describe, it, expect } from 'vitest';
import { evaluateVersionUpdate } from '../../src/main/version-compare';

describe('evaluateVersionUpdate', () => {
  it('same dotted version → same-version', () => {
    expect(evaluateVersionUpdate('1.2.3', '1.2.3')).toEqual({ shouldProcess: false, reason: 'same-version' });
  });

  it('treats v-prefix / case-insensitively as same', () => {
    expect(evaluateVersionUpdate('v1.2.3', 'V1.2.3')).toEqual({ shouldProcess: false, reason: 'same-version' });
  });

  it('server newer (dotted) → newer', () => {
    expect(evaluateVersionUpdate('2.0.0', '1.9.9')).toEqual({ shouldProcess: true, reason: 'newer' });
    expect(evaluateVersionUpdate('1.2.10', '1.2.9')).toEqual({ shouldProcess: true, reason: 'newer' });
  });

  it('server older (dotted) → not-newer', () => {
    expect(evaluateVersionUpdate('1.9.0', '2.0.0')).toEqual({ shouldProcess: false, reason: 'not-newer' });
    expect(evaluateVersionUpdate('1.2.3', '1.2.4')).toEqual({ shouldProcess: false, reason: 'not-newer' });
  });

  it('padded versions compare numerically, not lexically', () => {
    // 01.02.04 vs 01.02.03 → 4 > 3 → newer（不是字符串比较）
    expect(evaluateVersionUpdate('01.02.04', '01.02.03').reason).toBe('newer');
  });

  it('release ranks higher than a pre-release with the same numeric core (D7)', () => {
    // Previously the suffix was stripped, so 1.0.0 and 1.0.0-rc1 compared
    // equal and RC→release was NOT detected as newer. The fix preserves the
    // suffix: a release must rank higher than its pre-release.
    expect(evaluateVersionUpdate('1.0.0', '1.0.0-rc1')).toEqual({ shouldProcess: true, reason: 'newer' });
    // Reverse direction: an RC reported against an existing release is NOT newer.
    expect(evaluateVersionUpdate('1.0.0-rc1', '1.0.0')).toEqual({ shouldProcess: false, reason: 'not-newer' });
  });

  it('pre-release tiers order release > rc > beta > alpha (D7)', () => {
    // Same numeric core, tier climbs upward.
    expect(evaluateVersionUpdate('1.0.0-rc1', '1.0.0-beta1').reason).toBe('newer');
    expect(evaluateVersionUpdate('1.0.0-beta1', '1.0.0-alpha1').reason).toBe('newer');
    // release vs beta also newer.
    expect(evaluateVersionUpdate('1.0.0', '1.0.0-beta1').reason).toBe('newer');
    // Two equal pre-release tags on the same core → same-version path is hit
    // earlier (string equality), but via compare: rc1 vs rc1 is same.
    expect(evaluateVersionUpdate('1.0.0-rc2', '1.0.0-rc1').reason).toBe('newer');
  });

  it('missing server or reported version → missing-version', () => {
    expect(evaluateVersionUpdate(undefined, '1.0.0')).toEqual({ shouldProcess: true, reason: 'missing-version' });
    expect(evaluateVersionUpdate('1.0.0', '')).toEqual({ shouldProcess: true, reason: 'missing-version' });
  });

  it('garbage input → parse-failed (fallback to shouldProcess)', () => {
    expect(evaluateVersionUpdate('abc', '1.0.0')).toEqual({ shouldProcess: true, reason: 'parse-failed' });
  });
});
