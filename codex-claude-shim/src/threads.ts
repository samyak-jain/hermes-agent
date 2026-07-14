import { randomUUID } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import type { Query } from "@anthropic-ai/claude-agent-sdk";

export interface HostToolSchema {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
}

export interface ThreadState {
  threadId: string;
  hostSessionId?: string;
  claudeSessionId?: string;
  cwd?: string;
  model?: string;
  permissionMode?: string;
  systemPromptAppend?: string;
  systemPromptIdentity?: string;
  hostContextDelivered?: boolean;
  tools: HostToolSchema[];
  usageTotal: Usage;
  activeTurn?: {
    turnId: string;
    query: Query;
    abort: AbortController;
  };
}

export interface Usage {
  inputTokens: number;
  cachedInputTokens: number;
  outputTokens: number;
  reasoningOutputTokens: number;
  totalTokens: number;
}

interface PersistedThread {
  threadId: string;
  hostSessionId?: string;
  claudeSessionId?: string;
  cwd?: string;
  model?: string;
  systemPromptAppend?: string;
  systemPromptIdentity?: string;
  hostContextDelivered?: boolean;
}

const emptyUsage = (): Usage => ({
  inputTokens: 0,
  cachedInputTokens: 0,
  outputTokens: 0,
  reasoningOutputTokens: 0,
  totalTokens: 0,
});

export class ThreadStore {
  private threads = new Map<string, ThreadState>();
  private byHostSession = new Map<string, string>();

  constructor(
    private readonly persistencePath = join(
      process.env.HERMES_HOME ?? process.env.HOME ?? ".",
      ".claude-agent-bridge",
      "threads.json",
    ),
  ) {
    this.load();
  }

  create(options: Omit<ThreadState, "threadId" | "usageTotal" | "activeTurn">): ThreadState {
    const priorId = options.hostSessionId
      ? this.byHostSession.get(options.hostSessionId)
      : undefined;
    const prior = priorId ? this.threads.get(priorId) : undefined;
    if (prior) {
      prior.cwd = options.cwd ?? prior.cwd;
      prior.model = options.model ?? prior.model;
      prior.permissionMode = options.permissionMode;
      prior.tools = options.tools;
      prior.systemPromptAppend = prior.systemPromptAppend || options.systemPromptAppend;
      prior.systemPromptIdentity =
        prior.systemPromptIdentity || options.systemPromptIdentity;
      this.persist();
      return prior;
    }

    const state: ThreadState = {
      threadId: `thr_${randomUUID()}`,
      ...options,
      tools: options.tools ?? [],
      usageTotal: emptyUsage(),
    };
    this.threads.set(state.threadId, state);
    if (state.hostSessionId) this.byHostSession.set(state.hostSessionId, state.threadId);
    this.persist();
    return state;
  }

  resume(threadId: string, claudeSessionId?: string): ThreadState {
    let state = this.threads.get(threadId);
    if (!state) {
      state = { threadId, claudeSessionId, tools: [], usageTotal: emptyUsage() };
      this.threads.set(threadId, state);
    } else if (claudeSessionId) {
      state.claudeSessionId = claudeSessionId;
    }
    this.persist();
    return state;
  }

  bindClaudeSession(thread: ThreadState, claudeSessionId: string): void {
    if (thread.claudeSessionId === claudeSessionId) return;
    thread.claudeSessionId = claudeSessionId;
    this.persist();
  }

  markHostContextDelivered(thread: ThreadState): void {
    if (thread.hostContextDelivered) return;
    thread.hostContextDelivered = true;
    this.persist();
  }

  require(threadId: string): ThreadState {
    const state = this.threads.get(threadId);
    if (!state) throw new Error(`Unknown threadId: ${threadId}`);
    return state;
  }

  list(): ThreadState[] {
    return [...this.threads.values()];
  }

  private load(): void {
    if (!existsSync(this.persistencePath)) return;
    try {
      const parsed = JSON.parse(readFileSync(this.persistencePath, "utf8"));
      const persistenceVersion = Number(parsed.version ?? 1);
      for (const record of parsed.threads ?? []) {
        const persisted = record as PersistedThread;
        if (!persisted.threadId) continue;
        const state: ThreadState = {
          ...persisted,
          // Version 1 sessions embedded the complete host context in the
          // preset append. Start a fresh Claude session once so those threads
          // migrate onto the subscription-safe first-turn context layout.
          ...(persistenceVersion < 2
            ? { claudeSessionId: undefined, hostContextDelivered: false }
            : {}),
          tools: [],
          usageTotal: emptyUsage(),
        };
        this.threads.set(state.threadId, state);
        if (state.hostSessionId) this.byHostSession.set(state.hostSessionId, state.threadId);
      }
    } catch {
      // A corrupt cache must never prevent a new agent session.
    }
  }

  private persist(): void {
    const records: PersistedThread[] = [...this.threads.values()].map((thread) => ({
      threadId: thread.threadId,
      hostSessionId: thread.hostSessionId,
      claudeSessionId: thread.claudeSessionId,
      cwd: thread.cwd,
      model: thread.model,
      systemPromptAppend: thread.systemPromptAppend,
      systemPromptIdentity: thread.systemPromptIdentity,
      hostContextDelivered: thread.hostContextDelivered,
    }));
    mkdirSync(dirname(this.persistencePath), { recursive: true, mode: 0o700 });
    const temporary = `${this.persistencePath}.tmp-${process.pid}`;
    writeFileSync(temporary, JSON.stringify({ version: 2, threads: records }), {
      encoding: "utf8",
      mode: 0o600,
    });
    chmodSync(temporary, 0o600);
    renameSync(temporary, this.persistencePath);
  }
}
