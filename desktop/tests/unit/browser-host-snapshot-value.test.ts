/** snapshotEditableValue：snapshot 行内附带可编辑节点当前值的纯函数单测。 */
import { describe, it, expect } from 'vitest';
import { describeHitNode, snapshotEditableValue } from '../../src/main/browser-host';

type AxValue = { value: unknown };

function axNode(options: {
  editable?: boolean | string;
  value?: unknown;
}): { properties?: Array<{ name: string; value: AxValue }>; value?: AxValue } {
  return {
    ...(options.editable !== undefined
      ? { properties: [{ name: 'editable', value: { value: options.editable } }] }
      : {}),
    ...(options.value !== undefined ? { value: { value: options.value } } : {}),
  };
}

function domNode(attrs: Record<string, string>) {
  return {
    backendNodeId: 1,
    parentBackendNodeId: 0,
    nodeName: 'INPUT',
    attributes: new Map(Object.entries(attrs)),
  };
}

describe('snapshotEditableValue', () => {
  it('可编辑节点附带当前 value（截断到 100 字符）', () => {
    const node = axNode({ editable: true, value: 'ewc bin' }) as never;
    expect(snapshotEditableValue(node, domNode({ type: 'text' }) as never)).toBe(' value="ewc bin"');
    const long = axNode({ editable: true, value: `x${'y'.repeat(200)}` }) as never;
    const suffix = snapshotEditableValue(long, domNode({}) as never);
    expect(suffix.length).toBe(` value=${JSON.stringify('x'.padEnd(100, 'y'))}`.length);
    expect(snapshotEditableValue(axNode({ editable: 'plaintext', value: 'query' }) as never, domNode({}) as never)).toBe(' value="query"');
    expect(snapshotEditableValue(axNode({ editable: 'richtext', value: 'draft' }) as never, domNode({}) as never)).toBe(' value="draft"');
  });

  it('密码框一律省略 value', () => {
    const node = axNode({ editable: true, value: 'super-secret' }) as never;
    expect(snapshotEditableValue(node, domNode({ type: 'password' }) as never)).toBe('');
    expect(snapshotEditableValue(node, domNode({ type: 'PASSWORD' }) as never)).toBe('');
  });

  it('非可编辑节点或空值不附带', () => {
    expect(snapshotEditableValue(axNode({ editable: false, value: 'x' }) as never, domNode({}) as never)).toBe('');
    expect(snapshotEditableValue(axNode({ value: 'x' }) as never, domNode({}) as never)).toBe('');
    expect(snapshotEditableValue(axNode({ editable: true, value: '' }) as never, domNode({}) as never)).toBe('');
    expect(snapshotEditableValue(axNode({ editable: true }) as never, domNode({}) as never)).toBe('');
  });
});

describe('describeHitNode', () => {
  it('输出 tag#id.class 形态的遮挡元素描述', () => {
    expect(
      describeHitNode({
        nodeName: 'DIV',
        attributes: ['id', 'suggestion-layer', 'class', 'mask overlay hidden extra'],
      }),
    ).toBe('div#suggestion-layer.mask.overlay.hidden.extra');
  });

  it('缺属性时退化为 tag 名且截断', () => {
    expect(describeHitNode({ nodeName: 'SPAN' })).toBe('span');
    expect(describeHitNode({})).toBe('unknown');
    expect(describeHitNode({ nodeName: 'DIV', attributes: ['id', 'x'.repeat(200)] }).length).toBeLessThanOrEqual(160);
  });
});
