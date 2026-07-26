import { z } from "zod";

type JsonSchema = Record<string, any>;

function enumSchema(values: unknown[]): z.ZodTypeAny {
  const literals = values.map((value) => z.literal(value as any));
  if (literals.length === 0) return z.any();
  if (literals.length === 1) return literals[0];
  return z.union(
    literals as unknown as [z.ZodTypeAny, z.ZodTypeAny, ...z.ZodTypeAny[]],
  );
}

export function jsonSchemaToZod(schema: JsonSchema = {}): z.ZodTypeAny {
  let result: z.ZodTypeAny;
  if (Array.isArray(schema.enum)) {
    result = enumSchema(schema.enum);
  } else if (Array.isArray(schema.anyOf) && schema.anyOf.length > 0) {
    const variants = schema.anyOf.map((entry: JsonSchema) => jsonSchemaToZod(entry));
    result = variants.length === 1
      ? variants[0]
      : z.union(variants as [z.ZodTypeAny, z.ZodTypeAny, ...z.ZodTypeAny[]]);
  } else if (Array.isArray(schema.oneOf) && schema.oneOf.length > 0) {
    const variants = schema.oneOf.map((entry: JsonSchema) => jsonSchemaToZod(entry));
    result = variants.length === 1
      ? variants[0]
      : z.union(variants as [z.ZodTypeAny, z.ZodTypeAny, ...z.ZodTypeAny[]]);
  } else {
    switch (schema.type) {
      case "string":
        result = z.string();
        break;
      case "integer":
        result = z.number().int();
        break;
      case "number":
        result = z.number();
        break;
      case "boolean":
        result = z.boolean();
        break;
      case "array":
        result = z.array(jsonSchemaToZod(schema.items ?? {}));
        break;
      case "object":
        {
          const objectSchema = z.object(jsonSchemaShape(schema));
          if (
            schema.additionalProperties
            && typeof schema.additionalProperties === "object"
          ) {
            result = objectSchema.catchall(
              jsonSchemaToZod(schema.additionalProperties),
            );
          } else if (schema.additionalProperties === true) {
            result = objectSchema.catchall(z.any());
          } else {
            result = objectSchema;
          }
        }
        break;
      default:
        result = z.any();
    }
  }
  if (typeof schema.description === "string" && schema.description) {
    result = result.describe(schema.description);
  }
  return result;
}

export function jsonSchemaShape(
  schema: JsonSchema = {},
): Record<string, z.ZodTypeAny> {
  const required = new Set<string>(Array.isArray(schema.required) ? schema.required : []);
  const shape: Record<string, z.ZodTypeAny> = {};
  for (const [name, property] of Object.entries(schema.properties ?? {})) {
    const converted = jsonSchemaToZod(property as JsonSchema);
    shape[name] = required.has(name) ? converted : converted.optional();
  }
  return shape;
}
