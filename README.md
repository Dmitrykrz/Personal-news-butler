# Personal News Butler

A personal news digest that lives in a private Telegram chat. A few times a day it collects
articles from the sources you chose (RSS feeds, Hacker News, GitHub trending, Google News),
and Claude picks the few worth your time and writes a short summary of each.

You configure it by talking to it. "Less crypto." "Add Ars Technica, every other day."
"Send the evening digest at 20:00." Each message starts a Claude Code session that edits the
configuration and tells you what it changed. `/undo` reverts it. With `/dev` it can even fix
or extend the bot's own code.

Runs in Docker on a Linux server. Uses your Claude subscription (Pro or Max) through Claude
Code; no API key, nothing billed per call.

## Quickstart

You need: an Ubuntu server with Docker and the Compose plugin, a Claude Pro or Max
subscription, and Telegram.

**1. Get the code**

```sh
git clone https://github.com/Dmitrykrz/Personal-news-butler.git /opt/news-agent
cd /opt/news-agent
cp .env.example .env
```

**2. Create a Claude token.** On any machine with Claude Code installed
(`npm install -g @anthropic-ai/claude-code`), run:

```sh
claude setup-token
```

Log in when asked and copy the long-lived token it prints. Put it into `.env` as
`CLAUDE_CODE_OAUTH_TOKEN=...`.

**3. Create a Telegram bot.** In Telegram, open [@BotFather](https://t.me/BotFather), send
`/newbot` and follow the questions. Put the token it gives you into `.env` as
`TELEGRAM_BOT_TOKEN=...`.

**4. Set your timezone.** Schedule times are in your local time, so this is required. Put a
[tz database name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) into `.env`,
for example `TZ=Europe/Berlin` or `TZ=America/New_York`.

**5. Start it**

```sh
chown -R 1000:1000 .            # the bot runs as uid 1000 and edits its own files
docker compose up -d --build
```

**6. Connect your chat.** Open your bot in Telegram and send it any message. It replies with
your chat id. Put it into `.env` as `TELEGRAM_CHAT_ID=...`, then reload the environment:

```sh
docker compose up -d --force-recreate
```

(A plain `docker compose restart` does not re-read `.env`.) From now on the bot ignores every
other chat.

**7. Tell it what you want.** The bot checks its setup and writes to you: if the Claude token
or timezone is wrong it says so, otherwise it says it has no rules yet. Answer in plain
language, for example:

> I'm a backend developer in Berlin. Send me the top Hacker News stories, one practical
> Python tip a day, big space news, and the most important local news from Berlin twice a
> week. No crypto, no sports. 3 to 6 items a day, at 8:00 and 19:00, in English.

Claude creates your rules and taste profile from that and confirms. Send `/fire_once` to get a
digest right away.

## Using it

| Command | |
|---|---|
| `/fire_once` | run a digest now (takes a few minutes) |
| `/status` | current time, schedule, which rules are due in each run |
| `/undo` | revert the last change the bot made |
| `/dev <request>` | change the bot's code (see [Security](#security)) |

Anything else goes to Claude, which can only edit your configuration:

- **Taste feedback:** "too much AI news", "I liked this one". To talk about a specific item,
  reply to its message.
- **Rule changes:** "add the Cloudflare blog, weekly", "France news every day", "max 5 items".
  The bot test-fetches new sources and warns you if one doesn't work.
- **Questions:** "why didn't I get Hacker News today?" It reads the logs and run records.

Reply **deeper** to a news message to see the bigger picture: the bot finds the same story
on 4-6 outlets from your media map (different leanings, countries and owners), reads them and
tells you what they agree on and how each one frames it.

For bugs and features use `/dev`: "/dev the summaries are cut off", "/dev add a source type
for Mastodon". Claude then gets a shell, changes the code, and the bot checks the change,
commits it and restarts itself.

## Configuration files

Everything you set through the chat lives in three files. They are personal and gitignored, so
you can update the code without conflicts:

- `config/rules.yaml`: sources, schedule, how often and how many. The full schema is in the
  comment at the top of [`config/rules.example.yaml`](config/rules.example.yaml).
- `config/taste.md`: who you are and what you like, in prose
  ([example](config/taste.example.md)).
- `config/media.yaml`: the outlets "deeper" compares, with their leaning, stance on the EU and
  Russia, reliability and owner. Created from [the example](config/media.example.yaml) on first use.

You can also edit them by hand; the bot rereads them on every run. To start from the examples
instead of the chat:

```sh
cp config/rules.example.yaml config/rules.yaml
cp config/taste.example.md config/taste.md
```

Source types: `rss` (any RSS/Atom feed), `hn` (Hacker News stories above `min_points`),
`github_trending`, and `gnews` (a Google News RSS URL; any search on news.google.com can be
turned into one). Avoid paywalled sites: the bot can't read them.

## How it works

Each run, the bot finds the rules that are due, fetches their sources, and drops anything it
has sent before. If there are many candidates, Claude first shortlists them by title. Then
the bot downloads the article texts, and a final Claude call (allowed to search the web)
chooses the items and writes the summaries. Every item is one Telegram message.

Each run's prompts and Claude's replies are saved in `data/runs/`, and the log is
`data/bot.log`, so you (or the bot, when you ask it) can see why something was or wasn't sent.

Code layout is described in [CLAUDE.md](CLAUDE.md), which is also the briefing the Telegram
agent reads.

## Maintenance

```sh
docker compose logs -f                                          # live log
docker compose run --rm newsbot python -m newsagent fetch       # test all sources, no Claude
docker compose run --rm newsbot python -m newsagent digest --dry-run   # full run, nothing sent
```

**Updating:** `git pull && docker compose up -d --build`. If you asked the bot to change its
own code, those changes are commits in your local repo and may conflict with upstream.

**Claude token expired:** the bot tells you in Telegram. Run `claude setup-token` again, update
`.env`, then `docker compose up -d --force-recreate`.

Google News links are resolved through an undocumented Google endpoint. It is slow on purpose
(Google rate-limits it) and may stop working one day; Claude then finds the articles by web
search instead.

## Security

Only your chat can talk to the bot; messages from other chats are ignored. The remaining risk
is prompt injection: news articles are text written by strangers, and Claude reads them.

- **Digest runs:** Claude only gets WebSearch and WebFetch; no shell, no files. A malicious
  article could at worst get itself picked or summarized misleadingly.
- **Your messages (default):** Claude can read the project and edit only `config/`. It has no
  shell and no WebFetch, and `.env` is blocked by Claude Code's permission rules. Article text
  it reads in logs could at worst change your config, which `/undo` reverts.
- **`/dev` messages:** Claude gets a shell inside the container, which holds your Telegram and
  Claude tokens, and can change code. Use it for code work only, and avoid asking it to read
  run records or web pages at the same time.

The container runs as an unprivileged user with only the project folder mounted. Keep it that
way: don't mount anything else into it, and use a Telegram bot token you don't use elsewhere.

## License

[MIT](LICENSE)
