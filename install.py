#!/usr/bin/env python3
"""Register this server with Amazon Quick Desktop.

Quick keeps local MCP servers in the active profile's mcp_config.json -- the same
file its Settings -> Capabilities -> Connectors UI writes. Editing it directly is
faster than clicking through the dialog and makes the setup reproducible.

  python install.py status       show what Quick currently has registered
  python install.py install      add this server, enabled
  python install.py disable      keep the entry, turn it off
  python install.py enable       turn it back on
  python install.py uninstall    remove the entry

The file is backed up to mcp_config.json.bak before every write. Quick needs the
connector refreshed (or a restart) to pick a change up.

Stdlib only, so it runs with any python3 -- no venv needed to bootstrap.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SERVER_KEY = "exec"
DISPLAY_NAME = "Local Shell (exec)"
DESCRIPTION = "Run shell commands on this machine, outside Quick's sandbox"
STARTUP_TIMEOUT = 60
ENTRY = HERE / "exec_mcp_server.py"

# Env this installer owns. Everything else found in an existing entry is left
# alone, so hand-tuned values and Quick's secret:// references survive a reinstall.
MANAGED_ENV: dict[str, str] = {}
MANAGED_QUICK_KEYS = {"name", "description", "startupTimeout"}

QUICKWORK = Path(os.environ.get("QUICKWORK_HOME", Path.home() / ".quickwork"))

GREEN, YELLOW, RED, DIM, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def venv_python() -> Path:
    """The interpreter Quick should launch.

    Must be absolute: Quick does not start servers through a login shell, so its
    PATH is not yours and a bare `python3` may resolve to nothing.
    """
    candidate = HERE / ".venv" / "bin" / "python"
    if not candidate.exists():
        sys.exit(f"{RED}missing {candidate}{OFF}\n  run `uv sync` in {HERE} first.")
    return candidate


def config_path() -> Path:
    """Locate the mcp_config.json of Quick's currently active profile."""
    profiles = QUICKWORK / "profiles.json"
    if profiles.is_file():
        data = json.loads(profiles.read_text() or "{}")
        entries = {e["id"]: e for e in data.get("entries", [])}
        entry = entries.get(data.get("last_active")) or next(iter(entries.values()), None)
        if entry and entry.get("data_path"):
            return QUICKWORK / entry["data_path"] / "mcp_config.json"
    return QUICKWORK / "mcp_config.json"  # pre-profile layout


def load_config(path: Path) -> dict:
    if not path.is_file():
        return {"mcpServers": {}}
    data = json.loads(path.read_text() or "{}")
    data.setdefault("mcpServers", {})
    return data


def save_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def build_entry(existing: dict | None = None) -> dict:
    """Build the config entry.

    Built fresh rather than layered onto the old entry: a leftover key from a
    different kind of entry (a remote server's `url`, say) must not survive next
    to `command`/`args`, because Quick would follow the `url` and the install
    would silently do nothing.

    What is carried over is what only Quick can supply -- `secret://` references
    added through "Configure secrets" are opaque here, and dropping one breaks a
    working server invisibly -- plus any env or `_quick` keys a human set by hand.
    """
    existing = existing or {}

    env = dict(MANAGED_ENV)
    for key, value in (existing.get("env") or {}).items():
        if key not in MANAGED_ENV:
            env[key] = value

    quick = {k: v for k, v in (existing.get("_quick") or {}).items()
             if k not in MANAGED_QUICK_KEYS}
    quick.update({
        "name": DISPLAY_NAME,
        "description": DESCRIPTION,
        "startupTimeout": STARTUP_TIMEOUT,
    })

    entry = {"command": str(venv_python()), "args": [str(ENTRY)], "_quick": quick}
    if env:
        entry["env"] = env
    return entry


def reload_hint() -> None:
    print(f"\n{YELLOW}Refresh the connector in Quick (Settings -> Capabilities -> "
          f"Connectors) or restart Quick to pick this up.{OFF}")


def cmd_status(path: Path, data: dict) -> int:
    servers = data["mcpServers"]
    if not servers:
        print("  (nothing registered)")
        return 0
    for name, cfg in servers.items():
        state = "disabled" if cfg.get("disabled") else "ENABLED"
        target = cfg.get("url") or " ".join([cfg.get("command", "?"), *cfg.get("args", [])])
        mark = f"  {DIM}<- this server{OFF}" if name == SERVER_KEY else ""
        print(f"  {name:20} {state:9} {target}{mark}")
    return 0


def cmd_install(path: Path, data: dict) -> int:
    servers = data["mcpServers"]
    servers[SERVER_KEY] = build_entry(servers.get(SERVER_KEY))
    save_config(path, data)
    print(f"  {GREEN}installed{OFF} '{SERVER_KEY}' -> {servers[SERVER_KEY]['command']} "
          f"{ENTRY.name}")
    print(f"  {DIM}This grants Quick full shell access as your user. Keep Quick's tool "
          f"permissions on 'prompt'.{OFF}")
    reload_hint()
    return 0


def cmd_enable(path: Path, data: dict) -> int:
    if SERVER_KEY not in data["mcpServers"]:
        print(f"  '{SERVER_KEY}' is not registered -- run `install` instead.")
        return 1
    data["mcpServers"][SERVER_KEY].pop("disabled", None)
    save_config(path, data)
    print(f"  {GREEN}enabled{OFF} '{SERVER_KEY}'")
    reload_hint()
    return 0


def cmd_disable(path: Path, data: dict) -> int:
    if SERVER_KEY not in data["mcpServers"]:
        print(f"  '{SERVER_KEY}' is not registered; nothing to disable.")
        return 0
    data["mcpServers"][SERVER_KEY]["disabled"] = True
    save_config(path, data)
    print(f"  {YELLOW}disabled{OFF} '{SERVER_KEY}'")
    reload_hint()
    return 0


def cmd_uninstall(path: Path, data: dict) -> int:
    if data["mcpServers"].pop(SERVER_KEY, None) is None:
        print(f"  '{SERVER_KEY}' was not registered; nothing to do.")
        return 0
    save_config(path, data)
    print(f"  removed '{SERVER_KEY}'")
    reload_hint()
    return 0


COMMANDS = {
    "status": cmd_status,
    "install": cmd_install,
    "enable": cmd_enable,
    "disable": cmd_disable,
    "uninstall": cmd_uninstall,
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    args = ap.parse_args()

    path = config_path()
    print(f"config: {path}")
    return COMMANDS[args.command](path, load_config(path))


if __name__ == "__main__":
    sys.exit(main())
