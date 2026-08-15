import { describe, expect, it } from "vitest";
import { ClientProtocolIdentity } from "./ws";

describe("WebSocket client protocol identity", () => {
  it("adds an unspoofable sequence and fresh nonce to every frame", () => {
    let nonceIndex = 0;
    const identity = new ClientProtocolIdentity(
      () => `0000000000000000000000000000000${++nonceIndex}`,
    );

    const first = JSON.parse(identity.encode({
      kind: "pong",
      protocol_version: 99,
      client_sequence: 99,
      nonce: "caller-controlled",
    }));
    const second = JSON.parse(identity.encode({ kind: "pong" }));

    expect(first).toMatchObject({
      kind: "pong",
      protocol_version: 1,
      client_sequence: 1,
      nonce: "00000000000000000000000000000001",
    });
    expect(second).toMatchObject({
      kind: "pong",
      protocol_version: 1,
      client_sequence: 2,
      nonce: "00000000000000000000000000000002",
    });
  });

  it("starts a fresh sequence for each reconnected socket", () => {
    const identity = new ClientProtocolIdentity(
      () => "00000000000000000000000000000001",
    );
    identity.encode({ kind: "pong" });

    identity.reset();

    expect(JSON.parse(identity.encode({ kind: "pong" })).client_sequence).toBe(1);
  });
});
