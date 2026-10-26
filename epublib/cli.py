"""Hondana's command line: the pipeline stages, the server and the maintenance tools.

Usage: hondana <command> [--config PATH]   (or `uv run cli.py …` from a checkout;
`--help` lists them all)

    <stage>   scan | analyze | enrich | pair | audio-scan | xref | build — the pipeline
              stages, defined once in epublib/pipeline.py (also what /admin runs)
    all       every stage in order
    push      back up the hand-curated data/ files to a git repo (off unless configured; see epublib/data_mirror.py)
    seed-known [--top-n N]   seed active profile's known words with wordfreq top-N (en/ja)
    serve     run the web server
    desktop   tray app that keeps the server running (what the packaged app starts)
    --self-test   check that this installation works, then exit
    config [--json]   show every setting, its value and where it came from
    admin-password set|clear|status   the optional admin password (clear = the way back in)
    token create|list|revoke   script tokens (Authorization: Bearer) for align-remote and the like
    consolidate [--execute]   unify audiobook dirs (dry-run default)
    recover-moves   re-attach curated state orphaned by moving a file between folders
    gc [--apply]    report (and with --apply remove) data left behind by books no longer in the library
    align-audit [book_id …]   check alignment maps against their audio for gaps the narrator talks through (runs ffmpeg)
    align-remote <book>|--missing   align a server's audiobook on this machine's GPU (SubPlz) and upload the SRT

Each stage is resume-safe; re-running only does the new/changed work.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from epublib import align as align_mod
from epublib import align_remote as align_remote_mod
from epublib import annotate as annotate_mod
from epublib import audiobook_scanner as audiobook_scanner_mod
from epublib import build as build_mod
from epublib import config as config_mod
from epublib import consolidate as consolidate_mod
from epublib import data_mirror as data_mirror_mod
from epublib import desktop as desktop_mod
from epublib import known as known_mod
from epublib import orphans as orphans_mod
from epublib import pipeline as pipeline_mod
from epublib import procs as procs_mod
from epublib import profiles as profiles_mod
from epublib import rekey as rekey_mod
from epublib import selftest as selftest_mod
from epublib import stdio as stdio_mod
from epublib.config import FROZEN
from epublib.index_store import load_json


def _cfg(args):
    """Every command shares this, so cfg.active_user is always the registered
    active profile rather than the dataclass default "default" — otherwise
    any command whose chain touches known.py (analyze's internal recompute,
    seed-known, ...) silently reads/writes a throwaway unregistered profile
    instead of the real one. server.py does the same at import (server.py:51)."""
    cfg = config_mod.load(args.config)
    profiles_mod.ensure_initialized(cfg)
    return cfg


def cmd_stage(args):
    """Every pipeline stage (and `all`): the registry entry rides on the parser."""
    if getattr(args, "dry_run", False):  # audio-scan --dry-run: report, write nothing
        audiobook_scanner_mod.scan(_cfg(args), dry_run=True)
        return
    args.stage.run(_cfg(args), getattr(args, "refresh", False))


def cmd_consolidate(args):
    cfg = _cfg(args)
    plan = consolidate_mod.build_plan(cfg, use_fuzzy=not args.no_fuzzy)
    consolidate_mod.print_report(cfg, plan)
    if args.execute:
        consolidate_mod.execute(
            cfg, plan, clean_junk=args.clean_junk, verify_hash=args.verify_hash
        )
        audiobook_scanner_mod.scan(cfg)
        audiobook_scanner_mod.build_index(cfg)


def cmd_push(args):
    data_mirror_mod.push(_cfg(args))


def cmd_seed_known(args):
    cfg = _cfg(args)
    lang = profiles_mod.active_language(cfg)
    try:
        saved = known_mod.seed_common(cfg, lang, top_n=args.top_n)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    stats = known_mod.recompute(cfg)
    build_mod.build(cfg)
    print(
        f"Seeded {saved['count']} words ({saved['source']}) for the active "
        f"'{lang}' profile; {len(stats)} books recomputed."
    )


def cmd_recover_moves(args):
    cfg = _cfg(args)
    library = load_json(cfg.library_index, {})
    recovered = rekey_mod.recover_orphans(cfg, library)
    for old, new in recovered.items():
        print(f"  ⇄ {old} → {new}  {library[new].get('path')}")
    print(f"Recovered curated state for {len(recovered)} moved book(s).")
    if recovered:
        known_mod.recompute_all(cfg)
        build_mod.build(cfg)


def cmd_gc(args):
    cfg = _cfg(args)
    library = load_json(cfg.library_index, {})
    orphans_mod.run(cfg, library, do_apply=args.apply)


def cmd_align_audit(args):
    """Check alignment maps against their audio for gaps the narrator talks through."""
    cfg = _cfg(args)
    library = load_json(cfg.library_index, {})
    ids = args.book or [b for b in library if align_mod.map_path(cfg, b).exists()
                        or align_mod.source_kind(cfg, b, None)]
    worst = 0
    for book_id in ids:
        book = library.get(book_id)
        if not book:
            print(f"{book_id}: not in the library", file=sys.stderr)
            continue
        try:
            m = align_mod.audit(cfg, book)
        except (RuntimeError, align_mod.AlignError, annotate_mod.NoPack) as exc:
            print(f"{book_id}: {exc}", file=sys.stderr)
            continue
        if m is None:
            print(f"{book_id}  {book.get('title')}: no alignment, or its audiobook isn't on this machine")
            continue
        q = m["quality"]
        worst = max(worst, q["speech_gaps"])
        print(f"{book_id}  {book.get('title')}: {m['source']}, coverage {m['coverage']:.1%}, "
              f"{q['speech_gaps']} gaps with speech ({q['speech_gap_s'] / 60:.1f} min)")
    return 1 if worst >= args.fail_at else 0


def cmd_align_remote(args):
    """Fetch a server book's text and audio, run this machine's `transcribe_cmd` (or
    `subplz_cmd`), upload the SRTs."""
    ar = align_remote_mod
    if bool(args.book) == bool(args.missing):
        print("error: give a book (book_id or title) or --missing", file=sys.stderr)
        return 2
    if args.run and not args.missing:
        print("error: --run goes with --missing; name a book to align just it", file=sys.stderr)
        return 2
    # Without --config, this machine's ~/.config/hondana/align.toml supplies the
    # command and the server; the active repo config is only the fallback.
    try:
        mine = {} if args.config else ar.user_settings()
    except ar.RemoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if mine.get("transcribe_cmd") or mine.get("subplz_cmd"):
        tx_cmd, sync_cmd = mine.get("transcribe_cmd", ""), mine.get("subplz_cmd", "")
    else:
        fallback = config_mod.load(args.config)
        tx_cmd, sync_cmd = fallback.transcribe_cmd, fallback.subplz_cmd
    # A transcript is what fixes drift, so it is the default once there is a command for it.
    kind = args.kind or ("transcript" if tx_cmd else "synced")
    cmd = tx_cmd if kind == "transcript" else sync_cmd
    audio = [Path(a).expanduser() for a in args.audio]
    audio_dir = Path(args.audio_dir).expanduser() if args.audio_dir else None

    def plan_for(book, entries):
        return ar.make_plan(server, book, entries, audio, audio_dir)

    try:
        token = args.token or os.environ.get("HONDANA_TOKEN") or mine.get("token") or ""
        server = ar.Server(ar.find_server(args.server or mine.get("server")), token=token)
        entries = {e["ab_id"]: e for e in server.json("/api/audiobooks")}
        books = ([ar.find_book(server.books(), args.book)] if args.book else ar.candidates(server))
        plans = []
        for b in books:
            try:
                plans.append(plan_for(b, entries))
            except ar.RemoteError as exc:
                if args.book:
                    raise
                print(f"skipped: {exc}", file=sys.stderr)
    except ar.RemoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.missing:
        print(f"{len(plans)} book(s) on {server.base} with a local audiobook and no alignment:")
    for p in plans:
        print("  " + ar.describe(p))
    if args.missing:
        hours = sum((p.runtime_min or 0) for p in plans) / 60
        todo = [p.to_download() for p in plans]
        got = "" if None in todo else f", {ar.human_bytes(sum(todo))} to download"
        print(f"  total: {hours:.1f} h of audio{got}")
    if args.dry_run or (args.missing and not args.run):
        if args.missing and not args.run and not args.dry_run:
            print("(add --run to align them one after another)")
        return 0

    # A laptop that sleeps kills a four-hour run; SIGTERM unwinds like Ctrl-C.
    ar.keep_awake()
    def _terminate(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    failed = 0
    began = time.time()
    try:
        for n, p in enumerate(plans, 1):
            print(f"\n[{n}/{len(plans)}] {p.title}")
            try:
                r = ar.align_book(server, p, cmd, kind=kind, force=args.force, keep_audio=args.keep_audio)
            except ar.RemoteError as exc:
                failed += 1
                print(f"error: {exc}", file=sys.stderr)
                if not args.missing:
                    break
                continue
            if r.skipped:
                print(r.skipped)
                continue
            m = r.response
            guessed = f", {m['estimated']} estimated" if m.get("estimated") else ""
            print(f"{'aligned' if m['ok'] else 'POOR MATCH'}: coverage {m['coverage']:.1%} "
                  f"({m['matched']}/{m['total']} sentences heard{guessed}; "
                  f"{m.get('source', 'synced')}) in {ar.human_time(r.seconds)}")
            if not m["ok"]:
                failed += 1
                print("The server stored it but below the 90% the reader needs to highlight; "
                      "transcripts kept in " + str(ar.work_dir(server, r.book_id)), file=sys.stderr)
    except KeyboardInterrupt:
        print("\nInterrupted. Finished files are kept; run the same command to resume.", file=sys.stderr)
        return 130
    print(f"\nElapsed {ar.human_time(time.time() - began)}.")
    return 1 if failed else 0


def cmd_admin_password(args):
    """Set, remove or report the admin password (auth.py). Shell access is the way
    back in when it's forgotten, so `clear` asks for nothing."""
    import getpass

    from epublib import auth as auth_mod

    cfg = config_mod.load(args.config)
    if args.action == "status":
        if auth_mod.password_set(cfg):
            print("Admin password: set")
        else:
            print("Admin password: not set (anyone on the network can change things)")
        return 0
    if args.action == "clear":
        auth_mod.clear_password(cfg)
        print("Admin password removed; every browser is signed out.")
        return 0
    if args.stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New admin password: ")
        if getpass.getpass("Again: ") != password:
            print("error: the two don't match", file=sys.stderr)
            return 2
    try:
        auth_mod.set_password(cfg, password)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("Admin password set; other browsers will need it next time.")
    return 0


def cmd_token(args):
    """Script tokens for `Authorization: Bearer` (align-remote --token)."""
    import datetime as dt

    from epublib import auth as auth_mod

    cfg = config_mod.load(args.config)
    if args.action == "create":
        row, token = auth_mod.create_token(cfg, args.name or "")
        print(f"Token {row['id']} ({row['name']}): {token}")
        print("It isn't shown again.")
        return 0
    if args.action == "revoke":
        if not args.name or not auth_mod.revoke_token(cfg, args.name):
            print("error: no token with that id (see `token list`)", file=sys.stderr)
            return 2
        print(f"Revoked {args.name}.")
        return 0
    rows = auth_mod.list_tokens(cfg)
    if not rows:
        print("No tokens.")
    for r in rows:
        made = dt.datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d")
        used = (dt.datetime.fromtimestamp(r["last_used"]).strftime("%Y-%m-%d")
                if r.get("last_used") else "never")
        print(f"  {r['id']}  {r['name']}  (made {made}, last used {used})")
    return 0


def cmd_config(args):
    """Print every setting's effective value and where it came from."""
    import json

    from epublib import settings as settings_mod

    cfg = config_mod.load(args.config)
    rows = settings_mod.describe(cfg)
    if args.json:
        print(json.dumps({"config_path": str(cfg.config_path) if cfg.config_path else None,
                          "home": str(cfg.home), "settings": rows}, ensure_ascii=False, indent=2))
        return
    print(f"config file: {cfg.config_path or '(none: defaults)'}")
    print(f"home:        {cfg.home}")
    width = max(len(r["key"]) for r in rows)
    for r in rows:
        value = r["value"]
        shown = ", ".join(value) if isinstance(value, list) else ("" if value is None else str(value))
        print(f"  {r['key']:<{width}}  {shown or '-'}   [{r['source']}]")


def cmd_serve(args):
    import os
    import uvicorn

    # Make --config visible to the server process (it loads config at import).
    if args.config:
        os.environ[config_mod.CONFIG_ENV] = str(Path(args.config).expanduser().resolve())
    cfg = _cfg(args)
    stdio_mod.redirect_to_log(cfg.data_dir / "logs" / "server.log")  # before uvicorn sets up logging
    parent = os.environ.get(desktop_mod.PARENT_ENV, "")
    if parent.isdigit():  # started by the desktop app: stop with it
        procs_mod.watch_parent(int(parent), procs_mod.interrupt_self)
    uvicorn.run("epublib.server:app", host=cfg.host, port=cfg.port, reload=args.reload)


def cmd_desktop(args):
    return desktop_mod.run(library=args.library, open_browser=not args.no_browser, tray=not args.no_tray)


def main(argv=None):
    stdio_mod.make_output_safe()
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--self-test" in argv:
        return selftest_mod.run()
    if FROZEN and not argv:  # double-clicking the packaged app
        argv = ["desktop"]
    parser = argparse.ArgumentParser(description="Hondana: your Japanese EPUB and audiobook library")
    parser.add_argument("--config", help="Path to config.toml", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    # `scan`, `analyze`, … and `all` come from the stage registry.
    for stage in (*pipeline_mod.STAGES, pipeline_mod.ALL):
        p = sub.add_parser(stage.id, help=stage.help)
        if stage.refresh_help:
            p.add_argument("--refresh", action="store_true", help=stage.refresh_help)
        if stage.id == "audio-scan":
            p.add_argument("--dry-run", action="store_true",
                           help="print the moves and id changes a scan would make; write nothing")
        p.set_defaults(func=cmd_stage, stage=stage)

    p_cons = sub.add_parser(
        "consolidate", help="Plan (and with --execute, perform) audiobook unification"
    )
    p_cons.add_argument("--execute", action="store_true", help="Perform the moves")
    p_cons.add_argument("--no-fuzzy", action="store_true", help="Disable fuzzy tier")
    p_cons.add_argument(
        "--clean-junk", action="store_true", help="Delete ._* / .DS_Store files"
    )
    p_cons.add_argument(
        "--verify-hash", action="store_true",
        help="sha256-verify cross-volume copies (slow)",
    )
    p_cons.set_defaults(func=cmd_consolidate)

    p_push = sub.add_parser(
        "push", help="Back up your hand-made data/ files to a git repo ([data_mirror] in the config)"
    )
    p_push.set_defaults(func=cmd_push)

    p_seed = sub.add_parser(
        "seed-known",
        help="Seed the active profile's known-words list with wordfreq's top-N "
        "common words (en/ja — see known.SEEDABLE_LANGUAGES)",
    )
    p_seed.add_argument(
        "--top-n", type=int, default=known_mod.SEED_TOP_N,
        help=f"How many top-frequency words to seed (default {known_mod.SEED_TOP_N})",
    )
    p_seed.set_defaults(func=cmd_seed_known)

    p_rec = sub.add_parser(
        "recover-moves",
        help="Re-attach read status/overrides orphaned by moving a file between folders",
    )
    p_rec.set_defaults(func=cmd_recover_moves)

    p_gc = sub.add_parser(
        "gc",
        help="Report data keyed by books no longer in the library (dry run; --apply removes it)",
    )
    p_gc.add_argument(
        "--apply", action="store_true",
        help="Delete the orphaned data, including curated state (read status, overrides)",
    )
    p_gc.set_defaults(func=cmd_gc)

    p_aa = sub.add_parser(
        "align-audit",
        help="Check alignment maps against their audio for gaps the narrator talks through (runs ffmpeg)",
    )
    p_aa.add_argument("book", nargs="*", help="book_ids (default: every book with an alignment)")
    p_aa.add_argument("--fail-at", type=int, default=10, metavar="N",
                      help="exit non-zero when a book has N or more such gaps (default 10)")
    p_aa.set_defaults(func=cmd_align_audit)

    p_ar = sub.add_parser(
        "align-remote",
        help="Align a server's audiobook here: fetch its text and audio, transcribe it, upload the SRT",
    )
    p_ar.add_argument("book", nargs="?", help="a book_id on the server, or a title substring")
    p_ar.add_argument("--token", default=None,
                      help="a script token, when the server has an admin password "
                      "(default: HONDANA_TOKEN, else `token` in the align.toml)")
    p_ar.add_argument("--server", default=None,
                      help="the library server (default: `server` in "
                      f"{align_remote_mod.USER_CONFIG}, else the one found on this network)")
    p_ar.add_argument("--missing", action="store_true",
                      help="list JP books with a local audiobook and no alignment")
    p_ar.add_argument("--run", action="store_true", help="with --missing: align them one after another")
    p_ar.add_argument("--dry-run", action="store_true", help="print the plan; start nothing")
    p_ar.add_argument("--force", action="store_true", help="redo a book that is already aligned")
    p_ar.add_argument("--kind", choices=("transcript", "synced"), default=None,
                      help="transcript: run transcribe_cmd (SubPlz gen) and upload what was heard; "
                      "synced: run subplz_cmd (SubPlz sync). Default: transcript when transcribe_cmd is set. "
                      "A book aligned from synced is redone by a transcript without --force")
    p_ar.add_argument("--audio", action="append", default=[], metavar="PATH",
                      help="use this copy of an audio file instead of downloading it "
                      "(matched by file name; repeatable)")
    p_ar.add_argument("--audio-dir", metavar="DIR",
                      help="use audio files found here (by file name) instead of downloading them")
    p_ar.add_argument("--keep-audio", action="store_true",
                      help="keep the downloaded audio after a successful run")
    p_ar.set_defaults(func=cmd_align_remote)

    p_pw = sub.add_parser("admin-password", help="Set, remove or check the admin password")
    p_pw.add_argument("action", choices=["set", "clear", "status"])
    p_pw.add_argument("--stdin", action="store_true", help="read the new password from stdin (no prompt)")
    p_pw.set_defaults(func=cmd_admin_password)

    p_tok = sub.add_parser("token", help="Script tokens for align-remote and other tools")
    p_tok.add_argument("action", choices=["create", "list", "revoke"])
    p_tok.add_argument("name", nargs="?", help="create: what it's for; revoke: its id")
    p_tok.set_defaults(func=cmd_token)

    p_conf = sub.add_parser("config", help="Show every setting, its value and where it came from")
    p_conf.add_argument("--json", action="store_true", help="as JSON")
    p_conf.set_defaults(func=cmd_config)

    p_serve = sub.add_parser("serve", help="Run the web server")
    p_serve.add_argument("--reload", action="store_true", help="Auto-reload (dev)")
    p_serve.set_defaults(func=cmd_serve)

    p_desk = sub.add_parser("desktop", help="Run as a tray app that keeps the server running (what the packaged app starts)")
    p_desk.add_argument("--library", help="Where the first run makes its Books and Audiobooks folders (default ~/Documents/Hondana)")
    p_desk.add_argument("--no-browser", action="store_true", help="Don't open the browser")
    p_desk.add_argument("--no-tray", action="store_true", help="No tray icon: run until Ctrl-C (headless)")
    p_desk.set_defaults(func=cmd_desktop)

    parser.add_argument("--self-test", action="store_true", help="Check that this installation works, then exit")
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
