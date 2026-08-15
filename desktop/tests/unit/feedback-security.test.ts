import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  FeedbackService,
  type FeedbackConsentContext,
} from '../../src/main/feedback-service';
import {
  FeedbackCancelArgs,
  FeedbackImageArgs,
  FeedbackPreviewArgs,
  FeedbackSubmitArgs,
} from '../../src/shared/ipc-schemas';
import { isIpcInvokeChannel } from '../../src/shared/ipc-channels';

const CONTEXT: FeedbackConsentContext = {
  desktopSessionId: 'desktop-session-a',
  origin: 'file:///opt/ace/desktop/assets/index.html',
  ownerId: 'email:user@example.test',
  webContentsId: 7,
};
const OTHER_CONTEXT: FeedbackConsentContext = {
  ...CONTEXT,
  origin: 'file:///opt/ace/desktop/assets/other.html',
  webContentsId: 8,
};
const PNG_DATA_URL = 'data:image/png;base64,iVBORw0KGgo=';

function draft(overrides: Record<string, unknown> = {}) {
  return {
    title: 'Rendering bug',
    description: 'The preview is blank.',
    images: [{ name: 'screen.png', dataUrl: PNG_DATA_URL }],
    ...overrides,
  };
}

function successfulResponse(): Response {
  return new Response(JSON.stringify({
    resultCode: '000000',
    resultDesc: 'accepted',
  }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

function configuredService(options: ConstructorParameters<typeof FeedbackService>[0] = {}) {
  const service = new FeedbackService(options);
  service.setBaseURL('https://feedback.example/api');
  return service;
}

function authorize(
  service: FeedbackService,
  payload = draft(),
  context = CONTEXT,
) {
  const preview = service.createPreview(payload, context);
  expect(preview.success).toBe(true);
  if (!preview.success) throw new Error(preview.message);
  const approval = service.approvePreview(preview.previewId, context);
  expect(approval.success).toBe(true);
  if (!approval.success) throw new Error(approval.message);
  return {
    ...approval,
    approvedPayload: approval.payload,
    payload: {
      ...payload,
      title: approval.payload.title,
      description: approval.payload.description,
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('feedback network boundary', () => {
  it('accepts only credential-free HTTPS service origins', () => {
    const service = new FeedbackService();

    expect(() => service.setBaseURL('http://feedback.example')).toThrow(/HTTPS/);
    expect(() => service.setBaseURL('https://user:pass@feedback.example')).toThrow(/HTTPS/);
    expect(() => service.setBaseURL('https://feedback.example?target=other')).toThrow(/HTTPS/);
    expect(() => service.setBaseURL('https://feedback.example/api')).not.toThrow();
  });

  it('rejects renderer-supplied absolute image URLs before fetch', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService();

    const result = await service.getFeedbackImage('https://attacker.example/secret.png');

    expect(result.success).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('keeps relative image fetches on the configured origin and rejects redirects', async () => {
    const fetchMock = vi.fn(async () => new Response(
      new Uint8Array([1, 2, 3]),
      { status: 200, headers: { 'content-type': 'image/png' } },
    ));
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService();

    const result = await service.getFeedbackImage('upload/image.png');

    expect(result.success).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      'https://feedback.example/api/upload/image.png',
      expect.objectContaining({ method: 'GET', redirect: 'error' }),
    );
  });

  it('rejects oversized responses before buffering their body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('x', {
      status: 200,
      headers: {
        'content-length': String(11 * 1024 * 1024),
        'content-type': 'image/png',
      },
    })));
    const service = configuredService();

    const result = await service.getFeedbackImage('upload/image.png');

    expect(result.success).toBe(false);
    expect(result.message).toMatch(/过大/);
  });
});

describe('feedback consent authority', () => {
  it('does not preview, persist, or upload when no endpoint is configured', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const service = new FeedbackService();
    service.setBaseURL('');

    const preview = service.createPreview(draft(), CONTEXT);
    const bypass = await service.submitFeedback({
      ...draft(),
      authority: 'forged-authority',
    }, CONTEXT);

    expect(preview.success).toBe(false);
    expect(bypass.success).toBe(false);
    expect(service.readTrace(CONTEXT)).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects direct IPC/service bypass without a main-issued authority', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService();

    const result = await service.submitFeedback({
      ...draft(),
      authority: 'renderer-forged',
    }, CONTEXT);

    expect(result.success).toBe(false);
    expect(result.message).toMatch(/授权|同意/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('previews without uploading and consumes one approval exactly once', async () => {
    const fetchMock = vi.fn(async () => successfulResponse());
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService();
    const approval = authorize(service);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(approval.approvedPayload).not.toHaveProperty('images');
    const args = { ...approval.payload, authority: approval.authority };
    const [first, replay] = await Promise.all([
      service.submitFeedback(args, CONTEXT),
      service.submitFeedback(args, CONTEXT),
    ]);

    expect(first.success).toBe(true);
    expect(replay.success).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('rejects payload mutation and invalidates the authority before upload', async () => {
    const fetchMock = vi.fn(async () => successfulResponse());
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService();
    const approval = authorize(service);

    const mutated = await service.submitFeedback({
      ...approval.payload,
      title: `${approval.payload.title} mutated`,
      authority: approval.authority,
    }, CONTEXT);
    const originalAfterMutation = await service.submitFeedback({
      ...approval.payload,
      authority: approval.authority,
    }, CONTEXT);

    expect(mutated.success).toBe(false);
    expect(originalAfterMutation.success).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects expired and cross-session/origin approvals', async () => {
    let now = 10_000;
    const fetchMock = vi.fn(async () => successfulResponse());
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService({ now: () => now, consentTtlMs: 1_000 });
    const wrongContextApproval = authorize(service);

    const wrongContext = await service.submitFeedback({
      ...wrongContextApproval.payload,
      authority: wrongContextApproval.authority,
    }, OTHER_CONTEXT);
    const expiryApproval = authorize(service);
    now += 1_001;
    const expired = await service.submitFeedback({
      ...expiryApproval.payload,
      authority: expiryApproval.authority,
    }, CONTEXT);

    expect(wrongContext.success).toBe(false);
    expect(expired.success).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rechecks enterprise policy immediately before upload', async () => {
    let disabled = false;
    const fetchMock = vi.fn(async () => successfulResponse());
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService({ feedbackDisabled: () => disabled });
    const approval = authorize(service);
    disabled = true;

    const result = await service.submitFeedback({
      ...approval.payload,
      authority: approval.authority,
    }, CONTEXT);

    expect(result.success).toBe(false);
    expect(result.message).toMatch(/策略|禁用/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('cancels before submit without upload and rejects later replay', async () => {
    const fetchMock = vi.fn(async () => successfulResponse());
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService();
    const approval = authorize(service);

    const cancel = service.cancelFeedback(approval.authority, CONTEXT);
    const result = await service.submitFeedback({
      ...approval.payload,
      authority: approval.authority,
    }, CONTEXT);

    expect(cancel.canceled).toBe(true);
    expect(result.success).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('aborts an in-flight cancellation and never retries in background', async () => {
    let markStarted!: () => void;
    const started = new Promise<void>((resolve) => { markStarted = resolve; });
    const fetchMock = vi.fn((_url: string | URL | Request, init?: RequestInit) => (
      new Promise<Response>((_resolve, reject) => {
        markStarted();
        init?.signal?.addEventListener('abort', () => {
          reject(new DOMException('cancelled', 'AbortError'));
        }, { once: true });
      })
    ));
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService();
    const approval = authorize(service);

    const pending = service.submitFeedback({
      ...approval.payload,
      authority: approval.authority,
    }, CONTEXT);
    await started;
    const cancel = service.cancelFeedback(approval.authority, CONTEXT);
    const result = await pending;
    await Promise.resolve();

    expect(cancel.canceled).toBe(true);
    expect(result).toMatchObject({ success: false, canceled: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps cancellation active after response headers while the body stalls', async () => {
    let markBodyOpen!: () => void;
    const bodyOpen = new Promise<void>((resolve) => { markBodyOpen = resolve; });
    const fetchMock = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          markBodyOpen();
          init?.signal?.addEventListener('abort', () => {
            controller.error(new DOMException('cancelled', 'AbortError'));
          }, { once: true });
        },
      });
      return new Response(body, {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService();
    const approval = authorize(service, draft({ images: undefined }));

    const pending = service.submitFeedback({
      ...approval.payload,
      authority: approval.authority,
    }, CONTEXT);
    await bodyOpen;
    service.cancelFeedback(approval.authority, CONTEXT);
    const result = await pending;

    expect(result).toMatchObject({ success: false, canceled: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('spends authority on failure, redacts error secrets, and does not retry', async () => {
    const fetchMock = vi.fn(async () => {
      throw new Error(
        'connect HTTPS_PROXY=https://user:proxy-password-canary@proxy.example/?token=query-secret-canary',
      );
    });
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService();
    const approval = authorize(service);
    const args = { ...approval.payload, authority: approval.authority };

    const failed = await service.submitFeedback(args, CONTEXT);
    const replay = await service.submitFeedback(args, CONTEXT);

    expect(failed.success).toBe(false);
    expect(JSON.stringify(failed)).not.toContain('proxy-password-canary');
    expect(JSON.stringify(failed)).not.toContain('query-secret-canary');
    expect(replay.success).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('redacts proxy and credential canaries before preview, multipart, and trace', async () => {
    const fetchMock = vi.fn(async () => successfulResponse());
    vi.stubGlobal('fetch', fetchMock);
    vi.stubEnv(
      'HTTPS_PROXY',
      'https://env-user:env-proxy-password-canary@proxy.example/?token=env-query-canary',
    );
    const logSpies = [
      vi.spyOn(console, 'info').mockImplementation(() => undefined),
      vi.spyOn(console, 'warn').mockImplementation(() => undefined),
      vi.spyOn(console, 'error').mockImplementation(() => undefined),
    ];
    const service = configuredService({ traceLimit: 8 });
    const secretDraft = draft({
      title: 'HTTPS_PROXY=https://user:proxy-password-canary@proxy.example/?token=query-secret-canary',
      description: 'Authorization: Bearer bearer-secret-canary api_key=api-secret-canary',
    });
    const approval = authorize(service, secretDraft);

    expect(JSON.stringify(approval.payload)).not.toMatch(
      /proxy-password-canary|query-secret-canary|bearer-secret-canary|api-secret-canary/,
    );
    const result = await service.submitFeedback({
      ...approval.payload,
      authority: approval.authority,
    }, CONTEXT);
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const multipart = await new Response(init.body as BodyInit).text();
    const trace = JSON.stringify(service.readTrace(CONTEXT));
    const logs = JSON.stringify(logSpies.flatMap((spy) => spy.mock.calls));

    expect(result.success).toBe(true);
    expect(multipart).not.toMatch(
      /proxy-password-canary|query-secret-canary|bearer-secret-canary|api-secret-canary|env-proxy-password-canary|env-query-canary/,
    );
    expect(trace).not.toMatch(
      /proxy-password-canary|query-secret-canary|bearer-secret-canary|api-secret-canary|env-proxy-password-canary|env-query-canary/,
    );
    expect(logs).not.toMatch(
      /proxy-password-canary|query-secret-canary|bearer-secret-canary|api-secret-canary|env-proxy-password-canary|env-query-canary/,
    );
  });

  it('fails closed when temporary attachment cleanup cannot be verified', async () => {
    let cleanupCalls = 0;
    const fetchMock = vi.fn(async () => successfulResponse());
    vi.stubGlobal('fetch', fetchMock);
    const service = configuredService({
      cleanupAttachment: (buffer) => {
        cleanupCalls += 1;
        if (cleanupCalls > 1) throw new Error('cleanup failed');
        buffer.fill(0);
      },
    });
    const approval = authorize(service);

    const result = await service.submitFeedback({
      ...approval.payload,
      authority: approval.authority,
    }, CONTEXT);

    expect(result.success).toBe(false);
    expect(result.message).toMatch(/清理|安全/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('bounds the owner-scoped in-memory trace and supports explicit clearing', () => {
    const service = configuredService({ traceLimit: 3 });
    for (let index = 0; index < 5; index += 1) {
      const preview = service.createPreview(draft({ title: `bug-${index}` }), CONTEXT);
      expect(preview.success).toBe(true);
      if (preview.success) service.cancelPreview(preview.previewId, CONTEXT);
    }

    expect(service.readTrace(CONTEXT)).toHaveLength(3);
    expect(service.readTrace(OTHER_CONTEXT)).toEqual([]);
    service.clearTrace(CONTEXT);
    expect(service.readTrace(CONTEXT)).toEqual([]);
  });
});

describe('feedback IPC validation', () => {
  it('exposes preview/submit/cancel but no renderer approval channel', () => {
    expect(isIpcInvokeChannel('feedback:preview')).toBe(true);
    expect(isIpcInvokeChannel('feedback:submit')).toBe(true);
    expect(isIpcInvokeChannel('feedback:cancel')).toBe(true);
    expect(isIpcInvokeChannel('feedback:approve')).toBe(false);
  });

  it('permits only same-origin relative image paths', () => {
    expect(FeedbackImageArgs.parse({ path: 'upload/image.png' }).ok).toBe(true);
    expect(FeedbackImageArgs.parse({ path: 'https://attacker.example/image.png' }).ok).toBe(false);
    expect(FeedbackImageArgs.parse({ path: '//attacker.example/image.png' }).ok).toBe(false);
    expect(FeedbackImageArgs.parse({ path: 'upload\\image.png' }).ok).toBe(false);
  });

  it('requires an authority only for submit and validates cancel tokens', () => {
    expect(FeedbackPreviewArgs.parse(draft()).ok).toBe(true);
    expect(FeedbackSubmitArgs.parse(draft()).ok).toBe(false);
    expect(FeedbackSubmitArgs.parse({ ...draft(), authority: 'authority-token' }).ok).toBe(true);
    expect(FeedbackCancelArgs.parse({ authority: 'authority-token' }).ok).toBe(true);
    expect(FeedbackCancelArgs.parse({ authority: '' }).ok).toBe(false);
  });

  it('rejects multipart filenames with path or control characters', () => {
    const base = {
      ...draft(),
      authority: 'authority-token',
      images: [{ name: '../secret.png', dataUrl: PNG_DATA_URL }],
    };
    expect(FeedbackSubmitArgs.parse(base).ok).toBe(false);
    expect(FeedbackSubmitArgs.parse({
      ...base,
      images: [{ name: 'safe.png\r\nX-Test: injected', dataUrl: PNG_DATA_URL }],
    }).ok).toBe(false);
  });
});
