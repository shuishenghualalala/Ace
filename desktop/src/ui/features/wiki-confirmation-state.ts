import { backendApi } from '../backend-client';

const dismissed = new Set<string>();

export function dismissWikiConfirmation(id: string): void {
  dismissed.add(id);
}

export function restoreWikiConfirmation(id: string): void {
  dismissed.delete(id);
}

export async function revealPendingWikiConfirmation(id: string, element: HTMLElement): Promise<void> {
  if (dismissed.has(id)) return;
  try {
    const status = await backendApi.wikiConfirmationStatus(id);
    if (status.pending && !dismissed.has(id)) element.setAttribute('aria-hidden', 'false');
  } catch {
    // An offline or restarted gateway must not replay a historical approval.
  }
}
