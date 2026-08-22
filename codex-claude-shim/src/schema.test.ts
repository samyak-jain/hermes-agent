import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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

test("current workshop fixture catalog converts without dropping arguments", () => {
  const fixtureRoot = new URL(
    "../../fixtures/workshop-tool-schemas/",
    import.meta.url,
  );
  const manifest = JSON.parse(
    readFileSync(new URL("index.json", fixtureRoot), "utf8"),
  ) as { tools: Array<{ file: string }> };

  for (const entry of manifest.tools) {
    const tool = JSON.parse(
      readFileSync(new URL(entry.file, fixtureRoot), "utf8"),
    ) as {
      name: string;
      parameters: JsonSchemaFixture;
    };
    const required = new Set(tool.parameters.required ?? []);
    const sample = Object.fromEntries(
      Object.entries(tool.parameters.properties ?? {})
        .filter(([name]) => required.has(name))
        .map(([name, property]) => [name, sampleValue(property)]),
    );
    assert.deepEqual(
      jsonSchemaToZod(tool.parameters).parse(sample),
      sample,
      tool.name,
    );
  }

  const renderUi = JSON.parse(
    readFileSync(new URL("renderUI.json", fixtureRoot), "utf8"),
  ) as { parameters: JsonSchemaFixture };
  const renderArgs = {
    jsx: '<Input value={bind("form.name")} />',
    state: { form: { name: "Hermes" } },
  };
  assert.deepEqual(
    jsonSchemaToZod(renderUi.parameters).parse(renderArgs),
    renderArgs,
  );
});

interface JsonSchemaFixture {
  type?: string;
  enum?: unknown[];
  required?: string[];
  properties?: Record<string, JsonSchemaFixture>;
  items?: JsonSchemaFixture;
}

function sampleValue(schema: JsonSchemaFixture): unknown {
  if (schema.enum?.length) return schema.enum[0];
  switch (schema.type) {
    case "string":
      return "fixture-value";
    case "integer":
    case "number":
      return 1;
    case "boolean":
      return true;
    case "array":
      return [sampleValue(schema.items ?? {})];
    case "object":
      return Object.fromEntries(
        Object.entries(schema.properties ?? {}).map(([name, property]) => [
          name,
          sampleValue(property),
        ]),
      );
    default:
      return null;
  }
}
