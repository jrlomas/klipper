# Atlas CLI — the deterministic floor, drivable from a terminal today.
#
#   python3 -m atlas.cli decode   klippy.log      # render the timeline
#   python3 -m atlas.cli diagnose klippy.log      # decode + diagnose
#
# Works on a stock Klipper log with no HELIX board present (FD-0002 §4).
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import asyncio
import os
import sys

import json

from .decode import decode_klippy_log
from .diagnosis import Matcher, load_catalog
from .daemon import AtlasDaemon, DEFAULT_HEARTBEAT, DEFAULT_MAX_EVENTS
from .kb import assemble_bundle, render_issue
from .view import LiveTail, TimelineFilter, render

_CATALOG = os.path.join(os.path.dirname(__file__), "diagnosis", "patterns")


def _fmt_time(tl, e):
    if e.mtime is None:
        return "        ?   "
    mark = " " if e.t_exact else "~"
    return "%s%10.3f" % (mark, e.mtime)


def _cmd_decode(args) -> int:
    with open(args.logfile) as fh:
        tl = decode_klippy_log(fh.read())
    events = tl.ordered()
    if args.errors_only:
        events = [e for e in events if e.sev_rank() >= 4]
    for e in events:
        print("%s  %-8s %-14s %s"
              % (_fmt_time(tl, e), e.severity, e.kind, e.summary))
    print("\n%d events, %d error(s)/critical(s); span %s"
          % (len(tl), len(tl.errors()), tl.span()))
    for n in tl.notes:
        print("note: %s" % n)
    return 0


def _cmd_diagnose(args) -> int:
    with open(args.logfile) as fh:
        tl = decode_klippy_log(fh.read())
    patterns = load_catalog(args.catalog)
    diag = Matcher(patterns).diagnose(tl)
    print("Atlas diagnosis — %d pattern(s) loaded from %s"
          % (len(patterns), args.catalog))
    if diag.matched():
        for m in diag.matches:
            print("\nMATCH  %s  (confidence %.2f, %s)"
                  % (m.pattern_id, m.confidence, m.provenance))
            print("  cause: %s" % m.cause)
            print("  fix:   %s" % m.fix)
    else:
        c = diag.case
        print("\nNo known pattern matched — CASE CAPTURED")
        print("  case: %s" % c.case_hash)
        print("  headline: %s" % c.summary)
        for line in c.signature_lines():
            print("    · %s" % line)
        print("  (a candidate for the knowledge base; see FD-0002 §6a)")
    for n in diag.notes:
        print("note: %s" % n)
    return 0


def _build_filter(args) -> TimelineFilter:
    return TimelineFilter(
        min_severity=args.min_severity,
        sources=args.source or [],
        kinds=args.kind or [],
        subsystems=args.sub or [],
        ordered=not args.follow)


def _cmd_view(args) -> int:
    filt = _build_filter(args)
    if args.follow:
        LiveTail(args.logfile, filt).follow(wall=args.wall)
        return 0
    with open(args.logfile) as fh:
        tl = decode_klippy_log(fh.read())
    for line in render(tl, filt, wall=args.wall):
        print(line)
    return 0


def _cmd_bundle(args) -> int:
    with open(args.logfile) as fh:
        tl = decode_klippy_log(fh.read())
    patterns = load_catalog(args.catalog)
    diag = Matcher(patterns).diagnose(tl)
    bundle = assemble_bundle(tl, diag)
    if args.issue:
        issue = render_issue(bundle)
        print("# labels: %s\n# title: %s\n" % (", ".join(issue["labels"]),
                                               issue["title"]))
        print(issue["body"])
    else:
        print(json.dumps(bundle.to_dict(), indent=2))
    return 0


def _cmd_serve(args) -> int:
    daemon = AtlasDaemon(
        log_path=args.logfile, state_path=args.state_file,
        catalog_path=args.catalog, interval=args.interval,
        max_events=args.max_events, heartbeat=args.heartbeat)
    if args.once:
        state = daemon.poll_once(force=True)
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    try:
        asyncio.run(daemon.serve())
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="atlas", description="Atlas floor CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decode", help="decode a klippy.log into a timeline")
    d.add_argument("logfile")
    d.add_argument("--errors-only", action="store_true")
    d.set_defaults(func=_cmd_decode)

    g = sub.add_parser("diagnose", help="decode then run the diagnosis engine")
    g.add_argument("logfile")
    g.add_argument("--catalog", default=_CATALOG)
    g.set_defaults(func=_cmd_diagnose)

    v = sub.add_parser("view", help="filter/tail the merged timeline")
    v.add_argument("logfile")
    v.add_argument("--follow", "-f", action="store_true",
                   help="live-tail a growing log")
    v.add_argument("--min-severity", default="debug",
                   help="debug|info|notice|warning|error|critical")
    v.add_argument("--source", action="append",
                   help="filter by source substring (repeatable)")
    v.add_argument("--kind", action="append",
                   help="filter by event kind (repeatable)")
    v.add_argument("--sub", action="append",
                   help="filter by trace subsystem (repeatable)")
    v.add_argument("--wall", action="store_true",
                   help="render wall-clock times when anchored")
    v.set_defaults(func=_cmd_view)

    b = sub.add_parser("bundle", help="assemble a redacted blackbox bundle")
    b.add_argument("logfile")
    b.add_argument("--catalog", default=_CATALOG)
    b.add_argument("--issue", action="store_true",
                   help="render as a GitHub Issue instead of JSON")
    b.set_defaults(func=_cmd_bundle)

    s = sub.add_parser(
        "serve", help="run the always-on timeline and diagnosis service")
    s.add_argument("logfile", help="klippy.log to follow")
    s.add_argument(
        "--state-file",
        default=os.path.expanduser("~/.local/state/atlas/status.json"),
        help="atomic JSON snapshot consumed by API plumbing")
    s.add_argument("--catalog", default=_CATALOG)
    s.add_argument("--interval", type=float, default=0.5)
    s.add_argument("--heartbeat", type=float, default=DEFAULT_HEARTBEAT,
                   help="seconds between idle health snapshots")
    s.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    s.add_argument("--once", action="store_true",
                   help="publish one snapshot and exit")
    s.set_defaults(func=_cmd_serve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
