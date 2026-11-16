import { IPC_ARG_VALIDATION_FAILED } from '../shared/constants';
import { ShellOpenExternalArgs } from '../shared/ipc-schemas';

/** Share the validated shell capability between IPC and main-process navigation. */
export function createExternalLinkBridge() {
  let opener: ((url: string) => Promise<void>) | null = null;

  const open = async (raw: unknown): Promise<void> => {
    const parsed = ShellOpenExternalArgs.parse(raw);
    if (!parsed.ok) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:openExternal ${parsed.error}`);
    }
    if (!opener) throw new Error('External link handler is not registered');
    await opener(parsed.value.url);
  };

  return {
    openUrl: (url: string): Promise<void> => open({ url }),
    register(openExternal: (url: string) => Promise<void>) {
      opener = openExternal;
      return (_event: unknown, raw: unknown): Promise<void> => open(raw);
    },
  };
}
