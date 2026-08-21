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

test("JSON schema conversion preserves typed config arrays and reference maps", () => {
  const schema = jsonSchemaToZod({
    anyOf: [
      { type: "string" },
      {
        type: "array",
        items: {
          anyOf: [
            { type: "string" },
            { type: "number" },
            { type: "boolean" },
            { type: "null" },
          ],
        },
      },
      {
        type: "object",
        additionalProperties: { type: "string" },
      },
      { type: "null" },
    ],
  });

  const names = Array.from({ length: 16 }, (_, index) => `firecrawl_${index}`);
  assert.deepEqual(schema.parse(names), names);
  assert.deepEqual(
    schema.parse({ FIRECRAWL_API_KEY: "${FIRECRAWL_API_KEY}" }),
    { FIRECRAWL_API_KEY: "${FIRECRAWL_API_KEY}" },
  );
  assert.equal(schema.parse(null), null);
  assert.throws(() => schema.parse({ FIRECRAWL_API_KEY: 7 }));
  assert.throws(() => schema.parse(["firecrawl_map", { nested: true }]));
});
