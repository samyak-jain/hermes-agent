import assert from "node:assert/strict";
import test from "node:test";
import { z } from "zod";
import { jsonSchemaShape, jsonSchemaToZod } from "./schema.js";

test("JSON schema conversion preserves required and optional tool arguments", () => {
  const shape = jsonSchemaShape({
    type: "object",
    required: ["action"],
    properties: {
      action: { type: "string", enum: ["add", "remove"] },
      limit: { type: "integer" },
    },
  });
  const schema = z.object(shape);
  assert.deepEqual(schema.parse({ action: "add" }), { action: "add" });
  assert.deepEqual(schema.parse({ action: "remove", limit: 2 }), {
    action: "remove",
    limit: 2,
  });
  assert.throws(() => schema.parse({}));
  assert.throws(() => schema.parse({ action: "other" }));
});

test("JSON schema conversion handles nested arrays and objects", () => {
  const schema = jsonSchemaToZod({
    type: "array",
    items: {
      type: "object",
      required: ["name"],
      properties: { name: { type: "string" }, enabled: { type: "boolean" } },
    },
  });
  assert.deepEqual(schema.parse([{ name: "memory", enabled: true }]), [
    { name: "memory", enabled: true },
  ]);
});
