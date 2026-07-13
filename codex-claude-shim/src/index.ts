#!/usr/bin/env node
import { randomUUID } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, symlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import { RpcConnection, RpcMethodError } from "./rpc.js";
import { ThreadStore } from "./threads.js";
import { runTurn } from "./turn.js";

if (process.argv.includes("--version")) {
  process.stdout.write("codex-cli 0.130.0\n");
  process.exit(0);
}

function ensureRuntimeSkillsVisible(): void {
  const home = process.env.HOME;
  const runtimeHome = process.env.HERMES_HOME ?? home;
  if (!home || !runtimeHome) return;
  const source = join(runtimeHome, "skills");
  const destination = join(home, ".claude", "skills");
  if (!existsSync(source)) return;
  mkdirSync(dirname(destination), { recursive: true, mode: 0o700 });
  if (existsSync(destination)) {
    if (lstatSync(destination).isSymbolicLink()) return;
    return;
  }
  symlinkSync(source, destination, "dir");
}

ensureRuntimeSkillsVisible();

const rpc = new RpcConnection();
const threads = new ThreadStore();
let initialized = false;
let initializeReceived = false;

rpc.onRequest("initialize", (params: any) => {
  if (initializeReceived) throw new RpcMethodError(-32600, "Already initialized");
  initializeReceived = true;
  return {
    userAgent: `codex-claude-shim/0.1.0 (${params?.clientInfo?.name ?? "client"})`,
    backend: "claude-agent-sdk",
    platformFamily: process.platform === "win32" ? "windows" : "unix",
    platformOs: process.platform,
  };
});

rpc.onNotification("initialized", () => {
  initialized = true;
});

function requireInitialized(): void {
  if (!initialized) throw new RpcMethodError(-32002, "Not initialized");
}

rpc.onRequest("thread/start", (params: any) => {
  requireInitialized();
  const thread = threads.create({
    cwd: params?.cwd,
    hostSessionId: params?.hostSessionId,
    model: params?.model,
    permissionMode: params?.permissionMode,
    systemPromptAppend: params?.systemPromptAppend,
    tools: Array.isArray(params?.tools) ? params.tools : [],
  });
  rpc.notify("thread/started", { thread: { id: thread.threadId } });
  return { thread: { id: thread.threadId } };
});

rpc.onRequest("thread/resume", (params: any) => {
  requireInitialized();
  if (!params?.threadId) throw new RpcMethodError(-32602, "threadId required");
  const thread = threads.resume(params.threadId, params?.claudeSessionId);
  if (params.cwd) thread.cwd = params.cwd;
  return { thread: { id: thread.threadId } };
});

rpc.onRequest("thread/loaded/list", () => {
  requireInitialized();
  return { threads: threads.list().map((thread) => ({ id: thread.threadId })) };
});

function extractUserText(input: any): string {
  if (!input) return "";
  if (typeof input === "string") return input;
  if (typeof input.message === "string") return input.message;
  const items = Array.isArray(input.items) ? input.items : input;
  if (!Array.isArray(items)) return "";
  return items
    .filter((item: any) => item?.type === "text" && typeof item.text === "string")
    .map((item: any) => item.text)
    .join("\n");
}

function launchTurn(options: Parameters<typeof runTurn>[0]): void {
  void runTurn(options).catch((error: any) => {
    options.thread.activeTurn = undefined;
    rpc.notify("turn/completed", {
      threadId: options.thread.threadId,
      turn: {
        id: options.turnId,
        status: "failed",
        error: { message: error?.message ?? String(error) },
      },
    });
  });
}

rpc.onRequest("turn/start", (params: any) => {
  requireInitialized();
  const thread = threads.require(params?.threadId);
  if (thread.activeTurn) {
    throw new RpcMethodError(-32000, "A turn is already running on this thread");
  }
  const userText = extractUserText(params?.input);
  if (!userText) throw new RpcMethodError(-32602, "input with user text required");
  const turnId = `turn_${randomUUID()}`;
  launchTurn({ rpc, threads, thread, turnId, userText });
  return { turn: { id: turnId } };
});

rpc.onRequest("thread/compact/start", (params: any) => {
  requireInitialized();
  const thread = threads.require(params?.threadId);
  if (thread.activeTurn) {
    throw new RpcMethodError(-32000, "A turn is already running on this thread");
  }
  const turnId = `turn_${randomUUID()}`;
  launchTurn({ rpc, threads, thread, turnId, userText: "/compact", compaction: true });
  return { turn: { id: turnId } };
});

rpc.onRequest("turn/interrupt", async (params: any) => {
  requireInitialized();
  const thread = threads.require(params?.threadId);
  const active = thread.activeTurn;
  if (!active) return { interrupted: false };
  active.abort.abort();
  try {
    await active.query.interrupt();
  } catch {
    // The SDK query may already be winding down.
  }
  return { interrupted: true };
});

rpc.onNotification("optOutNotificationMethods", () => {});
rpc.start();
