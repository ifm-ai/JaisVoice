#!/usr/bin/env python3
"""Replace this process with the Pi agent in RPC mode, owned by the native app.

stdin/stdout carry Pi's JSONL protocol; diagnostics go to stderr. The app
closes stdin to stop the agent. Configuration comes from `agent/` and runtime
state (credentials store, caches) stays under `.runtime/pi-agent`.
"""
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RUNTIME = Path(os.environ.get("HORIZON_RUNTIME_DIR", ROOT / ".runtime"))
CONFIG_FILES = ("settings.json", "models.json", "SYSTEM.md")


def find_node(env: dict) -> str:
    node = env.get("HORIZON_NODE") or shutil.which("node")
    for candidate in (node, "/opt/homebrew/bin/node", str(Path.home() / ".local/bin/node"), "/usr/local/bin/node"):
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Node.js not found. Run the JAIS Voice setup again.")


def agent_effort(env: dict) -> str:
    """Startup level only: the app applies each model's catalog effort when it selects it."""
    effort = env.get("HORIZON_AGENT_REASONING_EFFORT") or "low"
    if effort not in {"low", "medium", "high"}:
        raise ValueError(f"HORIZON_AGENT_REASONING_EFFORT={effort!r} must be low, medium or high.")
    return effort


def command(env: dict) -> tuple[list[str], dict, Path]:
    node = find_node(env)
    agent_dir = RUNTIME / "pi-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for name in CONFIG_FILES:
        shutil.copyfile(ROOT / "agent" / name, agent_dir / name)
    workspace = RUNTIME / "agent-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    env.update({
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
        "HORIZON_PROJECT_ROOT": str(ROOT),
        "HORIZON_RUNTIME_DIR": str(RUNTIME),
        "HORIZON_NODE": node,
        "HORIZON_PYTHON": sys.executable,
        "HORIZON_PLAYWRIGHT_CLI": str(ROOT / "node_modules/@playwright/mcp/cli.js"),
        # The packaged app ships Chromium read-only in its bundle.
        "PLAYWRIGHT_BROWSERS_PATH": env.get("PLAYWRIGHT_BROWSERS_PATH") or str(RUNTIME / "browsers"),
        "PATH": os.pathsep.join([str(Path(node).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
    })
    thinking = agent_effort(env)
    argv = [
        node, str(ROOT / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"),
        "--mode", "rpc", "--no-session",
        "--no-builtin-tools", "--no-extensions", "-e", "builtin:mcp", "-e", str(ROOT / "agent/extensions/jais"),
        "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
        "--provider", "jais", "--model", "k2-3.7b", "--thinking", thinking,
    ]
    return argv, env, workspace


def main() -> None:
    argv, env, workspace = command(os.environ)
    os.chdir(workspace)
    os.execve(argv[0], argv, env)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        sys.exit(1)
