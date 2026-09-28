# How Amazon Quick Desktop loads local MCP servers

Notes gathered while building this server, against Quick Desktop
`0.1000.3070` on macOS (bundle id `com.amazon.QuickWork.mac`). The UI is the
supported path; the file layout below is what that UI writes, and it can change
between releases. Treat it as convenience, not contract.

## Why local MCP servers can do things Quick's own tools cannot

Quick's built-in agent tools (`run_python`, `ripgrep`, and friends) execute
inside `quickwork-sandbox`. From in there they cannot reach `localhost` and
cannot see most of the filesystem.

A local MCP server is different: Quick launches it as an ordinary child process
of the desktop app, with your user's privileges and no sandbox. That is the whole
reason this repo exists — an MCP server is the supported way to hand Quick a
capability its sandbox denies it.

Consequence worth stating plainly: any local MCP server you register can do
anything you can do. The sandbox is not protecting you from it.

## Where the config lives

Quick keeps one MCP config per signed-in profile:

```
~/.quickwork/
├── profiles.json                       # entries[], last_active
└── profiles/<data_path>/
    └── mcp_config.json                 # the file you want
```

Resolve it by reading `profiles.json`, matching `last_active` against
`entries[].id`, and joining that entry's `data_path`. Older installs that predate
profiles keep a single `~/.quickwork/mcp_config.json`; `quick-mcp` falls back to
it. (`mcp_config.json.legacy.bak`, if present, is a pre-v13 `{"servers": [...]}`
array — Quick migrates it and stops reading it.)

## Schema

```json
{
  "mcpServers": {
    "exec": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["/abs/path/to/exec_mcp_server.py"],
      "env": { "SOME_VAR": "value" },
      "disabled": false,
      "_quick": {
        "name": "Local Shell (exec)",
        "description": "shown in the Connectors list",
        "startupTimeout": 60
      }
    },
    "some-remote": {
      "url": "https://example.internal/mcp"
    }
  }
}
```

- **stdio** servers use `command` + `args`. **Remote** servers use `url`; Quick's
  client supports SSE and Streamable HTTP, with 3-legged OAuth, 2-legged OAuth,
  or no auth.
- `disabled: true` keeps the entry but stops Quick from starting it. The
  Connectors toggle writes this.
- `_quick` is Quick's own metadata block — display name, description, and
  `startupTimeout` in seconds. Raise it for a server that is slow to boot.
- Secrets can be written as `"AOE_TOKEN": "secret://<server>::e::<key>"`, which
  points at Quick's secret store instead of putting the value in the file. Set
  these through the UI ("Configure secrets"); this repo does not write them.

## The UI equivalent

**Settings → Capabilities → Connectors → + Add MCP server**, which offers
*Local (stdio)*, *Import from file*, and *Remote (URL)*. Import accepts a pasted
`{"mcpServers": {...}}` blob — it is lenient about malformed JSON (it runs the
text through `jsonrepair` first) and takes the first key in the object.

Each connector row has **Refresh**, which restarts that server. After editing
`mcp_config.json` by hand, Refresh is enough; you do not have to restart Quick.

## Gotchas

- **Use an absolute `command`.** Quick does not launch servers through a login
  shell, so its `PATH` is not your `PATH`. A bare `python3` may resolve to the
  wrong interpreter or to nothing. Point at a venv's `bin/python` directly.
  (This is also why the `exec` server runs its commands through `zsh -lc` — so
  the commands themselves get a normal environment even though the server did
  not.)
- **stdout is the transport.** Anything a server prints to stdout that is not
  JSON-RPC corrupts the stream and the connector dies. Log to stderr.
- **Keep stderr quiet at INFO.** The MCP Python SDK logs a line per request and
  an HTTP client like httpx logs every request URL. This server defaults to
  `WARNING`; otherwise a token in a query string lands in Quick's logs.
- **A crashing server shows up as a connector that will not connect**, with no
  detail in the UI. Run the server's test suite from a terminal to see the real
  error.
