# Maginary Claude Skill

A single-file [Agent Skill](https://docs.claude.com/en/docs/agents/skills) that
teaches Claude the Maginary `--flag` prompt DSL, model selection, and the async
generate→poll flow — via the `maginary` MCP server when connected, or the plain
REST API when not (the skill documents both).

The canonical file lives inside the package at
[`src/maginary_mcp/SKILL.md`](../src/maginary_mcp/SKILL.md) so it
ships in the wheel.

## Install

```bash
uvx maginary-mcp --install-skill   # -> ~/.claude/skills/maginary-image-gen/SKILL.md
```

Re-run any time to update; it refuses to clobber local edits unless you add
`--force`. Optionally connect the MCP server (hosted, no install):

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

The skill works on its own (DSL + REST); with the MCP connected, Claude can also
look up exact flags (`search_parameters`) and run generations natively
(`generate`, `wait_for_generation`).
