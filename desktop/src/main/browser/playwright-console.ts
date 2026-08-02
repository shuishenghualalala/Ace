import type { Page } from './playwright-compat';

export const CONSOLE_MESSAGE_LEVELS = [
  'error',
  'warning',
  'info',
  'debug',
] as const;

export type ConsoleMessageLevel = (typeof CONSOLE_MESSAGE_LEVELS)[number];

type ConsoleMessageType = Awaited<
  ReturnType<Page['consoleMessages']>
>[number]['type'] extends () => infer Type ? Type : never;

export interface ConsoleMessagesOptions {
  level: ConsoleMessageLevel;
  all: boolean;
}

export interface ConsoleMessagesResult {
  text: string;
  format: 'text';
  extension: 'log';
  total: number;
  errors: number;
  warnings: number;
  returned: number;
}

/**
 * Keep this mapping byte-for-byte equivalent in meaning to Playwright MCP's
 * `tab.ts#consoleLevelForMessageType`. Chromium has more console message
 * types than four severity names, so filtering the raw type string would
 * silently lose assert/group/timing messages.
 */
function levelForMessageType(type: ConsoleMessageType): ConsoleMessageLevel {
  switch (type) {
    case 'assert':
    case 'error':
      return 'error';
    case 'warning':
      return 'warning';
    case 'count':
    case 'dir':
    case 'dirxml':
    case 'info':
    case 'log':
    case 'table':
    case 'time':
    case 'timeEnd':
      return 'info';
    case 'clear':
    case 'debug':
    case 'endGroup':
    case 'profile':
    case 'profileEnd':
    case 'startGroup':
    case 'startGroupCollapsed':
    case 'trace':
      return 'debug';
    default:
      return 'info';
  }
}

export function shouldIncludeConsoleMessage(
  threshold: ConsoleMessageLevel,
  type: ConsoleMessageType,
): boolean {
  return CONSOLE_MESSAGE_LEVELS.indexOf(levelForMessageType(type))
    <= CONSOLE_MESSAGE_LEVELS.indexOf(threshold);
}

/**
 * Read Playwright's retained Page buffers directly.
 *
 * The count is intentionally always scoped to `since-navigation`, while the
 * returned list honors `all`. This slightly surprising behavior is part of the
 * upstream Playwright MCP output contract and is therefore preserved here.
 */
export async function readConsoleMessages(
  page: Page,
  options: ConsoleMessagesOptions,
): Promise<ConsoleMessagesResult> {
  const currentMessages = await page.consoleMessages({
    filter: 'since-navigation',
  });
  const currentErrors = await page.pageErrors({
    filter: 'since-navigation',
  });
  let errors = currentErrors.length;
  let warnings = 0;
  for (const message of currentMessages) {
    if (message.type() === 'error') errors += 1;
    else if (message.type() === 'warning') warnings += 1;
  }

  const filter = options.all ? 'all' : 'since-navigation';
  const messages = await page.consoleMessages({ filter });
  const rendered = messages
    .filter((message) => shouldIncludeConsoleMessage(options.level, message.type()))
    .map((message) => {
      const location = message.location();
      return `[${message.type().toUpperCase()}] ${message.text()} @ `
        + `${location.url}:${location.lineNumber}`;
    });
  if (shouldIncludeConsoleMessage(options.level, 'error')) {
    const pageErrors = await page.pageErrors({ filter });
    for (const errorOrValue of pageErrors as unknown[]) {
      rendered.push(
        errorOrValue instanceof Error
          ? (errorOrValue.stack || errorOrValue.message)
          : String(errorOrValue),
      );
    }
  }

  const total = currentMessages.length + currentErrors.length;
  const header = [
    `Total messages: ${total} (Errors: ${errors}, Warnings: ${warnings})`,
  ];
  if (rendered.length !== total) {
    header.push(
      `Returning ${rendered.length} messages for level "${options.level}"`,
    );
  }
  return {
    text: [...header, '', ...rendered].join('\n'),
    format: 'text',
    extension: 'log',
    total,
    errors,
    warnings,
    returned: rendered.length,
  };
}

export async function clearConsoleMessages(page: Page): Promise<void> {
  await Promise.all([
    page.clearConsoleMessages(),
    page.clearPageErrors(),
  ]);
}
