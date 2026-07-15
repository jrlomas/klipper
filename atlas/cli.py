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
from .assistant import AssistantRuntime
from .ipc import request as assistant_request
from .kb import assemble_bundle, render_issue
from .memory import MachineMemoryStore
from .model import LlamaCppBackend
from .view import LiveTail, TimelineFilter, render

_CATALOG = os.path.join(os.path.dirname(__file__), "diagnosis", "patterns")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        if c is None:
            print("\nNo active incident — no error or critical event found")
        else:
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
    state_dir = os.path.dirname(os.path.abspath(
        os.path.expanduser(args.state_file)))
    memory_store = MachineMemoryStore(
        args.memory_file or os.path.join(state_dir, "memory.json"))
    assistant = None
    moonraker_db = args.moonraker_db
    if not moonraker_db and args.printer_config:
        data_dir = os.path.dirname(os.path.dirname(os.path.abspath(
            os.path.expanduser(args.printer_config))))
        moonraker_db = os.path.join(
            data_dir, "database", "moonraker-sql.db")
    if args.model:
        model_path = os.path.abspath(os.path.expanduser(args.model))
        backend = LlamaCppBackend(
            model_path=model_path, accelerator=args.accelerator,
            n_ctx=args.model_context, cli_path=args.llama_cli,
            timeout=args.model_timeout)
        backend.enforce_budget()
        if not backend.available():
            raise RuntimeError(
                "configured model or llama.cpp runtime is unavailable")
        assistant = AssistantRuntime(
            backend, memory=memory_store.memory,
            config_path=args.printer_config,
            job_history_path=moonraker_db)
    daemon = AtlasDaemon(
        log_path=args.logfile, state_path=args.state_file,
        catalog_path=args.catalog, interval=args.interval,
        max_events=args.max_events, heartbeat=args.heartbeat,
        telemetry_paths=args.telemetry,
        history_path=args.history_file or os.path.join(
            state_dir, "incidents.sqlite3"),
        incident_dir=args.incident_dir or os.path.join(
            state_dir, "incidents"), incident_settle=args.incident_settle,
        baseline_path=args.baseline_file or os.path.join(
            state_dir, "baselines.json"), assistant=assistant,
        assistant_socket=(args.assistant_socket or os.path.join(
            state_dir, "assistant.sock")) if assistant else None,
        memory_store=memory_store, printer_config=args.printer_config,
        gcode_dir=args.gcode_dir, repo_root=args.repo_root)
    if args.once:
        try:
            state = daemon.poll_once(force=True)
            print(json.dumps(state, indent=2, sort_keys=True))
        finally:
            daemon.close()
        return 0
    try:
        try:
            asyncio.run(daemon.serve())
        except KeyboardInterrupt:
            pass
    finally:
        daemon.close()
    return 0


def _cmd_assistant(args) -> int:
    params = {}
    if args.assistant_cmd == "ask":
        operation = "ask"
        params["question"] = args.text
    elif args.assistant_cmd == "interpret":
        operation = "interpret"
        params["structured"] = args.structured
    elif args.assistant_cmd == "propose":
        operation = "propose_config"
        params["request"] = args.text
    else:  # pragma: no cover - argparse constrains this
        raise ValueError(args.assistant_cmd)
    response = asyncio.run(assistant_request(
        args.socket, operation, params, timeout=args.timeout))
    result = response["result"]
    if operation == "ask":
        print(result["answer"])
    elif operation == "interpret" and not args.structured:
        print(result["interpretation"])
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
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
    s.add_argument("--telemetry", action="append", default=[],
                   help="newline-delimited structured telemetry (repeatable)")
    s.add_argument("--history-file",
                   help="SQLite incident history (default: beside state file)")
    s.add_argument("--incident-dir",
                   default=os.environ.get("ATLAS_INCIDENT_DIR") or None,
                   help="mode-private occurrence bundles")
    s.add_argument("--incident-settle", type=float, default=2.0,
                   help="quiet seconds used to group one failure occurrence")
    s.add_argument("--baseline-file",
                   help="machine baseline JSON (default: beside state file)")
    s.add_argument("--gcode-dir",
                   default=os.environ.get("ATLAS_GCODE_DIR") or None,
                   help="optional G-code root for hash and bounded context")
    s.add_argument("--repo-root", default=_REPO_ROOT,
                   help="repository root used only for revision identity")
    s.add_argument("--once", action="store_true",
                   help="publish one snapshot and exit")
    s.add_argument("--model", default=os.environ.get("ATLAS_MODEL", ""),
                   help="Qwen3-4B Q4_K_M GGUF; enables the assistant")
    s.add_argument("--llama-cli",
                   default=os.environ.get("ATLAS_LLAMA_CLI") or None,
                   help="llama-completion path")
    s.add_argument("--accelerator", choices=("cpu", "cuda", "rocm"),
                   default=os.environ.get("ATLAS_ACCELERATOR", "cpu"))
    s.add_argument("--model-context", type=int, default=8192)
    s.add_argument("--model-timeout", type=float, default=300.0)
    s.add_argument("--assistant-socket",
                   default=os.environ.get("ATLAS_ASSISTANT_SOCKET") or None)
    s.add_argument("--memory-file",
                   default=os.environ.get("ATLAS_MEMORY_FILE") or None)
    s.add_argument("--printer-config",
                   default=os.environ.get("ATLAS_PRINTER_CONFIG") or None,
                   help="read-only config source for classified previews")
    s.add_argument("--moonraker-db",
                   default=os.environ.get("ATLAS_MOONRAKER_DB") or None,
                   help="read-only Moonraker SQLite job history")
    s.set_defaults(func=_cmd_serve)

    a = sub.add_parser("assistant", help="query the running local assistant")
    a.add_argument(
        "--socket", default=os.path.expanduser(
            "~/.local/state/atlas/assistant.sock"))
    a.add_argument("--timeout", type=float, default=300.0)
    assistant_sub = a.add_subparsers(dest="assistant_cmd", required=True)
    ask = assistant_sub.add_parser("ask", help="ask about current machine state")
    ask.add_argument("text")
    interpret = assistant_sub.add_parser(
        "interpret", help="interpret the current incident")
    interpret.add_argument("--structured", action="store_true")
    propose = assistant_sub.add_parser(
        "propose", help="draft and classify a printer.cfg edit")
    propose.add_argument("text")
    a.set_defaults(func=_cmd_assistant)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
