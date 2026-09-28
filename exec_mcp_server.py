#!/usr/bin/env python3
"""exec MCP Server — runs real shell commands on this Mac for Amazon Quick.

Quick's own run_python / ripgrep tools execute inside quickwork-sandbox: they
can't reach localhost and can't see most of the filesystem. Local MCP servers
are launched by the desktop app as plain child processes, outside that sandbox,
so every command here lands on the real machine as the real user.

Commands go through a login shell (zsh -lc) so PATH matches Terminal.app. That
matters more than it sounds: tools installed under ~/.local/bin, ~/.toolbox/bin
or a version manager only exist once the profile is sourced.

Setup:
  uv sync                            # create the venv
  ./.venv/bin/python test_exec_mcp.py   # end-to-end protocol test
  python install.py install          # register with Quick

Protocol: MCP stdio (FastMCP). Never write to stdout — it is the transport.
"""

import asyncio
import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ─── Config ───────────────────────────────────────────────────────────

SHELL = os.environ.get("QUICK_EXEC_SHELL", "/bin/zsh")
SHELL_ARGS = os.environ.get("QUICK_EXEC_SHELL_ARGS", "-lc").split()

DEFAULT_CWD = os.environ.get("QUICK_EXEC_DEFAULT_CWD", str(Path.home()))
DEFAULT_TIMEOUT = float(os.environ.get("QUICK_EXEC_DEFAULT_TIMEOUT", "120"))
MAX_TIMEOUT = float(os.environ.get("QUICK_EXEC_MAX_TIMEOUT", "3600"))

# Per-stream cap. An unbounded `find /` would otherwise eat the caller's whole
# context window. 60 KB is roughly 15k tokens — generous for real output.
MAX_BYTES = int(os.environ.get("QUICK_EXEC_MAX_BYTES", "60000"))

# How long to keep draining the pipes after the child has exited or been killed.
# This has to be bounded: a grandchild that left the process group (setsid, or any
# daemon) inherits the pipe and holds it open forever, and waiting for EOF on it
# would make `timeout` meaningless and leave the connector looking dead.
READER_GRACE = float(os.environ.get("QUICK_EXEC_READER_GRACE", "2"))

# Finished jobs are kept so their output stays readable, but not forever -- under
# Quick this process lives as long as the desktop app.
MAX_FINISHED_JOBS = int(os.environ.get("QUICK_EXEC_MAX_FINISHED_JOBS", "50"))

# Every command is appended here as JSONL so there is a record of what Quick ran.
AUDIT_LOG = Path(
    os.environ.get("QUICK_EXEC_AUDIT_LOG", Path.home() / ".quick-exec-mcp" / "audit.jsonl")
).expanduser()

# Catastrophic-command guard. QUICK_EXEC_ALLOW_DANGEROUS=1 disables it.
GUARD_ENABLED = os.environ.get("QUICK_EXEC_ALLOW_DANGEROUS", "") not in ("1", "true", "yes")

# Directories where a recursive delete has no plausible intent behind it.
_ROOT_TARGETS = (
    r"(?:/|/\*|~|~/|~/\*|\$HOME|\$HOME/\*|\$HOME/?\*?"
    r"|/System\S*|/Library/?\*?|/Applications/?\*?|/usr/?\*?"
    r"|/etc/?\*?|/var/?\*?|/bin/?\*?|/sbin/?\*?|/opt/?\*?|/Users/?\*?)"
)

DANGEROUS = [
    (rf"\brm\s+(?:-\S+\s+)*-\S*[rR]\S*\s+(?:-\S+\s+)*{_ROOT_TARGETS}(?=\s|;|&|\||$)",
     "recursive delete of a system root or $HOME"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdiskutil\s+(erase\w*|reformat|partitionDisk)\b", "disk erase"),
    (r"\bdd\b[^\n]*\bof=/dev/r?disk", "raw write to a disk device"),
    (r">\s*/dev/r?disk", "raw redirect to a disk device"),
    (r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"(?:^|[;&|]\s*|\bsudo\s+)(shutdown|reboot|halt)\b", "host shutdown/reboot"),
    (r"\bcsrutil\s+disable\b", "disabling System Integrity Protection"),
    (r"\bspctl\s+--(master-)?disable\b", "disabling Gatekeeper"),
]

# WARNING, not the SDK default INFO: at INFO every single call logs "Processing
# request of type CallToolRequest" to stderr, which just fills up Quick's logs.
LOG_LEVEL = os.environ.get("QUICK_EXEC_LOG_LEVEL", "WARNING").upper()

mcp = FastMCP("exec", log_level=LOG_LEVEL)

# job_id -> bookkeeping for background commands
_jobs: dict[str, dict] = {}


# ─── Output buffering ─────────────────────────────────────────────────

class Stream:
    """Bounded capture that keeps the head AND the tail of a stream.

    A failing build puts the useful part at the end, so dropping the tail (the
    obvious implementation) throws away exactly what the caller needs. This
    keeps the first third and the last two thirds and says what it dropped.
    """

    def __init__(self, cap: int = MAX_BYTES):
        self.head_cap = max(cap // 3, 1)
        self.tail_cap = max(cap - self.head_cap, 1)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if len(self.head) < self.head_cap:
            room = self.head_cap - len(self.head)
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            overflow = len(self.tail) - self.tail_cap
            if overflow > 0:
                del self.tail[:overflow]

    @property
    def kept(self) -> int:
        return len(self.head) + len(self.tail)

    @property
    def truncated(self) -> bool:
        return self.total > self.kept

    def text(self) -> str:
        if not self.truncated:
            return bytes(self.head + self.tail).decode("utf-8", "replace")
        omitted = self.total - self.kept
        return (
            bytes(self.head).decode("utf-8", "replace")
            + f"\n\n...[{omitted} bytes omitted from the middle by exec MCP]...\n\n"
            + bytes(self.tail).decode("utf-8", "replace")
        )


# ─── Helpers ──────────────────────────────────────────────────────────

def _guard(command: str) -> str | None:
    """Return a refusal reason if the command is catastrophic, else None.

    Matched against the command with quotes stripped as well as verbatim: a model
    writes `rm -rf "$HOME"` as readily as `rm -rf $HOME`, and the quoted form
    would otherwise sail past every pattern here. Stripping cannot create a false
    refusal for a specific path, because the patterns anchor on the target ending
    there -- `rm -rf "$HOME/project"` still goes through.
    """
    if not GUARD_ENABLED:
        return None
    unquoted = command.replace('"', "").replace("'", "")
    for pattern, label in DANGEROUS:
        if re.search(pattern, command) or re.search(pattern, unquoted):
            return (
                f"Refused: matches the '{label}' guard. If this is really what you "
                "want, run it yourself in Terminal, or restart this MCP server with "
                "QUICK_EXEC_ALLOW_DANGEROUS=1."
            )
    return None


def _resolve_cwd(cwd: str | None) -> tuple[str | None, str | None]:
    """Return (resolved_path, error)."""
    target = os.path.expanduser(cwd) if cwd else DEFAULT_CWD
    if not os.path.isdir(target):
        return None, f"cwd does not exist or is not a directory: {target}"
    return target, None


def _build_env(env: dict | None) -> dict:
    merged = os.environ.copy()
    # Keep the agent from hanging in an interactive pager or drowning in ANSI.
    merged.setdefault("PAGER", "cat")
    merged.setdefault("GIT_PAGER", "cat")
    merged.setdefault("TERM", "dumb")
    merged.setdefault("NO_COLOR", "1")
    merged.setdefault("CLICOLOR", "0")
    if env:
        merged.update({str(k): str(v) for k, v in env.items()})
    return merged


def _audit(kind: str, **fields) -> None:
    """Append one JSONL record. Never raise — auditing must not break a call."""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, **fields}
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        print(f"exec-mcp: audit write failed: {exc}", file=sys.stderr)


async def _drain(stream, buf: Stream) -> None:
    """Read a pipe to EOF into buf. Keeps reading after the cap so the child
    never blocks writing into a full pipe."""
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        buf.feed(chunk)


async def _finish_readers(readers, grace: float = READER_GRACE) -> bool:
    """Let the pipe readers reach EOF, but only for `grace` seconds.

    Returns True if both streams closed. False means something still holds the
    write end -- a process that escaped the killed group -- so the captured output
    may be incomplete. Whatever did arrive is already in the Stream buffers, so
    cancelling costs nothing but the tail.
    """
    _, pending = await asyncio.wait(readers, timeout=grace)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return not pending


def _prune_jobs() -> None:
    """Drop the oldest finished jobs once there are more than MAX_FINISHED_JOBS.

    _jobs is insertion-ordered, so the finished list is oldest-first.
    """
    finished = [jid for jid, j in _jobs.items() if j["proc"].returncode is not None]
    for jid in finished[:max(0, len(finished) - MAX_FINISHED_JOBS)]:
        del _jobs[jid]


async def _kill_tree(proc) -> None:
    """SIGTERM then SIGKILL the whole process group.

    start_new_session=True puts the child in its own group, so a timed-out
    `npm install` doesn't leave orphaned children behind.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
            return
        except asyncio.TimeoutError:
            continue


# ─── Tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def shell_execute(
    command: str,
    cwd: str | None = None,
    timeout: float | None = None,
    stdin: str | None = None,
    env: dict | None = None,
) -> dict:
    """Run a shell command on this Mac and wait for it to finish.

    Runs through a login shell, so pipes, globs, redirects, &&, $VARS, heredocs
    and shell functions all work — pass the command exactly as you would type it
    in Terminal. Use this for anything that finishes within the timeout.

    For work that outlives the timeout (builds, installs, dev servers) use
    shell_start instead.

    Args:
        command: Shell command line, e.g. "ls -la ~/Documents | head -20".
        cwd: Working directory. Defaults to the user's home. ~ is expanded.
        timeout: Seconds before the process tree is killed. Default 120, max 3600.
            Zero or negative means "use the default", not "give up immediately".
        stdin: Text piped to the command's stdin.
        env: Extra environment variables, merged over the inherited environment.

    Returns:
        exit_code, stdout, stderr, duration_s, cwd, timed_out, and
        stdout_truncated / stderr_truncated flags.
    """
    refusal = _guard(command)
    if refusal:
        _audit("refused", command=command, reason=refusal)
        return {"error": refusal, "command": command}

    workdir, err = _resolve_cwd(cwd)
    if err:
        return {"error": err}

    # A non-positive timeout makes wait_for fire immediately, which would kill the
    # command before it produced anything and report it as a timeout. Treat any
    # such value as "use the default" rather than as an instant kill.
    requested = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
    limit = min(requested if requested > 0 else DEFAULT_TIMEOUT, MAX_TIMEOUT)
    started = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            SHELL, *SHELL_ARGS, command,
            cwd=workdir,
            env=_build_env(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return {"error": f"Failed to spawn {SHELL}: {exc}"}

    out, errb = Stream(), Stream()
    readers = [
        asyncio.create_task(_drain(proc.stdout, out)),
        asyncio.create_task(_drain(proc.stderr, errb)),
    ]

    if stdin is not None:
        try:
            proc.stdin.write(stdin.encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
    try:
        proc.stdin.close()
    except Exception:  # noqa: BLE001 - already-closed pipe is fine
        pass

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=limit)
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_tree(proc)

    drained = await _finish_readers(readers)

    duration = round(time.monotonic() - started, 2)
    _audit("execute", command=command, cwd=workdir, exit_code=proc.returncode,
           duration_s=duration, timed_out=timed_out, output_incomplete=not drained)

    return {
        "exit_code": proc.returncode,
        "stdout": out.text(),
        "stderr": errb.text(),
        "duration_s": duration,
        "cwd": workdir,
        "timed_out": timed_out,
        "stdout_truncated": out.truncated,
        "stderr_truncated": errb.truncated,
        # True when a process outlived the command and still holds its pipes open,
        # so what came back may be missing the tail.
        "output_incomplete": not drained,
    }


@mcp.tool()
async def shell_start(
    command: str,
    cwd: str | None = None,
    env: dict | None = None,
) -> dict:
    """Start a long-running command in the background and return a job id.

    Output is buffered as it arrives; read it with shell_job_output, block on it
    with shell_job_wait, stop it with shell_job_kill. Use this for builds,
    installs, test suites and dev servers that would blow shell_execute's
    timeout.

    Args:
        command: Shell command line.
        cwd: Working directory. Defaults to the user's home.
        env: Extra environment variables.

    Returns:
        job_id, pid, command, cwd.
    """
    refusal = _guard(command)
    if refusal:
        _audit("refused", command=command, reason=refusal)
        return {"error": refusal, "command": command}

    workdir, err = _resolve_cwd(cwd)
    if err:
        return {"error": err}

    _prune_jobs()

    try:
        proc = await asyncio.create_subprocess_exec(
            SHELL, *SHELL_ARGS, command,
            cwd=workdir,
            env=_build_env(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return {"error": f"Failed to spawn {SHELL}: {exc}"}

    job_id = uuid.uuid4().hex[:12]
    out, errb = Stream(), Stream()
    _jobs[job_id] = {
        "proc": proc,
        "command": command,
        "cwd": workdir,
        "started_at": time.time(),
        "stdout": out,
        "stderr": errb,
        "readers": [
            asyncio.create_task(_drain(proc.stdout, out)),
            asyncio.create_task(_drain(proc.stderr, errb)),
        ],
    }
    _audit("start", job_id=job_id, command=command, cwd=workdir, pid=proc.pid)
    return {"job_id": job_id, "pid": proc.pid, "command": command, "cwd": workdir}


def _job_snapshot(job_id: str, job: dict, tail_lines: int) -> dict:
    proc = job["proc"]

    def render(buf: Stream) -> str:
        text = buf.text()
        if tail_lines and tail_lines > 0:
            lines = text.splitlines()
            if len(lines) > tail_lines:
                return "\n".join(lines[-tail_lines:])
        return text

    return {
        "job_id": job_id,
        "command": job["command"],
        "cwd": job["cwd"],
        "running": proc.returncode is None,
        "exit_code": proc.returncode,
        "pid": proc.pid,
        "elapsed_s": round(time.time() - job["started_at"], 2),
        "stdout": render(job["stdout"]),
        "stderr": render(job["stderr"]),
        "stdout_total_bytes": job["stdout"].total,
        "stderr_total_bytes": job["stderr"].total,
    }


@mcp.tool()
async def shell_job_output(job_id: str, tail_lines: int = 200) -> dict:
    """Read the output captured so far from a background job.

    Safe to call repeatedly while the job runs — it returns a snapshot and does
    not consume the buffer.

    Args:
        job_id: Id returned by shell_start.
        tail_lines: Return only the last N lines of each stream. 0 means all.

    Returns:
        running, exit_code (null while running), stdout, stderr, elapsed_s.
    """
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Unknown job_id: {job_id}", "known_jobs": list(_jobs)}
    return _job_snapshot(job_id, job, tail_lines)


@mcp.tool()
async def shell_job_wait(job_id: str, timeout: float = 120, tail_lines: int = 200) -> dict:
    """Block until a background job exits, then return its output.

    Cheaper and more reliable than polling shell_job_output in a loop. If the
    timeout expires the job keeps running and `running` comes back true — call
    again or kill it.

    Args:
        job_id: Id returned by shell_start.
        timeout: Max seconds to wait. The job is NOT killed on timeout.
        tail_lines: Return only the last N lines of each stream. 0 means all.
    """
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Unknown job_id: {job_id}", "known_jobs": list(_jobs)}

    waited_out = False
    drained = True
    try:
        await asyncio.wait_for(job["proc"].wait(), timeout=min(timeout, MAX_TIMEOUT))
        # Let the readers flush what the child wrote just before exiting -- bounded,
        # because a process that outlived the job still holds these pipes open and
        # would otherwise make this wait ignore its own timeout.
        drained = await _finish_readers(job["readers"])
    except asyncio.TimeoutError:
        # Still running: leave the readers alone so later reads keep collecting.
        waited_out = True

    snap = _job_snapshot(job_id, job, tail_lines)
    snap["wait_timed_out"] = waited_out
    snap["output_incomplete"] = not drained
    return snap


@mcp.tool()
async def shell_job_kill(job_id: str) -> dict:
    """Kill a background job and its whole process tree.

    Args:
        job_id: Id returned by shell_start.
    """
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Unknown job_id: {job_id}", "known_jobs": list(_jobs)}

    proc = job["proc"]
    if proc.returncode is not None:
        return {"job_id": job_id, "already_exited": True, "exit_code": proc.returncode}

    await _kill_tree(proc)
    _audit("kill", job_id=job_id, command=job["command"], exit_code=proc.returncode)
    return {"job_id": job_id, "killed": True, "exit_code": proc.returncode}


@mcp.tool()
async def shell_list_jobs() -> dict:
    """List background jobs started this session, running and finished."""
    return {
        "jobs": [
            {
                "job_id": jid,
                "command": j["command"],
                "cwd": j["cwd"],
                "running": j["proc"].returncode is None,
                "exit_code": j["proc"].returncode,
                "pid": j["proc"].pid,
                "elapsed_s": round(time.time() - j["started_at"], 2),
            }
            for jid, j in _jobs.items()
        ]
    }


@mcp.tool()
async def shell_info() -> dict:
    """Report how this server is configured and what environment commands see.

    Worth calling first in a session: it proves the server is running on the
    real host as the real user rather than inside Quick's sandbox.
    """
    probe = await shell_execute(
        'echo "user=$(whoami) host=$(hostname -s) home=$HOME"; echo "PATH=$PATH"',
        timeout=20,
    )
    return {
        "shell": f"{SHELL} {' '.join(SHELL_ARGS)}",
        "python": sys.executable,
        "server_pid": os.getpid(),
        "default_cwd": DEFAULT_CWD,
        "default_timeout_s": DEFAULT_TIMEOUT,
        "max_timeout_s": MAX_TIMEOUT,
        "max_bytes_per_stream": MAX_BYTES,
        "audit_log": str(AUDIT_LOG),
        "dangerous_command_guard": "enabled" if GUARD_ENABLED else "DISABLED",
        "guarded_patterns": [label for _, label in DANGEROUS] if GUARD_ENABLED else [],
        "probe": probe.get("stdout", "").strip(),
        "probe_exit_code": probe.get("exit_code"),
    }


# ─── Main ─────────────────────────────────────────────────────────────

def main_sync():
    """Entry point for pyproject [project.scripts] and direct execution."""
    mcp.run()


if __name__ == "__main__":
    main_sync()
