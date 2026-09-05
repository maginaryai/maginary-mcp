# maginary-mcp

Model Context Protocol server for [Maginary](https://maginary.ai) — enumerate the prompt-DSL flags the engine accepts, kick off generations, and poll for results, all from inside your MCP-compatible client (Claude Desktop, Cursor, Continue, custom).

## why

Maginary uses a Midjourney-style `--flag` prompt DSL over an async HTTP API. This server:

- surfaces the full parameter catalog to your LLM so it can pick the right flags
- offers a one-shot `generate` tool that hits `POST /api/gens/`
- offers `get_generation` + `wait_for_generation` for polling to a terminal state
- works offline for the catalog tools (ships a bundled snapshot; refreshed from the live docs endpoint at startup when reachable)

## install

```bash
uvx maginary-mcp                   # ephemeral run via uv
# — or —
pip install maginary-mcp
maginary-mcp
```

Requires Python 3.10+.

## configuration

Environment variables:

| var | default | meaning |
|---|---|---|
| `MAGINARY_API_KEY` | — | Bearer token from [app.maginary.ai/dashboard#api-keys](https://app.maginary.ai/dashboard#api-keys). **Required** for `generate` / `get_generation` / `wait_for_generation`. Catalog tools work without it. |
| `MAGINARY_BASE_URL` | `https://app.maginary.ai/api` | Override for staging or self-hosted. |
| `MAGINARY_PUBLIC_HOST` | `app.maginary.ai` | Hosted mode only. Sent to the backend as `X-Forwarded-Host` (with `-Proto`/`-For`) when `MAGINARY_BASE_URL` is an internal address, so the backend builds public URLs. |
| `MAGINARY_MCP_REQUIRE_AUTH` | off | Hosted mode only. On: every `/mcp` call needs a Bearer (OAuth token or API key); without one the server answers 401 + `WWW-Authenticate` pointing at `/.well-known/oauth-protected-resource`, which is how Claude/ChatGPT start the login. Trade-off: a wallet-only agent has no Bearer to send, so with the gate on it must make its first x402 payment over plain HTTP (`POST /api/gens/` returns an API key) and connect with that key; the 401 body says so. |
| `MAGINARY_OAUTH_ISSUER` | `https://app.maginary.ai/o` | The authorization server named in the protected-resource metadata (the backend, django-oauth-toolkit). |
| `MAGINARY_MCP_RESOURCE_URL` | `https://mcp.maginary.ai/mcp` | This server's canonical resource identifier (RFC 8707 audience). |
| `MAGINARY_MCP_LOG_LEVEL` | `INFO` | Standard Python log level; goes to stderr (stdout is reserved for MCP JSON-RPC). |

### Claude Desktop config

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your OS:

```json
{
  "mcpServers": {
    "maginary": {
      "command": "uvx",
      "args": ["maginary-mcp"],
      "env": {
        "MAGINARY_API_KEY": "sk-mag-…"
      }
    }
  }
}
```

## hosted (no-install) — Streamable HTTP

Connect a client straight to the hosted server at `https://mcp.maginary.ai/mcp`.
Zero install — the server is multi-tenant, so each request is scoped to
whatever credential it arrives with. Two ways to authenticate, pick whichever
fits the client:

**Connect (OAuth)** — for Claude Desktop, claude.ai, and any other client that
speaks MCP's OAuth spec. Add the server with no headers at all:

```json
{
  "mcpServers": {
    "maginary": { "url": "https://mcp.maginary.ai/mcp" }
  }
}
```

Click "Connect" in the client. It opens a login page on `app.maginary.ai`,
you sign in and approve the requested scopes, and the client holds the token
from then on — no key to generate or paste. Requires the server to be running
with `MAGINARY_MCP_REQUIRE_AUTH=1`; without it, no login is asked for at all.

**API key** — for any client that doesn't do the OAuth dance (or if you'd
rather not click through a login), generate a key at
[app.maginary.ai/dashboard#api-keys](https://app.maginary.ai/dashboard#api-keys)
and send it yourself:

```json
{
  "mcpServers": {
    "maginary": {
      "url": "https://mcp.maginary.ai/mcp",
      "headers": { "Authorization": "Bearer sk-mag-…" }
    }
  }
}
```

Both are equivalent once connected — same tools, same account. Catalog tools
work with no credential either way; `generate` / `get_generation` /
`wait_for_generation` need one. Run the hosted server yourself with:

### paying inside the tool call (x402 over MCP)

No key at all? Call `generate` anyway. Out of credits (or no account), the
result is `isError: true` with the x402 PaymentRequired at the top level
(`accepts`, `resource`, …) plus `error: "payment_required"`. An x402-capable
MCP client — the x402 SDK's `x402MCPSession` — signs `accepts[0]` and calls
the same tool again with the payment in `_meta["x402/payment"]`. The server
forwards it to the backend as `PAYMENT-SIGNATURE`; the backend verifies,
settles on Base and, for a wallet with no account, creates one. The settled
result carries the on-chain receipt in `_meta["x402/payment-response"]` and
`x402_receipt`, and a first settlement returns `x402_account: {api_key,
wallet}`. Pass that key as `_meta["maginary/api_key"]` on later calls
(polling needs it), or open a new connection with it as the Bearer header.
The server holds no payment logic; everything is decided by the backend's
`/api/gens/` contract.

### lost the key? recover it, no new payment

A key returned by `x402_account` is shown exactly once. If it's gone — the
agent never persisted it, or a human never wrote it down — paying again from
the *same* wallet does **not** hand back a second one: repeat payments just
add credits to the account. That's deliberate (an unbounded stream of fresh
keys from routine top-ups would be a bigger secret-exposure surface than
losing one, and would remove any reason to persist a key at all), so
recovery is a separate, explicit step: prove you hold the private key by
signing a short message, and the backend reissues a key.

```
POST https://app.maginary.ai/api/auth/x402/recover-key/
{
  "address": "0xYourWalletAddress",
  "timestamp": 1741000000,
  "signature": "0x..."
}
```

`signature` is a standard `personal_sign` (EIP-191 — the same call MetaMask,
ethers' `signer.signMessage(str)`, or `eth_account`'s `Account.sign_message`
already expose) over the literal string:

```
Maginary: issue a new API key for <address> at <timestamp>. This does not move funds.
```

with `<address>` lowercased and `<timestamp>` the same unix seconds sent in
the body. The timestamp must be within 5 minutes of the server's clock (30 s
of future skew tolerated) — it's the only replay defense, so a stale or
reused signature is rejected the same as a wrong one. A 200 revokes every
existing key on the account and returns exactly one fresh one, in the body
and in `X-Maginary-Api-Key` — same shape as `x402_account`, so code that
already handles the first-payment response handles this response too.

**This is a plain backend REST call, not an MCP tool** — deliberately, for
the same reason the payment logic itself lives in the backend and not here:
the server holds no identity logic of its own, and any agent that can
already construct and sign the x402 payment above can construct and sign
this one the same way. The one place this needs to be *discoverable* from
inside an MCP session is the 402 itself: an anonymous `generate` call always
comes back `payment_required` before the backend has any idea which wallet
is asking, so its `error` text always names this endpoint alongside the
payment instructions — an agent that gets stuck here learns about it from
the exact same message it already parses to learn about paying in the first
place, no separate discovery step.

```bash
pip install "maginary-mcp[http]"
maginary-mcp-http          # serves /mcp on 0.0.0.0:8642 (MAGINARY_MCP_PORT to change)
# — or —
docker build -t maginary-mcp . && docker run -p 8642:8642 maginary-mcp
```

The hosted server sets **no** `MAGINARY_API_KEY` (keys come per-request). Extra
env: `MAGINARY_MCP_HOST` (default `0.0.0.0`), `MAGINARY_MCP_PORT` (default `8642`).

## Claude Skill

The server ships an [Agent Skill](https://docs.claude.com/en/docs/agents/skills) that
teaches the `--flag` DSL, model selection, and the async generate→poll flow:

```bash
maginary-mcp --install-skill   # -> ~/.claude/skills/maginary-image-gen/SKILL.md
```

The skill stands on its own — hosts without MCP get the DSL plus the raw REST
calls (`POST /gens/` → poll). With the server connected, Claude instead calls
`search_parameters` for the authoritative flag list and `generate`/`wait_for_generation`
natively. Re-running updates it; local edits are protected unless you pass `--force`.
Source: [`src/maginary_mcp/SKILL.md`](src/maginary_mcp/SKILL.md).

## tools

### catalog (no auth)

- **`list_parameters(category?, status?, include_reserved=false)`** — enumerate the catalog
- **`search_parameters(query, category?, include_reserved=false)`** — text search over names / aliases / desc / examples
- **`get_parameter(name)`** — full record for one flag (canonical name or alias)

`list_parameters` responses include the `categories` / `statuses` taxonomy, and both
list/search responses carry `source` (`live` vs `bundled-snapshot`).

### generation (auth required)

- **`generate(prompt, callback_url?)`** — `POST /api/gens/`
- **`get_generation(uuid)`** — `GET /api/gens/{uuid}/`
- **`wait_for_generation(uuid, timeout_s=45)`** — poll to `done` / `failed`; a `timeout` result means still running — call again

## worked example

Inside an MCP-capable client, once configured:

> "Search the maginary catalog for anything about aspect ratio."

The LLM calls `search_parameters("aspect")` and gets back the `--ar` entry with values, examples, and supported models.

> "Now generate a cinematic portrait 16:9 with the flagship model."

The LLM calls `generate("a cinematic portrait --ar 16:9 --flagship")`, gets a `uuid`, then `wait_for_generation(uuid)` and reads `image_urls[]` out of the terminal record.

## catalog freshness

- **Live fetch** on startup from `https://maginary.ai/docs/parameters.json`, 5-second timeout.
- **Bundled snapshot** at `src/maginary_mcp/parameters_snapshot.json` used as a fallback whenever live fetch fails (no network, docs site down, etc.).
- The snapshot is refreshed manually by the maintainer via `python scripts/refresh_snapshot.py` — deliberately not baked into the wheel build so a new snapshot always corresponds to a reviewed commit.

The `source` field on `list_parameters` / `search_parameters` responses tells you which one is active.

## development

```bash
cd mcp
python -m venv venv && source venv/bin/activate
pip install -e .
maginary-mcp   # runs on stdio; kill with Ctrl+D
```

## license

MIT.
