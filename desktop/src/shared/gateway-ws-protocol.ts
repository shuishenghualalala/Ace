const GATEWAY_WS_PROTOCOL_VERSION = 1;
const PROTOCOL_NONCE_RE = /^[A-Za-z0-9._~-]{16,128}$/;

function secureProtocolNonce(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

export class GatewayWsProtocolIdentity {
  private clientSequence = 0;

  constructor(private readonly nonceFactory: () => string = secureProtocolNonce) {}

  reset(): void {
    this.clientSequence = 0;
  }

  frame(payload: object): Record<string, unknown> {
    if (
      payload === null
      || Array.isArray(payload)
      || typeof payload !== 'object'
      || this.clientSequence >= Number.MAX_SAFE_INTEGER
    ) {
      throw new Error('invalid Gateway WebSocket protocol frame');
    }
    const nonce = this.nonceFactory();
    if (!PROTOCOL_NONCE_RE.test(nonce)) {
      throw new Error('invalid Gateway WebSocket protocol nonce');
    }
    this.clientSequence += 1;
    return {
      ...payload,
      protocol_version: GATEWAY_WS_PROTOCOL_VERSION,
      client_sequence: this.clientSequence,
      nonce,
    };
  }

  encode(payload: object): string {
    return JSON.stringify(this.frame(payload));
  }
}
