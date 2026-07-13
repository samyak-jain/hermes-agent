import { createInterface } from "node:readline";

type Handler = (params: any) => Promise<unknown> | unknown;
type NotificationHandler = (params: any) => void;

export class RpcConnection {
  private handlers = new Map<string, Handler>();
  private notificationHandlers = new Map<string, NotificationHandler>();
  private pendingOutbound = new Map<
    number,
    { resolve: (value: any) => void; reject: (error: Error) => void }
  >();
  private nextOutboundId = 1;

  onRequest(method: string, handler: Handler): void {
    this.handlers.set(method, handler);
  }

  onNotification(method: string, handler: NotificationHandler): void {
    this.notificationHandlers.set(method, handler);
  }

  request<T = any>(method: string, params: unknown): Promise<T> {
    const id = this.nextOutboundId++;
    return new Promise<T>((resolve, reject) => {
      this.pendingOutbound.set(id, { resolve, reject });
      this.write({ id, method, params });
    });
  }

  notify(method: string, params?: unknown): void {
    this.write(params === undefined ? { method } : { method, params });
  }

  start(): void {
    const lines = createInterface({ input: process.stdin, terminal: false });
    lines.on("line", (line) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      let message: any;
      try {
        message = JSON.parse(trimmed);
      } catch {
        this.write({ id: null, error: { code: -32700, message: "Parse error" } });
        return;
      }
      void this.dispatch(message);
    });
    lines.on("close", () => process.exit(0));
  }

  private async dispatch(message: any): Promise<void> {
    if (message.id !== undefined && message.method === undefined) {
      const pending = this.pendingOutbound.get(message.id);
      if (!pending) return;
      this.pendingOutbound.delete(message.id);
      if (message.error) {
        pending.reject(new Error(`${message.error.code}: ${message.error.message}`));
      } else {
        pending.resolve(message.result);
      }
      return;
    }

    if (message.id === undefined && message.method) {
      this.notificationHandlers.get(message.method)?.(message.params);
      return;
    }

    const handler = this.handlers.get(message.method);
    if (!handler) {
      this.write({
        id: message.id,
        error: { code: -32601, message: `Method not found: ${message.method}` },
      });
      return;
    }
    try {
      const result = await handler(message.params);
      this.write({ id: message.id, result: result ?? {} });
    } catch (error: any) {
      this.write({
        id: message.id,
        error: {
          code: typeof error?.code === "number" ? error.code : -32603,
          message: error?.message ?? "Internal error",
        },
      });
    }
  }

  private write(value: unknown): void {
    process.stdout.write(`${JSON.stringify(value)}\n`);
  }
}

export class RpcMethodError extends Error {
  constructor(
    public readonly code: number,
    message: string,
  ) {
    super(message);
  }
}
