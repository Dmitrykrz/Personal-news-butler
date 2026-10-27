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
               model=None, schema=None) -> str:
    """Run one non-interactive Claude turn. `tools` is the full list of tools Claude has.
    `allow` are the permission rules it may use without asking (default: all of `tools`), e.g.
    "Edit(config/**)"; anything not allowed is refused, since nobody can approve it. `deny` wins over allow.
    With a JSON `schema`, the CLI validates Claude's answer against it and the reply is that JSON."""
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
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
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
    if schema and isinstance(out.get("structured_output"), dict):
        return json.dumps(out["structured_output"], ensure_ascii=False)
    return out.get("result", "")


# Claude sometimes closes a JSON string with a curly quote (seen 2026-10-24: `...unavailable).”}`).
# Suspects are curly quotes next to JSON punctuation; curly quotes inside the text can look the
# same (`“bold”, then`), so each suspect is tried on its own before all of them together.
SUSPECT_QUOTE = re.compile(r'[“”](?=\s*[,:}\]])|(?<=[{\[,:])[“”]|(?<=[{\[,:] )[“”]')
MAX_SUSPECTS = 20


def _repairs(text):
    """The reply as is, then variants with suspect curly quotes made straight."""
    yield text
    spots = [m.start() for m in SUSPECT_QUOTE.finditer(text)][:MAX_SUSPECTS]
    for i in spots:
        yield text[:i] + '"' + text[i + 1:]
    if len(spots) > 1:
        yield "".join('"' if i in spots else ch for i, ch in enumerate(text))


def _objects(text, strict=True):
    """Every top-level JSON object in `text`, in order (prose and ``` fences around them are skipped)."""
    decoder = json.JSONDecoder(strict=strict)
    found, i = [], text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            end = i + 1
        else:
            if isinstance(obj, dict):
                found.append(obj)
        i = text.find("{", end)
    return found


def extract_json(text, key):
    """Pull the answer object (the one with `key`) out of Claude's reply. If there are several, the
    last wins: Claude sometimes corrects itself ("Wait — ... Final answer: {...}", seen 2026-10-26).
    The error message never includes the reply itself: it is saved in the run folder."""
    for variant in _repairs(text):
        for strict in (True, False):  # strict=False also accepts raw newlines inside strings
            matches = [o for o in _objects(variant, strict) if key in o]
            if matches:
                return matches[-1]
    try:
        json.loads(text[text.find("{"):])
        detail = f"no object with {key!r}"
    except json.JSONDecodeError as e:
        detail = str(e)
    raise ClaudeError(f"could not parse JSON from Claude ({detail})")
