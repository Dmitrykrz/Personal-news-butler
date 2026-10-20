"""Entry point: python -m newsagent {bot,digest,fetch,deeper}"""
import argparse

from .config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN, ensure_dirs, load_rules, log


def cmd_digest(args):
    from .bot import format_item
    from .digest import run_digest
    from .telegram import Telegram

    if args.dry_run:
        def send(item):
            log(f"\n--- [{item['rule_id']}/{item['kind']}]\n{item['title']}\n{item['summary']}\n{item['url']}")
    else:
        tg = Telegram(TELEGRAM_TOKEN)
        def send(item):
            return tg.send(int(TELEGRAM_CHAT_ID), format_item(item), html=True)
    run_digest(send, run_name=args.run, dry_run=args.dry_run)


def cmd_deeper(args):
    """Debug: run "deeper" for any article URL and print the report (nothing sent)."""
    from .deeper import dig_deeper

    print(dig_deeper({"title": args.title or args.url, "summary": "", "url": args.url}))


def cmd_fetch(args):
    """Debug: fetch every source of every rule (no Claude, no state changes)."""
    from .sources import fetch_source

    for rule in load_rules()["rules"]:
        if args.rule and rule["id"] != args.rule:
            continue
        for src in rule["sources"]:
            try:
                cands = fetch_source(rule["id"], src, rule["lookback_days"])
                log(f"{rule['id']:16} {src['name']:28} {len(cands):3} items  {cands[0].title[:60] if cands else ''}")
            except Exception as e:
                log(f"{rule['id']:16} {src['name']:28} ERROR {e}")


def main():
    p = argparse.ArgumentParser(prog="newsagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bot", help="run the Telegram listener and scheduler")
    d = sub.add_parser("digest", help="run one digest from the command line")
    d.add_argument("--run", help="schedule slot name (e.g. morning); default: manual run")
    d.add_argument("--dry-run", action="store_true", help="print instead of sending; don't change state")
    f = sub.add_parser("fetch", help="test all sources without Claude")
    f.add_argument("rule", nargs="?")
    dp = sub.add_parser("deeper", help="compare outlets' coverage of an article, print the report")
    dp.add_argument("url")
    dp.add_argument("title", nargs="?")
    args = p.parse_args()

    ensure_dirs()
    if args.cmd == "bot":
        from .bot import main as bot_main
        bot_main()
    elif args.cmd == "digest":
        cmd_digest(args)
    elif args.cmd == "deeper":
        cmd_deeper(args)
    else:
        cmd_fetch(args)


if __name__ == "__main__":
    main()
