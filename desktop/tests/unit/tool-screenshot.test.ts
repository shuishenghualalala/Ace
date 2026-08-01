/** tool-screenshot：browser_use screenshot 结果提取与 crew-file URL 拼装单测。 */
import { describe, it, expect } from 'vitest';
import {
  crewFileUrl,
  imageDisplayUrl,
  isAbsoluteLocalPath,
  screenshotResultPath,
} from '../../src/ui/tool-screenshot';

describe('screenshotResultPath', () => {
  it('提取 screenshot action 的图片路径', () => {
    expect(
      screenshotResultPath({
        name: 'browser_use',
        args: '{"action": "screenshot", "filename": "a.png"}',
        result: '/home/u/.Crew/accounts/acct_x/task_workspaces/default/downloads/browser/a.png',
      }),
    ).toBe('/home/u/.Crew/accounts/acct_x/task_workspaces/default/downloads/browser/a.png');
  });

  it('非 browser_use / 非 screenshot action / 非图片路径均不提取', () => {
    expect(
      screenshotResultPath({ name: 'terminal', args: '{"action": "screenshot"}', result: '/tmp/a.png' }),
    ).toBe('');
    expect(
      screenshotResultPath({ name: 'browser_use', args: '{"action": "navigate"}', result: '/tmp/a.png' }),
    ).toBe('');
    expect(
      screenshotResultPath({ name: 'browser_use', args: '{"action": "screenshot"}', result: '截图失败' }),
    ).toBe('');
    expect(
      screenshotResultPath({ name: 'browser_use', args: '{"action": "screenshot"}', result: '/tmp/a.txt' }),
    ).toBe('');
    expect(
      screenshotResultPath({
        name: 'browser_use',
        args: '{"action":"snapshot","text":"\\\"action\\\":\\\"screenshot\\\""}',
        result: '/tmp/a.png',
      }),
    ).toBe('');
    expect(
      screenshotResultPath({ name: 'browser_use', args: 'not-json', result: '/tmp/a.png' }),
    ).toBe('');
    expect(
      screenshotResultPath({ name: 'browser_use', args: '{"action":"screenshot"}', result: 'relative.png' }),
    ).toBe('');
  });

  it('接受 Windows 绝对截图路径', () => {
    expect(screenshotResultPath({
      name: 'browser_use',
      args: '{"action":"screenshot"}',
      result: 'C:\\Users\\u\\shot.png',
    })).toBe('C:\\Users\\u\\shot.png');
  });
});

describe('crewFileUrl', () => {
  it('绝对路径编码进协议 URL（带 host 占位段，空 host 会被 Chromium 判 Invalid URL）', () => {
    expect(crewFileUrl('/tmp/截图 1.png')).toBe(
      `crew-file://img/${encodeURIComponent('/tmp/截图 1.png')}`,
    );
  });

  it('本地绝对路径走私有协议，网络与 data URL 保持原样', () => {
    expect(isAbsoluteLocalPath('/tmp/a.png')).toBe(true);
    expect(isAbsoluteLocalPath('C:\\Users\\u\\a.png')).toBe(true);
    expect(isAbsoluteLocalPath('https://example.test/a.png')).toBe(false);
    expect(imageDisplayUrl('/tmp/a.png')).toBe(crewFileUrl('/tmp/a.png'));
    expect(imageDisplayUrl('data:image/png;base64,AA==')).toBe('data:image/png;base64,AA==');
  });
});
