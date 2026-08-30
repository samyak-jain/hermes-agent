# Image provenance and runtime boundaries

Hermes carries an image through several trust and runtime boundaries. This
contract keeps the pixels available without broadening file access or leaking
embedded image data into ordinary logs.

## Inbound images

Messaging adapters download attachments into
`$HERMES_HOME/cache/images/`. The gateway validates and normalizes the file,
then selects either native routing or an auxiliary text description according
to the destination session's provider and model. Native routing constructs a
typed user message containing text and one image item per attachment. Empty
captions and multiple images are valid.

The Codex app-server adapter must preserve those typed items through
`turn/start`; it must never replace an image with a text marker. The Claude SDK
shim validates embedded image MIME, magic bytes, base64 encoding, and decoded
size before converting the item to a Claude image content block. Invalid
native images fail closed rather than silently becoming a text-only request.

## Tool-returned images

Host tools may return a multimodal envelope with text and image content. The
app-server adapter preserves that content, and the Claude SDK shim translates
it to MCP text and image blocks. Ordinary tool results remain text/JSON. Data
URLs are redacted in tool debug metadata and log labels; exact provider-request
logging remains the only intentionally configured location that may contain
the request body.

`vision_analyze` first uses the current agent's native vision when available.
Its availability is evaluated inside that agent's provider/model runtime,
including both initial tool construction and refresh. Auxiliary vision is a
fallback and its credentials do not gate the native path.

## Cached path authority

The gateway sees cached attachments at `$HERMES_HOME/cache/images/` (typically
`/opt/data/cache/images/`). A sandboxed child sees the same narrow read-only
tree at `/opt/hermes-cache/images/`. Delegation rewrites only paths under the
approved host cache root when building the child prompt. The secure image
resolver maps that projection back to the host cache for a remote child task;
it does not grant access to neighboring host paths.

Images created inside the sandbox remain accessible through the existing
sandbox resolver. No broad home-directory or EFS mount is required. For image
generation edits, local reference images are read through the same resolver
and converted to data URLs before being sent to Fal; host or sandbox paths are
never sent to the remote API as if it could read them.

## Operational checks

Regression coverage should retain all of these properties:

- image-only and multi-image user turns contain real image blocks;
- malformed, oversized, or MIME-mismatched embedded images fail safely;
- resizing, compaction, and session continuation preserve valid requests;
- Lena and delegated children can inspect the same approved cached image;
- child tool construction and refresh use the child's runtime; and
- paths outside approved caches stay confined to the sandbox.

