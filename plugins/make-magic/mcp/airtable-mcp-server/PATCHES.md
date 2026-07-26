# PATCHES — fork delta vs upstream `airtable-mcp-server` 1.13.0

This directory vendors [`domdomegg/airtable-mcp-server`](https://github.com/domdomegg/airtable-mcp-server)
**1.13.0** with a small local fork. This file documents every intentional deviation from upstream so
the delta stays auditable and re-applyable on a future upstream bump.

Base version: **1.13.0** (`package.json` `version` unchanged).

---

## 1. `fields` projection on `list_records` (feature fork — MUST be preserved)

**What:** `list_records` accepts an optional `fields: string[]` argument. When provided, the server
appends one `fields[]=<name>` query param per requested field to the Airtable
`GET /v0/{baseId}/{tableId}` request, so Airtable returns **only** those columns instead of every
field. This dramatically reduces payload size for wide tables (e.g. a Magic card catalog).

**Why:** upstream 1.13.0's `list_records` returns all fields for every record. Skills in this plugin
frequently need one or two columns from very wide tables; without projection each call transfers
large, mostly-unused payloads.

**Files touched vs upstream:**

- `src/tools/list-records.ts` — adds the `fields` entry to the tool `inputSchema`
  (`z.array(z.string()).optional()`) and passes `args.fields` through to
  `airtableService.listRecords(...)`.
- `src/types.ts` — `ListRecordsOptions` gains `fields?: string[] | undefined`.
- `src/airtableService.ts` — in `listRecords`, when `options.fields?.length`, loops and calls
  `queryParams.append('fields[]', field)` for each requested field.

**Regression guard:** `src/airtableService.test.ts` →
`listRecords › handles fields projection option (fork)` asserts the request URL contains
`fields%5B%5D=Name` and `fields%5B%5D=Set` when `fields: ['Name', 'Set']` is passed. Run:
`npx vitest run src/airtableService.test.ts`.

**Evidence in the shipped bundle:** `grep -a 'fields\[\]' dist/server.js` →
`append("fields[]", field);` (the projection loop survives bundling).

> When re-basing on a newer upstream, re-apply this projection to all three files and keep the
> regression test green.

---

## 2. stdio-only — HTTP/Streamable transport removed (packaging fork)

**What:** the server is now **stdio-only**. The upstream HTTP transport path was deleted from
`src/main.ts`:

- Removed the `import express from 'express'` and
  `import {StreamableHTTPServerTransport} from '@modelcontextprotocol/sdk/server/streamableHttp.js'`.
- Removed the `MCP_TRANSPORT === 'http'` branch (the Express app, `POST /mcp` handler,
  per-request server/transport construction, `app.listen`, and the "no authentication" warning).
- `main.ts` now unconditionally connects a single `StdioServerTransport`.

**Also removed:**

- `express` from `package.json` `dependencies`; `@types/express` from `devDependencies`.
- The `start:http` npm script.
- The `describe('HTTP transport stateless mode', ...)` block in `src/e2e.test.ts` (which spawned
  `node dist/main.js` with `MCP_TRANSPORT=http` and waited for an HTTP URL that stdio-only `main.ts`
  never prints — a guaranteed timeout under `npm test`). Its sole-use imports `Client`
  (`.../client/index.js`) and `StreamableHTTPClientTransport` (`.../client/streamableHttp.js`), plus
  the now-unused `beforeAll`/`afterAll` from `vitest`, were dropped with it. The remaining e2e blocks
  (InMemory / MCP Bundle / Docker, gated by `Boolean(AIRTABLE_API_KEY)` or `RUN_*` env vars) are
  intact.

**Why:** this plugin launches the MCP server locally over stdio (via `.mcp.json`). The HTTP transport
was unused surface area. Upstream itself warns the HTTP transport has **no authentication** and must
sit behind a reverse proxy — undesirable to ship in a turnkey local plugin.

**Security note — mooted upstream HTTP fix:** upstream's stateless HTTP handler builds a fresh
`server` + `StreamableHTTPServerTransport` **per request** specifically to avoid response misrouting
on concurrent requests with colliding JSON-RPC IDs (`GHSA-345p-7cg4-v4c7`). By removing the HTTP path
entirely, that vulnerability class **does not apply** to this fork — there is no shared HTTP transport
and no `express` in the dependency tree or the shipped bundle.

**Preserved:** the deprecated positional API-key CLI arg warning and the `AIRTABLE_API_KEY` env-var
path are untouched. Signal handlers (SIGINT/SIGTERM cleanup) are retained.

---

## 3. Single-file bundle via esbuild (packaging)

**What:** added `esbuild` (devDependency) and a `bundle` npm script that emits one self-contained
file, `dist/server.js`, committed to the repo. The rest of `dist/` and `node_modules/` remain
gitignored; `.gitignore` was amended to un-ignore **only** `dist/server.js`.

**Bundle command:**

```
esbuild src/main.ts --bundle --platform=node --target=node20 --format=esm \
  --banner:js="import{createRequire as __cr}from'node:module';const require=__cr(import.meta.url);" \
  --outfile=dist/server.js
```

**ESM/CJS note:** `--format=esm` is used because `@modelcontextprotocol/sdk` is ESM (and `zod` is
dual). A `createRequire` banner is injected so any transitive CJS-interop `require(...)` resolves at
runtime under ESM output. Run: `AIRTABLE_API_KEY=<key> node dist/server.js`.

**No secrets in the bundle:** `dist/server.js` contains no `AIRTABLE_API_KEY` value and no `pat…`
token — the key is read from the environment at runtime only.

---

## Upstream files intentionally left in place

`src/` is kept in full (not just the bundle) so the fork remains buildable, testable, and
re-basable. `Dockerfile`, `smithery.yaml`, `manifest.json`, `server.json`, and `build-mcpb.sh` are
upstream packaging artifacts left untouched; they are not used by this plugin's stdio launch path.
