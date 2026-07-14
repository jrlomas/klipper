#!/usr/bin/env python3
# Standalone unit test for the Atlas A7 fleet-coherence floor (FD-0002 §5).
# Checks the protocol-hash derivation from intentproto's core ids (stable,
# order-independent, sensitive to any contract change) and the lockstep
# decision matrix: lockstep / board-behind / host-behind / incompatible,
# with signed-flash required for any board remediation.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.fleet import (BoardState, abi_header, check_board,  # noqa: E402
                         check_fleet, host_protocol_hash, parse_core_ids,
                         protocol_hash)
from atlas.fleet.abi import DEFAULT_CORE_IDS  # noqa: E402

HEADER = """
namespace intentproto { namespace v2 {
constexpr uint32_t MSGID_GET_CLOCK = 2;
constexpr uint32_t MSGID_CLOCK = 3;
constexpr uint32_t MSGID_QUEUE_TRAJ_SEGMENT = 12;
} }
"""

ABI_1_0 = 0x00010000  # major 1, minor 0
ABI_1_1 = 0x00010001
ABI_2_0 = 0x00020000


def test_parse_core_ids():
    defs = parse_core_ids(HEADER)
    assert defs == {"MSGID_GET_CLOCK": 2, "MSGID_CLOCK": 3,
                    "MSGID_QUEUE_TRAJ_SEGMENT": 12}
    print("PASS: core ids parsed from the header")


def test_hash_stable_and_order_independent():
    a = protocol_hash({"MSGID_A": 2, "MSGID_B": 3})
    b = protocol_hash({"MSGID_B": 3, "MSGID_A": 2})   # different order
    assert a == b and len(a) == 16
    print("PASS: protocol hash is stable and declaration-order independent")


def test_hash_sensitive_to_contract_change():
    base = protocol_hash({"MSGID_A": 2, "MSGID_B": 3})
    renumbered = protocol_hash({"MSGID_A": 2, "MSGID_B": 4})  # id changed
    renamed = protocol_hash({"MSGID_A": 2, "MSGID_C": 3})     # name changed
    assert base != renumbered and base != renamed
    print("PASS: hash changes when an id is renumbered or renamed")


def test_real_core_ids_hash():
    # The real intentproto header must parse and hash deterministically.
    assert os.path.exists(DEFAULT_CORE_IDS), DEFAULT_CORE_IDS
    h = host_protocol_hash()
    assert len(h) == 16 and int(h, 16) >= 0
    # Recomputing gives the same value (determinism).
    assert h == host_protocol_hash()
    print("PASS: real intentproto core_ids.hpp hashes to %s" % h)


def test_lockstep():
    h = "abc123abc123abc1"
    r = check_board(h, ABI_1_0,
                    BoardState("mcu", h, ABI_1_0, framing_v2=True))
    assert r.in_lockstep and r.action == "none"
    print("PASS: matching hash + ABI + framing = lockstep")


def test_framing_gap_still_lockstep():
    h = "abc123abc123abc1"
    r = check_board(h, ABI_1_0, BoardState("mcu", h, ABI_1_0,
                                           framing_v2=False))
    assert r.in_lockstep                      # legacy framing is a fallback
    assert any("FRAMING_V2" in x for x in r.reasons)
    print("PASS: missing FRAMING_V2 is flagged but not a lockstep failure")


def test_board_behind_minor():
    h = "hosthosthosthost"
    board = BoardState("old", "boardboardboardb", ABI_1_0)  # host is 1.1
    r = check_board(h, ABI_1_1, board)
    assert r.status == "board-behind" and r.action == "flash-board"
    assert r.requires_signed_flash is True    # any board flash must be signed
    print("PASS: older minor -> board-behind -> signed flash-board")


def test_host_behind_minor():
    h = "hosthosthosthost"
    board = BoardState("new", h, ABI_1_1)     # board newer than host 1.0
    r = check_board(h, ABI_1_0, board)
    assert r.status == "host-behind" and r.action == "update-host"
    assert r.requires_signed_flash is False
    print("PASS: newer minor -> host-behind -> update the host")


def test_incompatible_major():
    h = "hosthosthosthost"
    board = BoardState("ancient", "x", ABI_1_0)   # host is 2.0
    r = check_board(h, ABI_2_0, board)
    assert r.status == "incompatible" and r.action == "flash-board"
    assert r.requires_signed_flash is True
    print("PASS: major mismatch -> incompatible -> signed reflash")


def test_hash_divergence_without_abi():
    # A board that reports no syscall ABI but a divergent hash is treated
    # as behind (the safe default: bring it to the host contract).
    r = check_board("hosthash00000000", 0,
                    BoardState("mystery", "otherhash1111111", syscall_abi=0))
    assert r.status == "board-behind" and r.requires_signed_flash
    print("PASS: hash divergence with no ABI defaults to signed reflash")


def test_check_fleet_mixed():
    h = "hosthosthosthost"
    boards = [
        BoardState("good", h, ABI_1_0, framing_v2=True),
        BoardState("old", "x", ABI_1_0 - 0 + 0),  # placeholder good ABI
        BoardState("ancient", "y", ABI_2_0),
    ]
    # host at 1.1 so 'good' (1.0) is behind; 'ancient' (2.0) incompatible
    reports = check_fleet(h, ABI_1_1, boards)
    statuses = [r.status for r in reports]
    assert "board-behind" in statuses and "host-behind" in statuses
    print("PASS: check_fleet classifies a mixed fleet")


def test_abi_header_emits_constant():
    text = abi_header("deadbeefdeadbeef")
    assert 'DECL_CONSTANT_STR("PROTOCOL_ABI_HASH", "deadbeefdeadbeef")' in text
    assert "#ifndef" in text
    print("PASS: abi_header bakes the hash as a firmware DECL_CONSTANT_STR")


def test_build_generator_matches_host_contract():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory() as td:
        output = os.path.join(td, "protocol_abi_hash.h")
        subprocess.check_call([
            sys.executable, os.path.join(root, "scripts",
                                         "gen_protocol_abi.py"),
            "--output", output])
        with open(output, encoding="utf-8") as fh:
            generated = fh.read()
    assert ('DECL_CONSTANT_STR("PROTOCOL_ABI_HASH", "%s")'
            % host_protocol_hash()) in generated
    print("PASS: firmware build generator embeds the live host contract hash")


def main():
    test_parse_core_ids()
    test_hash_stable_and_order_independent()
    test_hash_sensitive_to_contract_change()
    test_real_core_ids_hash()
    test_lockstep()
    test_framing_gap_still_lockstep()
    test_board_behind_minor()
    test_host_behind_minor()
    test_incompatible_major()
    test_hash_divergence_without_abi()
    test_check_fleet_mixed()
    test_abi_header_emits_constant()
    test_build_generator_matches_host_contract()
    print("ALL PASS")


if __name__ == "__main__":
    main()
