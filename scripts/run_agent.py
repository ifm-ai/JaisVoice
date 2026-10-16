#!/usr/bin/env python3
"""Replace this process with the Pi agent in RPC mode, owned by the native app.

stdin/stdout carry Pi's JSONL protocol; diagnostics go to stderr. The app
closes stdin to stop the agent. Configuration and skills come from `agent/`
and runtime state (credentials store, caches) stays under `.runtime/pi-agent`.
Skills are read in place: the user's own, in `.runtime/skills`, before the
app's in `agent/skills`.
Pi runs from the home folder, so its file tools resolve paths as the user
says them; the browser and other helpers work in `.runtime/agent-workspace`.
When the app sets JAIS_REMOTE_BASE_URL and JAIS_REMOTE_MODEL, the agent's
models also list that OpenAI-compatible API as the `remote` provider; its key
stays in the environment (JAIS_REMOTE_API_KEY), never in a file.
"""
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get("JAIS_RUNTIME_DIR", ROOT / ".runtime"))
CONFIG_FILES = ("settings.json", "SYSTEM.md")
REMOTE_PROVIDER = "remote"
# Pi needs these numbers; they bound what it sends, the API enforces its own limits.
REMOTE_CONTEXT, REMOTE_MAX_TOKENS = 128000, 16384


def find_node(env: dict) -> str:
    node = env.get("JAIS_NODE") or shutil.which("node")
    for candidate in (node, "/opt/homebrew/bin/node", str(Path.home() / ".local/bin/node"), "/usr/local/bin/node"):
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Node.js not found. Run the Jais Voice setup again.")


def models_config(env: dict) -> dict:
    """agent/models.json, plus the remote API the app chose, if any."""
    config = json.loads((ROOT / "agent" / "models.json").read_text())
    base_url, model = env.get("JAIS_REMOTE_BASE_URL", "").strip(), env.get("JAIS_REMOTE_MODEL", "").strip()
    if base_url and model:
        reasoning = env.get("JAIS_REMOTE_REASONING") == "1"
        config["providers"][REMOTE_PROVIDER] = {
            "baseUrl": base_url.rstrip("/"),
            "api": "openai-completions",
            "apiKey": "$JAIS_REMOTE_API_KEY",
            # Some reasoning APIs refuse a conversation whose assistant turns drop
            # `reasoning_content`: Pi then replays it, empty when the model did not think.
            "compat": {"supportsDeveloperRole": False, "requiresReasoningContentOnAssistantMessages": True},
            "models": [{
                "id": model, "name": f"{model} (API)", "reasoning": reasoning, "input": ["text"],
                "contextWindow": REMOTE_CONTEXT, "maxTokens": REMOTE_MAX_TOKENS,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }],
        }
    return config


def command(env: dict) -> tuple[list[str], dict, Path]:
    node = find_node(env)
    agent_dir = RUNTIME / "pi-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for name in CONFIG_FILES:
        shutil.copyfile(ROOT / "agent" / name, agent_dir / name)
    (agent_dir / "models.json").write_text(json.dumps(models_config(env), indent=2))
    # Skills are read where they are, again for each new conversation: the user's own first, so
    # one named like an app skill replaces it, then the app's. Earlier versions copied them here.
    shutil.rmtree(agent_dir / "skills", ignore_errors=True)
    user_skills, app_skills = RUNTIME / "skills", ROOT / "agent" / "skills"
    user_skills.mkdir(parents=True, exist_ok=True)
    workspace = RUNTIME / "agent-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    env.update({
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
        "JAIS_PROJECT_ROOT": str(ROOT),
        "JAIS_RUNTIME_DIR": str(RUNTIME),
        "JAIS_NODE": node,
        "JAIS_PLAYWRIGHT_CLI": str(ROOT / "node_modules/@playwright/mcp/cli.js"),
        "JAIS_AGENT_WORKSPACE": str(workspace),
        "JAIS_USER_SKILLS": str(user_skills),
        "JAIS_APP_SKILLS": str(app_skills),
        # Pi refuses an empty key; servers without authentication ignore this one.
        "JAIS_REMOTE_API_KEY": env.get("JAIS_REMOTE_API_KEY") or "none",
        # Without Google Chrome, the agent downloads Chromium there when it starts.
        "PLAYWRIGHT_BROWSERS_PATH": env.get("PLAYWRIGHT_BROWSERS_PATH") or str(RUNTIME / "browsers"),
        # rg and fd for Pi's grep and find tools come first.
        "PATH": os.pathsep.join([env.get("JAIS_TOOLS_BIN") or str(RUNTIME / "bin"), str(Path(node).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
    })
    argv = [
        node, str(ROOT / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"),
        "--mode", "rpc", "--no-session",
        "--no-extensions", "-e", "builtin:mcp", "-e", str(ROOT / "agent/extensions/jais"),
        "--no-prompt-templates", "--no-themes", "--no-context-files",
        "--skill", str(user_skills), "--skill", str(app_skills),
    ]
    return argv, env, Path.home()


def main() -> None:
    argv, env, cwd = command(os.environ)
    os.chdir(cwd)
    os.execve(argv[0], argv, env)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        sys.exit(1)
