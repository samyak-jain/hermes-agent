import assert from "node:assert/strict";
import test from "node:test";
import { RpcConnection } from "./rpc.js";

test("turn teardown rejects matching pending outbound requests", async () => {
  const rpc = new RpcConnection();
  const originalWrite = process.stdout.write;
  process.stdout.write = (() => true) as typeof process.stdout.write;
  try {
    const pending = rpc.request("agent/tool/call", {
      turnId: "turn-dead",
      name: "write_file",
    });
    rpc.rejectPendingOutboundForTurn("turn-dead", "turn aborted");
    await assert.rejects(pending, /turn aborted/);
  } finally {
    process.stdout.write = originalWrite;
  }
});
