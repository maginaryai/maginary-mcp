# maginary-mcp — install guide for AI assistants

You are setting up the Maginary MCP server (AI image + video generation) for a user. Pick the first option that fits; do not ask the user to choose unless step 1 is impossible.

## Option 1 — hosted, nothing to install (preferred)

Add this server entry to the client's MCP configuration and finish:

```json
{
  "mcpServers": {
    "maginary": { "url": "https://mcp.maginary.ai/mcp" }
  }
}
```

- Cline / Cursor / VS Code / Windsurf: `type` is `streamableHttp` (or `http`) if the client asks for one.
- No credential is required to connect. Catalog tools work immediately.
- For generation the user needs an account. Either the user pastes an API key from https://app.maginary.ai/dashboard#api-keys as `"headers": { "Authorization": "Bearer <key>" }`, or you create one: call `create_account` with their email, wait for `check_account_status` to report verified, then `manage_api_key(action="create")` returns the key. The hosted server stores nothing per session, so put that key into the same `headers` entry (edit the client config) and reconnect. `configure_api_key` only persists keys on the local server (Option 2).
- If the client cannot send headers (Claude Desktop accounts whose add-connector dialog has no "request headers" section; it is a beta), use Option 2.

## Option 2 — local stdio via uvx (needs Python 3.10+ and uv)

Where the config lives: Claude Desktop → settings → developer → edit config (`claude_desktop_config.json`; macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`), Cursor `~/.cursor/mcp.json`, Cline its own MCP settings file. Install uv if missing: `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`.

```json
{
  "mcpServers": {
    "maginary": {
      "command": "uvx",
      "args": ["--upgrade", "maginary-mcp"],
      "env": { "MAGINARY_API_KEY": "<optional, see above>" }
    }
  }
}
```

If `uvx` is missing: `pip install maginary-mcp` and use `"command": "maginary-mcp"` with no args. Local mode adds `upload_image(file_path)` for image-to-image from files on disk.

## Verify

Call `list_parameters` (no auth). Then ask the user for a prompt and call `generate("a fox in autumn foliage --ar 16:9 --1")`, then `wait_for_generation(uuid)` until `processing_state` is `done`. A `timeout` result means still running: call `wait_for_generation` again with the same uuid. A `payment_required` result means the account has no credits: show the user `billing_url`.

## Prompt rules (so your first generation succeeds)

- Flags go at the end of the prompt: `--ar 16:9`, `--1` … `--4` (image count), `--flagship` (premium models), `--mp4` (video).
- Unknown flags are rejected with a 400 naming the flag. Use `get_parameter(name)` to check one.
- Image-to-image: put a public image URL in the prompt text.

Full docs: https://maginary.ai/mcp · https://maginary.ai/docs
