# amazon-quick-exec-mcp — a real shell for Amazon Quick

[中文](README.zh-CN.md)

Amazon Quick's built-in agent tools (`run_python`, `ripgrep`, and friends) execute
inside `quickwork-sandbox`: they cannot reach `localhost` and cannot see most of
your filesystem. Local MCP servers are different — Quick launches them as ordinary
child processes of the desktop app, outside that sandbox.

So this server runs commands on the real machine, as you. Sync execution,
background jobs, process-tree cleanup, an audit log, and a narrow guard against
the handful of commands nobody means to run.

Commands go through a login shell (`zsh -lc`), so `PATH` matches Terminal. That
matters more than it sounds: anything installed under `~/.local/bin`,
`~/.toolbox/bin`, or by a version manager only exists once the profile is sourced.

## Install

```bash
git clone <this repo> && cd quick-exec-mcp

uv sync                                # create the venv
./.venv/bin/python test_exec_mcp.py    # 70 checks over real MCP stdio
python install.py install              # register with Quick
```

Then in Quick: **Settings → Capabilities → Connectors → Local Shell (exec) →
Refresh**. Ask it `call shell_info` as a first message — it reports the user, host
and `PATH` your commands actually get, which is how you confirm you are outside
the sandbox.

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). `install.py` is
stdlib-only, so it runs with any `python3`.

```
python install.py status       what Quick currently has registered
python install.py install      add this server, enabled
python install.py disable      keep the entry, turn it off
python install.py enable       turn it back on
python install.py uninstall    remove the entry
```

It edits the active profile's `mcp_config.json` — the same file the Connectors UI
writes — and backs it up to `mcp_config.json.bak` before every write. A reinstall
preserves `secret://` references and any env you set by hand. See
[docs/how-quick-loads-mcp.md](docs/how-quick-loads-mcp.md) for the file layout,
the schema, and the gotchas that cost the most time.

## Tools

| Tool | What it does |
|---|---|
| `shell_execute` | Run and wait. Returns `exit_code`, `stdout`, `stderr`, `duration_s`, `timed_out` |
| `shell_start` | Run in the background, returns a `job_id`. For builds, installs, dev servers |
| `shell_job_output` | Snapshot of a job's output so far. Safe to call repeatedly |
| `shell_job_wait` | Block until a job exits, then return its output. Beats polling |
| `shell_job_kill` | Kill a job and its whole process tree |
| `shell_list_jobs` | Every job started this session, running and finished |
| `shell_info` | Configuration plus a live `whoami` / `hostname` / `PATH` probe |

`shell_execute` takes `cwd` (defaults to `$HOME`, `~` expands), `timeout`
(default 120s), `stdin`, and `env` (merged over the inherited environment).

## Configuration

Set these in the `env` block of the `exec` entry in Quick's `mcp_config.json`.

| Variable | Default | Meaning |
|---|---|---|
| `QUICK_EXEC_SHELL` | `/bin/zsh` | Which shell to use |
| `QUICK_EXEC_SHELL_ARGS` | `-lc` | `-l` sources the profile; drop it to skip that |
| `QUICK_EXEC_DEFAULT_CWD` | `$HOME` | Working directory when `cwd` is omitted |
| `QUICK_EXEC_DEFAULT_TIMEOUT` | `120` | Default timeout, seconds |
| `QUICK_EXEC_MAX_TIMEOUT` | `3600` | Ceiling on the `timeout` argument |
| `QUICK_EXEC_MAX_BYTES` | `60000` | Per-stream output cap, roughly 15k tokens |
| `QUICK_EXEC_READER_GRACE` | `2` | Seconds to keep draining the pipes after the child exits |
| `QUICK_EXEC_MAX_FINISHED_JOBS` | `50` | Finished jobs retained before the oldest are dropped |
| `QUICK_EXEC_AUDIT_LOG` | `~/.quick-exec-mcp/audit.jsonl` | Audit log path |
| `QUICK_EXEC_LOG_LEVEL` | `WARNING` | `INFO` logs every call to stderr |
| `QUICK_EXEC_ALLOW_DANGEROUS` | unset | `1` turns the command guard off |

## Audit log

Every command appends one JSON line to `~/.quick-exec-mcp/audit.jsonl` — time,
command, cwd, exit code, duration — and refusals are logged too. This is how you
find out what Quick actually ran on your machine.

```bash
tail -20 ~/.quick-exec-mcp/audit.jsonl | jq -c '[.ts, .kind, .exit_code, .command]'
```

## Command guard

Refused by default (`QUICK_EXEC_ALLOW_DANGEROUS=1` disables it):

- recursive delete of `/`, `$HOME`, `/System`, `/Applications`, `/usr`, and peers
- `mkfs*`, `diskutil eraseDisk/reformat/partitionDisk`, `dd of=/dev/disk*`
- fork bombs
- `shutdown` / `reboot` / `halt` in command position
- `csrutil disable`, `spctl --master-disable` (turning off SIP / Gatekeeper)

Patterns are matched against the command both verbatim and with quotes stripped,
because `rm -rf "$HOME"` is how a model writes that as often as not. They stay
deliberately narrow, so ordinary work goes through: `rm -rf` on a specific
directory is allowed, `rm -rf "$HOME/project"` is allowed, and so is
`grep shutdown /etc/hosts` — `shutdown` there is an argument, not a command.

**This guards against a model slipping, not against malice.** It is pattern
matching, so any indirection (`$(echo rm) -rf /`) defeats it, and anyone who can
reach this server can already run anything you can.

## Behaviour worth knowing

- **Timeouts kill the whole process tree.** Children run in their own process
  group (`start_new_session=True`); on timeout the group gets SIGTERM then
  SIGKILL, so a timed-out `npm install` leaves no orphans. Output produced before
  the timeout is kept, with `timed_out: true`.
- **`timeout` is a real bound, even against a process that escapes.** Anything
  that calls `setsid` — a daemon, mostly — leaves the process group, survives the
  kill, and keeps the inherited stdout pipe open. Waiting for end-of-file on that
  pipe would hang the call forever and make the connector look dead, so the drain
  gets its own `QUICK_EXEC_READER_GRACE` budget and the reply comes back with
  `output_incomplete: true`. Expect roughly `timeout + 5s` in that case: SIGTERM,
  the escalation to SIGKILL, then the grace period.
- **`timeout: 0` or a negative value means "use the default"**, not "give up
  immediately".
- **Truncation keeps the head *and* the tail.** Over the cap, the first third and
  last two thirds survive with a note about how many bytes went missing in
  between. A failing build puts the useful part at the end; keeping only the head
  would discard exactly what you need.
- **stdout and stderr come back separately**, never interleaved.
- **Interactive pagers are disabled** via `PAGER=cat`, `GIT_PAGER=cat`,
  `TERM=dumb`, `NO_COLOR=1`, so `git log` cannot hang in `less` or return a wall
  of ANSI escapes. Override any of them through `env`.

## Tests

```bash
./.venv/bin/python test_exec_mcp.py
```

70 checks driving the server over **real MCP stdio** — `initialize`,
`tools/list`, `tools/call`, the whole JSON-RPC exchange Quick makes — rather than
importing the module and calling functions. Tools that look fine in Python and
break over the wire are exactly the failure this catches. Covered: handshake, tool
schemas, login-shell `PATH` (compared against an actual `zsh -lc`), stream
separation, stdin, env overrides, invalid UTF-8, timeout with orphan detection via
`pgrep`, an orphan that escapes the process group entirely, head-and-tail
truncation, background job lifecycle, job pruning, and the audit log.

The destructive guard cases — `rm -rf "$HOME"` and friends — are asserted against
`_guard()` **in-process, so they are never handed to a shell.** A suite that sends
them to a live server and trusts a regex to stop them is one bad regex away from
deleting the home directory of whoever ran it. The one case that does go over the
wire runs with `HOME` pointed at a throwaway directory and then asserts that
directory is still intact, so a future regression destroys a temp dir instead.

## Security

This gives Quick full shell access as your user — read and write any file you can,
read credential files, call the AWS CLI, push code, make network requests.
Bypassing the sandbox is the point of it, not a bug.

- Leave Quick's `global_default` tool permission on `prompt` so calls are
  confirmed before they run.
- Read `~/.quick-exec-mcp/audit.jsonl` now and then.
- Do not register this on a machine or client you do not trust.
- Keep credentials out of the config file; Quick's secret store (`secret://`)
  exists for that, and a reinstall preserves those references.

## License

MIT — see [LICENSE](LICENSE).
