/**
 * 渲染层错误上报单测：去抖（同 source+message 60s 一次）与 transport 隔离。
 */
import { describe, expect, it } from 'vitest';
import {
  createRendererErrorReporter,
  type RendererErrorReport,
} from '../../src/ui/renderer-error-report';

describe('createRendererErrorReporter', () => {
  it('dedupes identical reports within the window and re-sends after it elapses', () => {
    let now = 1_000;
    const sent: RendererErrorReport[] = [];
    const reporter = createRendererErrorReporter((report) => sent.push(report), () => now);

    reporter.report('render', new Error('boom'));
    reporter.report('render', new Error('boom'));
    reporter.report('render', new Error('boom'));
    expect(sent).toHaveLength(1);

    now += 59_999;
    reporter.report('render', new Error('boom'));
    expect(sent).toHaveLength(1);

    now += 1;
    reporter.report('render', new Error('boom'));
    expect(sent).toHaveLength(2);
  });

  it('different sources or messages are reported independently', () => {
    const now = 0;
    const sent: RendererErrorReport[] = [];
    const reporter = createRendererErrorReporter((report) => sent.push(report), () => now);

    reporter.report('render', new Error('a'));
    reporter.report('streaming-patch', new Error('a'));
    reporter.report('render', new Error('b'));
    expect(sent).toHaveLength(3);
  });

  it('a failing transport does not rethrow into the caller', () => {
    const reporter = createRendererErrorReporter(() => {
      throw new Error('transport dead');
    });
    expect(() => reporter.report('render', new Error('boom'))).not.toThrow();
  });

  it('non-Error values are stringified safely', () => {
    const sent: RendererErrorReport[] = [];
    const reporter = createRendererErrorReporter((report) => sent.push(report), () => 0);
    reporter.report('watchdog', 'plain string');
    reporter.report('watchdog', { weird: 'object' });
    expect(sent.map((r) => r.message)).toEqual(['plain string', '{"weird":"object"}']);
  });

  it('stack and context are carried and sliced', () => {
    const sent: RendererErrorReport[] = [];
    const reporter = createRendererErrorReporter((report) => sent.push(report), () => 0);
    const err = new Error('with stack');
    reporter.report('apply-chunk', err, { kind: 'delta', session_id: 's1' });
    expect(sent[0]).toMatchObject({
      source: 'apply-chunk',
      message: 'with stack',
      context: { kind: 'delta', session_id: 's1' },
    });
    expect(sent[0].stack).toContain('with stack');
  });
});
