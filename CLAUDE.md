# News agent

Personal news digest bot. Runs in Docker, sends news to its owner's private Telegram chat, and
takes prose feedback there. Each feedback message starts a headless Claude Code session
(`claude -p`) in this directory, inside the container. See README.md for setup.

## If you are a Claude session outside the container (developing)
- Don't start a second bot (`docker compose up`) with the same Telegram token as a live one:
  two bots polling one token break each other. Test with
  `docker compose run --rm newsbot python -m newsagent fetch` or `... digest --dry-run`.
- Personal deployment notes (server address, sync commands) go in `CLAUDE.local.md`, which is
  gitignored. Never put them in this file or the README.

If you are the agent inside the container, ignore this section.

## How it works
- `newsagent/bot.py`: Telegram long polling + scheduler thread. Scheduled runs are
  `settings.schedule` in `config/rules.yaml` (local time in the `TZ` timezone from `.env`).
  `/fire_once` = manual run, `/status`, `/undo`. Other text goes to `feedback.py`.
  At startup it checks TZ, the Claude token and the config, and reports problems in Telegram.
- `newsagent/digest.py`: one run. Picks DUE rules (calendar days since last run, `run:` slot),
  fetches candidates (`sources.py`), drops already-sent URLs, Claude shortlist (titles only) when
  there are many, resolves Google News redirects (`gnews.py`, rate-limited by Google: slow on
  purpose), downloads article text (`extract.py`, unless the feed already carries it), then the
  final Claude call (with WebSearch/WebFetch) chooses items and writes summaries as JSON.
  Each item becomes one Telegram message: bold title, summary, link.
- `newsagent/feedback.py`: the agent session you are probably running in right now. After you
  finish, the bot validates (rules.yaml loads, Python compiles). Code changes become a git
  commit and restart the container; config changes are recorded in the DB so `/undo` can
  restore the previous version. If validation fails, all your changes are reverted.
- `newsagent/claude.py`: runs `claude -p` isolated (`--setting-sources ""`, `--strict-mcp-config`,
  explicit `--tools`). Auth is the subscription token in `CLAUDE_CODE_OAUTH_TOKEN`, never an API key.
- State: `data/newsbot.db` (SQLite: sent items, rule state, feedback log, change log for /undo).

## Config
- `config/rules.yaml`: sources, schedules, counts, priorities. Schema in the comment at the top
  of `config/rules.example.yaml`.
- `config/taste.md`: the reader's taste in prose. Soft preferences go here.
- Both are personal and gitignored. The `*.example.*` files next to them are the shared
  templates: don't put the owner's preferences there.

## Debugging
- Bot log: `data/bot.log` (rotating). Each digest run saves its prompts and Claude replies in
  `data/runs/<timestamp>-<run>/` (1-shortlist-*, 2-final-*, 3-sent.json).
- `python -m newsagent fetch [rule-id]`: test sources without Claude.
- `python -m newsagent digest --dry-run`: full run printed to stdout, nothing sent, no state change.
- `python -m compileall -q newsagent`: syntax check.

## Rules for the agent
- Don't run `git commit`, `git reset`, or anything that rewrites history: the bot commits for you.
- Never touch `.env` or print secrets.
- New Python packages: add them to `requirements.txt` (pinned). They are installed when the
  container restarts. Changes to `Dockerfile`/`docker-compose.yml` need the owner to rebuild
  (`docker compose up -d --build`): say so in your answer.
- No paywalled sources. Keep Google News decoding slow.
- You can't run docker commands or reach the host; you are inside the container.
