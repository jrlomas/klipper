#!/usr/bin/env python3
# Standalone unit test for the Atlas A8 KB framework (FD-0002 §6, §6a).
# The redaction pass is load-bearing — it enforces the privacy promise
# that nothing leaves the Pi unredacted except numeric diagnostics — so
# it gets the most coverage. Also checks bundle assembly (redaction
# applied, content hash stable) and the GitHub-Issue rendering + labels.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.diagnosis import Matcher, load_patterns  # noqa: E402
from atlas.kb import (ALL_LABELS, REJECT_REASONS, STATE_LABELS,  # noqa: E402
                      assemble_bundle, redact_event, redact_fields,
                      render_issue)

INCIDENT = """\
Start printer at Sat Jul 12 10:00:00 2026 (1752314400.0 6.7)
Stats 7.0: gcodein=0 mcu: bytes_retransmit=1500 bytes_invalid=40
MCU 'mcu' shutdown: Timer too close
Transition to shutdown state: MCU 'mcu' shutdown: Timer too close
"""


# -- redaction: the three tiers ------------------------------------------

def test_numeric_kept_raw():
    out = redact_fields({"queue_depth": 12, "horizon_us": 1200.5,
                         "bytes_retransmit": 1500})
    assert out == {"queue_depth": 12, "horizon_us": 1200.5,
                   "bytes_retransmit": 1500}
    print("PASS: numeric diagnostics shared raw (tier a)")


def test_secrets_never_shared():
    fields = {"wifi_ssid": "MyHomeNet", "api_key": "abcd1234",
              "password": "hunter2", "psk": "x", "mac_addr": "aa:bb",
              "board_serial": "SN12345", "uuid": "11-22", "ip": "10.0.0.5",
              "queue_depth": 7}
    out = redact_fields(fields)
    assert out == {"queue_depth": 7}, out   # only the safe numeric survives
    print("PASS: secrets/network-ids/serials never shared (tier c)")


def test_secret_cannot_be_allowlisted():
    # Even a *numeric* secret-named field is dropped — a secret can't be
    # shared by naming, whatever its type.
    out = redact_fields({"secret_token": 42, "session_key": 7})
    assert out == {}
    print("PASS: a secret cannot be shared even as a numeric value")


def test_paths_basenamed_freetext_dropped():
    out = redact_fields({"gcode": "/home/pi/prints/secret_model_v2.gcode",
                         "message": "some free text note"})
    # 'gcode'/'path'-ish keys with '/' -> but 'gcode' has no sensitive
    # token, so it's a path value -> basename; 'message' free-text dropped.
    assert out.get("gcode") == "secret_model_v2.gcode"
    assert "message" not in out
    print("PASS: paths -> basename, free-text strings dropped (tier b)")


def test_path_key_is_sensitive():
    # A key literally named with a path/file token is dropped entirely.
    out = redact_fields({"file_path": "/etc/passwd", "log_dir": "/var/log"})
    assert out == {}
    print("PASS: path/file-named keys dropped outright")


def test_safe_structural_strings_kept():
    out = redact_fields({"mcu": "stm32f446", "kinematics": "corexy",
                         "fault_class": "timer_too_close",
                         "protocol_hash": "27141a58f61f9fbc"})
    assert out == {"mcu": "stm32f446", "kinematics": "corexy",
                   "fault_class": "timer_too_close",
                   "protocol_hash": "27141a58f61f9fbc"}
    print("PASS: safe structural strings shared verbatim (tier a)")


def test_wallclock_dropped():
    out = redact_fields({"systime": 1752314400.0, "mtime": 8.0, "wall": "x"})
    assert "systime" not in out and "wall" not in out
    assert out["mtime"] == 8.0            # relative machine-time kept
    print("PASS: absolute wall-clock dropped, relative time kept (tier b)")


def test_nested_and_list_redaction():
    out = redact_fields({
        "sections": {"mcu": {"bytes_retransmit": 12, "wifi_ssid": "x"}},
        "values": [1, 2, 3]})
    assert out["sections"]["mcu"] == {"bytes_retransmit": 12}
    assert out["values"] == [1, 2, 3]
    print("PASS: redaction recurses into nested dicts and lists")


def test_event_redaction_drops_raw():
    tl = decode_klippy_log(INCIDENT)
    shut = tl.of_kind("mcu_shutdown")[0]
    red = redact_event(shut)
    assert "raw" not in red                     # source text never shared
    assert red["fields"]["fault_class"] == "timer_too_close"
    assert red["kind"] == "mcu_shutdown"
    print("PASS: event redaction keeps the spine, drops raw source text")


# -- bundle --------------------------------------------------------------

def test_bundle_assembled_and_redacted():
    tl = decode_klippy_log(INCIDENT)
    diag = Matcher([]).diagnose(tl)             # empty catalog -> case
    bundle = assemble_bundle(tl, diag)
    assert bundle.redacted and bundle.content_hash
    assert bundle.diagnosis["matched"] is False
    assert bundle.timeline and all("raw" not in e for e in bundle.timeline)
    # No raw source strings leaked into any field.
    for e in bundle.timeline:
        assert "/" not in str(e["fields"])
    print("PASS: bundle assembled from timeline+diagnosis, fully redacted")


def test_bundle_hash_stable_across_volatiles():
    a = assemble_bundle(decode_klippy_log(INCIDENT), Matcher([]).diagnose(
        decode_klippy_log(INCIDENT)))
    variant = INCIDENT.replace("1500", "9999").replace("7.0", "77.0")
    b = assemble_bundle(decode_klippy_log(variant), Matcher([]).diagnose(
        decode_klippy_log(variant)))
    assert a.content_hash == b.content_hash
    print("PASS: bundle content hash ignores volatile values (dedup)")


def test_bundle_with_match():
    pat = load_patterns([{
        "id": "timer", "signature": {"fault_class": ["timer_too_close"]},
        "cause": "host overload", "fix": "reduce load", "confidence": 0.6}])
    tl = decode_klippy_log(INCIDENT)
    bundle = assemble_bundle(tl, Matcher(pat).diagnose(tl))
    assert bundle.diagnosis["matched"] is True
    assert bundle.diagnosis["patterns"][0]["id"] == "timer"
    print("PASS: a matched bundle records the pattern id + confidence")


# -- issue + labels ------------------------------------------------------

def test_render_issue():
    tl = decode_klippy_log(INCIDENT)
    bundle = assemble_bundle(tl, Matcher([]).diagnose(tl))
    issue = render_issue(bundle)
    assert issue["labels"] == ["case/new"]      # fresh case, nothing more
    assert "Symptom" in issue["body"]
    assert bundle.content_hash in issue["body"]
    assert "no known pattern matched" in issue["body"]
    assert issue["title"].startswith("Atlas case:")
    print("PASS: issue renders the §6a template, labelled case/new")


def test_label_vocabulary():
    assert STATE_LABELS[0] == "case/new" and STATE_LABELS[-1] == "accepted"
    assert all(r.startswith("rejected/") for r in REJECT_REASONS)
    assert len(ALL_LABELS) == len(set(ALL_LABELS))   # no dupes
    print("PASS: label vocabulary is the fixed §6a set, no duplicates")


def main():
    test_numeric_kept_raw()
    test_secrets_never_shared()
    test_secret_cannot_be_allowlisted()
    test_paths_basenamed_freetext_dropped()
    test_path_key_is_sensitive()
    test_safe_structural_strings_kept()
    test_wallclock_dropped()
    test_nested_and_list_redaction()
    test_event_redaction_drops_raw()
    test_bundle_assembled_and_redacted()
    test_bundle_hash_stable_across_volatiles()
    test_bundle_with_match()
    test_render_issue()
    test_label_vocabulary()
    print("ALL PASS")


if __name__ == "__main__":
    main()
