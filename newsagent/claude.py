"""Run headless Claude Code (`claude -p`) and parse its answer."""
import json
import re
import subprocess
import tempfile

from .config import CLAUDE_MODEL, CLAUDE_TOKEN, log

TOKEN_HELP = ("Run `claude setup-token` on any machine with Claude Code, put the result into .env "
              "as CLAUDE_CODE_OAUTH_TOKEN=..., and restart the container.")
AUTH_ERROR = re.compile(r"\b401\b|authenticat|unauthori|invalid api key|oauth token|/login", re.I)


class ClaudeError(RuntimeError):
    pass


def _error(details):
    if AUTH_ERROR.search(details):
        return ClaudeError(f"Claude rejected the token (wrong or expired). {TOKEN_HELP}\n({details[-300:]})")
    return ClaudeError(details)


def run_claude(prompt, tools=(), allow=None, deny=(), cwd=None, permission_mode=None, timeout=900,
               model=None) -> str:
    """Run one non-interactive Claude turn. `tools` is the full list of tools Claude has.
    `allow` are the permission rules it may use without asking (default: all of `tools`), e.g.
    "Edit(config/**)"; anything not allowed is refused, since nobody can approve it. `deny` wins over allow."""
    if not CLAUDE_TOKEN:
        raise ClaudeError(f"CLAUDE_CODE_OAUTH_TOKEN is not set. {TOKEN_HELP}")
    # isolated: no user/project settings, no MCP servers, only the tools we name
    cmd = ["claude", "-p", "--model", model or CLAUDE_MODEL, "--output-format", "json",
           "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
           "--tools", ",".join(tools)]
    allow = tools if allow is None else allow
    if allow:
        cmd += ["--allowedTools", ",".join(allow)]
    if deny:
        cmd += ["--disallowedTools", ",".join(deny)]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout, cwd=cwd or tmp)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise _error(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}") from None
    if out.get("is_error"):
        raise _error(f"claude error: {str(out.get('result'))[-800:]}")
    # on a subscription token nothing is billed; total_cost_usd is only a measure of usage
    log(f"  claude: {out.get('duration_ms', 0) / 1000:.0f}s, {out.get('num_turns')} turns, "
        f"usage ~{out.get('total_cost_usd', 0):.2f} API-equivalent (not billed on subscription)")
    return out.get("result", "")


def extract_json(text):
    """Pull the JSON object out of Claude's reply (bare or inside a ```json fence)."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ClaudeError(f"could not parse JSON from Claude: {e}; reply was: {text[:500]}") from e
