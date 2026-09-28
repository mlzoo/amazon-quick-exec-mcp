#!/usr/bin/env python3
"""End-to-end test: drives exec_mcp_server over real MCP stdio, the way Quick does.

Speaks the wire protocol rather than importing the module, so it exercises the
same path the desktop app takes — JSON-RPC framing, tool schemas, serialization.

  python test_exec_mcp.py        # exits non-zero if anything fails
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE / ".venv" / "bin" / "python"
SRV = HERE / "exec_mcp_server.py"

# Imported so the destructive guard cases can be asserted against _guard() itself,
# without ever handing them to a shell. See the guard section below.
sys.path.insert(0, str(HERE))
import exec_mcp_server as server  # noqa: E402

_failures: list[str] = []
_passes = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    if ok:
        _passes += 1
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        _failures.append(name)
        print(f"  \033[31mFAIL\033[0m  {name}" + (f"\n          {detail}" if detail else ""))


class Client:
    """Minimal MCP stdio client."""

    def __init__(self, proc):
        self.proc = proc
        self._id = 0

    async def _send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def _recv(self, timeout=90):
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
        if not line:
            err = (await self.proc.stderr.read()).decode()
            raise RuntimeError("server closed stdout. stderr:\n" + err)
        return json.loads(line)

    async def request(self, method, params=None, timeout=90):
        await self._send(method, params)
        return await self._recv(timeout)

    async def notify(self, method, params=None):
        await self._send(method, params, notify=True)

    async def call(self, tool, args, timeout=90):
        """Call a tool and return its parsed dict result."""
        resp = await self.request("tools/call", {"name": tool, "arguments": args}, timeout)
        if "error" in resp:
            return {"_rpc_error": resp["error"]}
        text = resp["result"]["content"][0]["text"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_text": text}


async def main() -> int:
    if not PY.exists():
        sys.exit(f"Missing {PY}. Run `uv sync` in {HERE} first.")

    audit = Path(tempfile.mkdtemp(prefix="exec-mcp-test-")) / "audit.jsonl"
    env = {**os.environ,
           "QUICK_EXEC_AUDIT_LOG": str(audit),
           "QUICK_EXEC_MAX_FINISHED_JOBS": "3"}  # so the pruning check is quick

    proc = await asyncio.create_subprocess_exec(
        str(PY), str(SRV),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=8 * 1024 * 1024,  # one MCP response is a single long line
    )
    c = Client(proc)

    print("\n== handshake ==")
    init = await c.request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    })
    check("initialize returns serverInfo",
          init.get("result", {}).get("serverInfo", {}).get("name") == "exec",
          json.dumps(init)[:300])
    await c.notify("notifications/initialized", {})

    listed = await c.request("tools/list")
    names = {t["name"] for t in listed["result"]["tools"]}
    expected = {"shell_execute", "shell_start", "shell_job_output", "shell_job_wait",
                "shell_job_kill", "shell_list_jobs", "shell_info"}
    check("all 7 tools advertised", names == expected, f"got {sorted(names)}")
    schema = next(t for t in listed["result"]["tools"] if t["name"] == "shell_execute")
    check("shell_execute schema requires only `command`",
          schema["inputSchema"].get("required") == ["command"],
          json.dumps(schema["inputSchema"])[:300])

    print("\n== environment / not sandboxed ==")
    info = await c.call("shell_info", {})
    real_user = os.environ.get("USER", "")
    check("runs as the real user",
          f"user={real_user}" in info.get("probe", ""), info.get("probe", ""))
    check("guard is on by default",
          info.get("dangerous_command_guard") == "enabled", str(info))

    r = await c.call("shell_execute", {"command": "echo hello from $(whoami)"})
    check("basic command", r.get("exit_code") == 0 and real_user in r.get("stdout", ""), str(r))

    r = await c.call("shell_execute", {"command": "ls -1 *.py | sort", "cwd": str(HERE)})
    check("cwd + glob + pipe",
          r.get("exit_code") == 0 and "exec_mcp_server.py" in r.get("stdout", ""), str(r))

    r = await c.call("shell_execute", {"command": "cd ~ && pwd", "cwd": "~/Documents"})
    check("~ expands in cwd", r.get("cwd") == str(Path.home() / "Documents"), str(r))

    # The point of -lc is that the profile is sourced, so PATH must match what a
    # real login shell gives -- not the (shorter) PATH this test process inherited.
    expected_path = subprocess.run(
        ["/bin/zsh", "-lc", "echo $PATH"], capture_output=True, text=True,
    ).stdout.strip()
    r = await c.call("shell_execute", {"command": "echo $PATH"})
    check("commands see the login-shell PATH",
          r.get("stdout", "").strip() == expected_path,
          f"server={r.get('stdout', '').strip()[:200]}\n          login={expected_path[:200]}")
    r = await c.call("shell_execute", {"command": "command -v git"})
    check("PATH actually resolves binaries", r.get("exit_code") == 0, str(r)[:200])

    print("\n== streams, exit codes, stdin, env ==")
    r = await c.call("shell_execute", {"command": "echo to-out; echo to-err >&2; exit 42"})
    check("exit code propagates", r.get("exit_code") == 42, str(r))
    check("stdout/stderr not mixed",
          r.get("stdout", "").strip() == "to-out" and r.get("stderr", "").strip() == "to-err",
          str(r))

    r = await c.call("shell_execute", {"command": "tr a-z A-Z", "stdin": "shouting\n"})
    check("stdin is piped in", r.get("stdout", "").strip() == "SHOUTING", str(r))

    r = await c.call("shell_execute", {"command": "echo $MY_VAR", "env": {"MY_VAR": "injected"}})
    check("env override applies", r.get("stdout", "").strip() == "injected", str(r))

    r = await c.call("shell_execute", {"command": "printf 'a\\xffb'"})
    check("invalid utf-8 does not crash the call", r.get("exit_code") == 0, str(r)[:200])

    print("\n== timeout and process-tree cleanup ==")
    marker = "sleep 31337"
    r = await c.call("shell_execute",
                     {"command": f"echo before; {marker}; echo after", "timeout": 3})
    check("timed_out reported", r.get("timed_out") is True, str(r))
    check("partial output kept", "before" in r.get("stdout", ""), str(r))
    check("returns near the timeout, not after the sleep",
          2 <= (r.get("duration_s") or 0) < 10, str(r.get("duration_s")))
    probe = await c.call("shell_execute", {"command": f"pgrep -f '{marker}' | wc -l"})
    check("no orphaned child survives the timeout",
          probe.get("stdout", "").strip() == "0", str(probe))

    print("\n== output truncation keeps head and tail ==")
    r = await c.call("shell_execute",
                     {"command": "echo FIRSTLINE; yes filler | head -200000; echo LASTLINE"},
                     timeout=120)
    out = r.get("stdout", "")
    check("truncation flagged", r.get("stdout_truncated") is True, str(r)[:200])
    check("head survives truncation", "FIRSTLINE" in out, out[:120])
    check("tail survives truncation", "LASTLINE" in out, out[-120:])
    check("truncated output stays bounded", len(out) < 120_000, f"len={len(out)}")

    print("\n== error handling ==")
    r = await c.call("shell_execute", {"command": "pwd", "cwd": "/nope/missing"})
    check("bad cwd is an error, not a crash", "error" in r, str(r))
    r = await c.call("shell_job_output", {"job_id": "nope"})
    check("unknown job_id is an error", "error" in r, str(r))

    print("\n== dangerous-command guard (checked in-process, never executed) ==")
    # These are asserted against _guard() directly. Sending `rm -rf "$HOME"` to a
    # live server and trusting a regex to stop it would put this suite one bad
    # regex away from deleting the home directory of whoever ran it.
    for cmd in ["rm -rf /", "rm -rf ~", "rm -rf $HOME", "sudo rm -fr /Applications",
                "sudo reboot", "csrutil disable", "spctl --master-disable",
                "mkfs.ext4 /dev/disk2", "dd if=/dev/zero of=/dev/rdisk0",
                "diskutil eraseDisk JHFS+ x /dev/disk2",
                # quoting is how a model writes these, so it must not be an escape hatch
                'rm -rf "$HOME"', "rm -rf '/'", 'rm -rf "/usr"', "rm -rf '$HOME'"]:
        check(f"refuses {cmd!r}", server._guard(cmd) is not None, "guard returned None")

    for cmd in ['rm -rf "$HOME/project"', "rm -rf /tmp/scratch", "rm -rf ./build",
                "grep -c shutdown /etc/hosts", "echo /usr/bin is fine",
                "git log --oneline"]:
        check(f"allows {cmd!r}", server._guard(cmd) is None, str(server._guard(cmd)))

    print("\n== the refusal reaches the caller over the wire ==")
    # $HOME is redirected at a throwaway directory, so a future regression in the
    # guard destroys a temp dir instead of someone's home.
    decoy = Path(tempfile.mkdtemp(prefix="exec-mcp-decoy-"))
    (decoy / "canary").write_text("intact")
    r = await c.call("shell_execute", {"command": 'rm -rf "$HOME"',
                                       "env": {"HOME": str(decoy)},
                                       "cwd": "/tmp", "timeout": 10})
    check("a guarded command is refused through the MCP call", "error" in r, str(r)[:200])
    check("and the decoy $HOME survived regardless",
          (decoy / "canary").is_file(), f"{decoy} was wiped")

    tmpdir = tempfile.mkdtemp(prefix="exec-mcp-safe-")
    for cmd, label in [
        (f"rm -rf {tmpdir} && echo gone", "rm -rf of a specific temp dir"),
        ('rm -rf "$HOME/exec-mcp-no-such-dir" && echo ok',
         "quoted rm -rf of a specific dir under $HOME"),
        ("grep -c shutdown /etc/hosts; true", "the word 'shutdown' as an argument"),
        ("echo /usr/bin is fine", "a system path mentioned harmlessly"),
    ]:
        r = await c.call("shell_execute", {"command": cmd, "timeout": 10})
        check(f"allows {label}", "error" not in r, str(r)[:200])

    print("\n== timeout argument edge cases ==")
    for value in (-5, 0):
        r = await c.call("shell_execute", {"command": "echo alive", "timeout": value})
        check(f"timeout={value} falls back to the default instead of killing instantly",
              r.get("timed_out") is False and r.get("stdout", "").strip() == "alive",
              str(r))

    print("\n== background jobs ==")
    j = await c.call("shell_start",
                     {"command": "for i in 1 2 3; do echo tick $i; sleep 1; done; echo done"})
    check("shell_start returns a job_id and pid",
          bool(j.get("job_id")) and bool(j.get("pid")), str(j))
    jid = j.get("job_id")

    await asyncio.sleep(1.5)
    o = await c.call("shell_job_output", {"job_id": jid})
    check("output readable mid-run", o.get("running") is True and "tick 1" in o.get("stdout", ""),
          str(o))

    o = await c.call("shell_job_wait", {"job_id": jid, "timeout": 20})
    check("shell_job_wait blocks until exit",
          o.get("running") is False and o.get("exit_code") == 0, str(o))
    check("final output complete", "done" in o.get("stdout", ""), str(o))

    j2 = await c.call("shell_start", {"command": "sleep 300"})
    o = await c.call("shell_job_wait", {"job_id": j2["job_id"], "timeout": 2})
    check("shell_job_wait reports its own timeout without killing the job",
          o.get("wait_timed_out") is True and o.get("running") is True, str(o))
    k = await c.call("shell_job_kill", {"job_id": j2["job_id"]})
    check("shell_job_kill kills the job", k.get("killed") is True, str(k))
    probe = await c.call("shell_execute", {"command": "pgrep -f 'sleep 300' | wc -l"})
    check("killed job leaves no process behind", probe.get("stdout", "").strip() == "0",
          str(probe))

    jobs = await c.call("shell_list_jobs", {})
    check("shell_list_jobs lists both jobs", len(jobs.get("jobs", [])) >= 2, str(jobs))

    print("\n== a process that escapes the group cannot hang the call ==")
    # setsid puts the grandchild in its own session, so killpg misses it and it keeps
    # the inherited stdout pipe open. Waiting for EOF on that pipe would make
    # `timeout` meaningless and leave the connector looking dead.
    marker = "exec-mcp-orphan-probe"
    r = await c.call("shell_execute", {
        "command": f"python3 -c 'import os,time,sys;os.setsid();time.sleep(30)' {marker} & "
                   f"echo spawned; sleep 20",
        "timeout": 3}, timeout=60)
    check("timeout still bounds the call when an orphan holds the pipes",
          (r.get("duration_s") or 99) < 15, f"duration_s={r.get('duration_s')}")
    check("output from before the kill is still returned",
          "spawned" in r.get("stdout", ""), repr(r.get("stdout")))
    check("and the response says the output may be incomplete",
          r.get("output_incomplete") is True, str(r)[:200])
    await c.call("shell_execute", {"command": f"pkill -f {marker}; true", "timeout": 10})

    print("\n== finished jobs do not accumulate forever ==")
    for _ in range(6):
        j = await c.call("shell_start", {"command": "true"})
        await c.call("shell_job_wait", {"job_id": j["job_id"], "timeout": 10})
    jobs = await c.call("shell_list_jobs", {})
    finished = [x for x in jobs.get("jobs", []) if not x["running"]]
    check("finished jobs are pruned to the configured cap",
          len(finished) <= 4, f"{len(finished)} finished jobs retained with a cap of 3")

    print("\n== audit log ==")
    check("audit file written", audit.exists(), str(audit))
    if audit.exists():
        records = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
        kinds = {r["kind"] for r in records}
        check("logs executes, starts, kills and refusals",
              {"execute", "start", "kill", "refused"} <= kinds, str(sorted(kinds)))
        check("audit records carry the command",
              all("command" in r for r in records), str(records[:2]))

    print("\n== shutdown ==")
    proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
    stderr = (await proc.stderr.read()).decode().strip()
    noisy = [ln for ln in stderr.splitlines()
             if ln.strip() and not ln.startswith("Processing request")]
    check("no tracebacks or errors on stderr", not noisy, "\n".join(noisy[:10]))
    check("server does not log per-request chatter at default level",
          "Processing request" not in stderr, stderr[:200])

    total = _passes + len(_failures)
    print(f"\n{'='*60}")
    if _failures:
        print(f"\033[31m{len(_failures)}/{total} checks FAILED:\033[0m")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"\033[32mall {total} checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
