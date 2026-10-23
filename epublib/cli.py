"""Hondana's command line: the pipeline stages, the server and the maintenance tools.

Usage: hondana <command> [--config PATH]   (or `uv run cli.py …` from a checkout;
`--help` lists them all)

    <stage>   scan | analyze | enrich | pair | audio-scan | xref | build — the pipeline
              stages, defined once in epublib/pipeline.py (also what /admin runs)
    all       every stage in order
    push      back up the hand-curated data/ files to a git repo (off unless configured; see epublib/data_mirror.py)
    seed-known [--top-n N]   seed active profile's known words with wordfreq top-N (en/ja)
    serve     run the web server
    consolidate [--execute]   unify audiobook dirs (dry-run default)
    recover-moves   re-attach curated state orphaned by moving a file between folders
    gc [--apply]    report (and with --apply remove) data left behind by books no longer in the library
    align-remote <book>|--missing   align a server's audiobook on this machine's GPU (SubPlz) and upload the SRT

Each stage is resume-safe; re-running only does the new/changed work.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from epublib import align_remote as align_remote_mod
from epublib import audiobook_scanner as audiobook_scanner_mod
from epublib import build as build_mod
from epublib import config as config_mod
from epublib import consolidate as consolidate_mod
from epublib import data_mirror as data_mirror_mod
from epublib import known as known_mod
from epublib import orphans as orphans_mod
from epublib import pipeline as pipeline_mod
from epublib import profiles as profiles_mod
from epublib import rekey as rekey_mod
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


def cmd_align_remote(args):
    """Fetch a server book's text and audio, run this machine's `subplz_cmd`, upload the SRTs."""
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
    cmd = mine.get("subplz_cmd") or config_mod.load(args.config).subplz_cmd
    audio = [Path(a).expanduser() for a in args.audio]
    audio_dir = Path(args.audio_dir).expanduser() if args.audio_dir else None

    def plan_for(book, entries):
        return ar.make_plan(server, book, entries, audio, audio_dir)

    try:
        server = ar.Server(ar.find_server(args.server or mine.get("server")))
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
                r = ar.align_book(server, p, cmd, force=args.force, keep_audio=args.keep_audio)
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
            print(f"{'aligned' if m['ok'] else 'POOR MATCH'}: coverage {m['coverage']:.1%} "
                  f"({m['matched']}/{m['total']} sentences) in {ar.human_time(r.seconds)}")
            if not m["ok"]:
                failed += 1
                print("The server stored it but below the 90% the reader needs to highlight; "
                      "transcripts kept in " + str(ar.work_dir(server, r.book_id)), file=sys.stderr)
    except KeyboardInterrupt:
        print("\nInterrupted. Finished files are kept; run the same command to resume.", file=sys.stderr)
        return 130
    print(f"\nElapsed {ar.human_time(time.time() - began)}.")
    return 1 if failed else 0


def cmd_serve(args):
    import os
    import uvicorn

    # Make --config visible to the server process (it loads config at import).
    if args.config:
        os.environ[config_mod.CONFIG_ENV] = str(args.config)
    cfg = _cfg(args)
    uvicorn.run("epublib.server:app", host=cfg.host, port=cfg.port, reload=args.reload)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hondana: your Japanese EPUB and audiobook library")
    parser.add_argument("--config", help="Path to config.toml", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    # `scan`, `analyze`, … and `all` come from the stage registry.
    for stage in (*pipeline_mod.STAGES, pipeline_mod.ALL):
        p = sub.add_parser(stage.id, help=stage.help)
        if stage.refresh_help:
            p.add_argument("--refresh", action="store_true", help=stage.refresh_help)
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

    p_ar = sub.add_parser(
        "align-remote",
        help="Align a server's audiobook here: fetch its text and audio, run subplz_cmd, upload the SRT",
    )
    p_ar.add_argument("book", nargs="?", help="a book_id on the server, or a title substring")
    p_ar.add_argument("--server", default=None,
                      help="the library server (default: `server` in "
                      f"{align_remote_mod.USER_CONFIG}, else the one found on this network)")
    p_ar.add_argument("--missing", action="store_true",
                      help="list JP books with a local audiobook and no alignment")
    p_ar.add_argument("--run", action="store_true", help="with --missing: align them one after another")
    p_ar.add_argument("--dry-run", action="store_true", help="print the plan; start nothing")
    p_ar.add_argument("--force", action="store_true", help="redo a book that is already aligned")
    p_ar.add_argument("--audio", action="append", default=[], metavar="PATH",
                      help="use this copy of an audio file instead of downloading it "
                      "(matched by file name; repeatable)")
    p_ar.add_argument("--audio-dir", metavar="DIR",
                      help="use audio files found here (by file name) instead of downloading them")
    p_ar.add_argument("--keep-audio", action="store_true",
                      help="keep the downloaded audio after a successful run")
    p_ar.set_defaults(func=cmd_align_remote)

    p_serve = sub.add_parser("serve", help="Run the web server")
    p_serve.add_argument("--reload", action="store_true", help="Auto-reload (dev)")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
