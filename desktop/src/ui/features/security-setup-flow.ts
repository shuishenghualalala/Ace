import { notify } from '../state';
import { showConfirmDialog } from '../ui-feedback';

type UacStatus = { enabled?: boolean | null; detail?: string };
type UacEnableResult = {
  ok: boolean;
  exitCode?: number | null;
  detail?: string;
  restartRequired?: boolean;
};

export type UacPreparation = 'ready' | 'cancelled' | 'restart-required' | 'failed';

function resultDetail(result: UacEnableResult | undefined): string {
  return result?.detail || (result?.exitCode == null ? '未返回结果' : `退出码 ${result.exitCode}`);
}

export async function promptUacRestart(): Promise<UacPreparation> {
  await showConfirmDialog({
    title: 'UAC 已启用，请重启电脑',
    message: '系统安全设置已启用，但需要重启电脑后生效。请先重启电脑，再重新打开应用完成安全防护设置。重启前，受管命令暂不可用。',
    confirmText: '我知道了',
    cancelText: '稍后重启',
  });
  return 'restart-required';
}

/**
 * Ask once, enable UAC through an elevated main-process helper, then stop until reboot.
 * The registry write is never attempted without an explicit user confirmation.
 */
export async function enableUacAndPromptRestart(): Promise<UacPreparation> {
  const accepted = await showConfirmDialog({
    title: '需要启用系统安全设置',
    message: '检测到系统安全设置未启用。为保护系统并完成安全防护安装，应用需要请求管理员权限自动完成设置；设置生效前可能需要重启电脑。是否继续？',
    confirmText: '启用 UAC',
    cancelText: '取消',
  });
  if (!accepted) return 'cancelled';

  const result = (await window.Crew?.securityEnableUac?.()) as UacEnableResult | undefined;
  if (!result?.ok) {
    notify(`UAC 启用未完成：${resultDetail(result)}`);
    return 'failed';
  }
  if (result.restartRequired === false) return 'ready';
  return promptUacRestart();
}

export async function prepareWindowsSecuritySetup(): Promise<UacPreparation> {
  try {
    const status = (await window.Crew?.securityUacStatus?.()) as (UacStatus & { restartRequired?: boolean }) | undefined;
    if (status?.restartRequired) return promptUacRestart();
    if (status?.enabled !== false) return 'ready';
    return enableUacAndPromptRestart();
  } catch (error) {
    notify(`无法检查系统安全设置：${String(error)}`);
    return 'failed';
  }
}
