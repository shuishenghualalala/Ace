import { describe, expect, it } from 'vitest';
import { formatToolDisplayName, toolDisplayTitle } from '../../src/ui/tool-labels';

describe('formatToolDisplayName', () => {
  it('maps built-in tools to Chinese labels', () => {
    expect(formatToolDisplayName('terminal')).toBe('终端执行');
    expect(formatToolDisplayName('file_read')).toBe('读文件');
    expect(formatToolDisplayName('file_write')).toBe('写文件');
    expect(formatToolDisplayName('skill_view')).toBe('查看技能');
  });

  it('falls back for unknown tools', () => {
    expect(formatToolDisplayName('custom_mcp_tool')).toBe('调用 custom mcp tool');
  });

  it('maps tool_search bridge tools to Chinese labels', () => {
    expect(formatToolDisplayName('tool_search')).toBe('搜索工具');
    expect(formatToolDisplayName('tool_describe')).toBe('查看工具详情');
    expect(formatToolDisplayName('tool_call')).toBe('调用工具');
  });
});

describe('toolDisplayTitle（过程时间线标题，对齐 web processDisplay）', () => {
  it('uiLabel 优先', () => {
    expect(toolDisplayTitle({ name: 'terminal', uiLabel: '查看目录', args: '{"command":"ls"}' }))
      .toBe('查看目录');
  });

  it('terminal/bash/process 拼 command 参数', () => {
    expect(toolDisplayTitle({ name: 'terminal', args: '{"command":"ls -lah ~/Desktop/"}' }))
      .toBe('运行 ls -lah ~/Desktop/');
    expect(toolDisplayTitle({ name: 'terminal' })).toBe('运行命令');
  });

  it('file_write / file_read / patch 拼 basename 路径', () => {
    expect(toolDisplayTitle({ name: 'file_write', args: '{"path":"/a/b/foo.ts"}' })).toBe('写入 foo.ts');
    expect(toolDisplayTitle({ name: 'file_read', args: '{"file_path":"docs/readme.md"}' })).toBe('读取 readme.md');
    expect(toolDisplayTitle({ name: 'patch', args: '{"path":"C:\\\\x\\\\bar.ts"}' })).toBe('修改 bar.ts');
    expect(toolDisplayTitle({ name: 'file_write' })).toBe('写入文件');
  });

  it('grep / glob 拼搜索词', () => {
    expect(toolDisplayTitle({ name: 'grep', args: '{"pattern":"TODO"}' })).toBe('搜索 TODO');
    expect(toolDisplayTitle({ name: 'glob', args: '{"query":"*.ts"}' })).toBe('搜索 *.ts');
    expect(toolDisplayTitle({ name: 'grep' })).toBe('搜索文件');
  });

  it('args 非 JSON 或缺字段时回退静态展示名', () => {
    expect(toolDisplayTitle({ name: 'terminal', args: 'not-json' })).toBe('运行命令');
    expect(toolDisplayTitle({ name: 'web_search' })).toBe('网页搜索');
    expect(toolDisplayTitle({ name: 'custom_mcp_tool' })).toBe('调用 custom mcp tool');
  });

  it('bridge 工具拼 query / 目标工具名', () => {
    expect(toolDisplayTitle({ name: 'tool_search', args: '{"query":"contacts"}' }))
      .toBe('搜索工具 contacts');
    expect(toolDisplayTitle({ name: 'tool_search' })).toBe('搜索工具');
    expect(toolDisplayTitle({ name: 'tool_describe', args: '{"name":"terminal"}' }))
      .toBe('查看工具 终端执行');
    expect(toolDisplayTitle({ name: 'tool_call', args: '{"name":"web_search","arguments":{}}' }))
      .toBe('调用 网页搜索');
  });

  it('bridge 目标工具完全未知时不叠加「调用」前缀', () => {
    expect(toolDisplayTitle({ name: 'tool_call', args: '{"name":"custom_mcp_tool"}' }))
      .toBe('调用 custom mcp tool');
    expect(toolDisplayTitle({ name: 'tool_describe', args: '{"name":"custom_mcp_tool"}' }))
      .toBe('查看工具 custom mcp tool');
  });
});
