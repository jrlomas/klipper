# A7 fleet coherence — the lockstep answer (FD-0002 §5).
#
# Protocol correctness depends on the host, the intentproto library, and
# every board's firmware agreeing on the wire contract.  So the library
# is the single version authority: a protocol/ABI hash derived from
# intentproto's spec-frozen core ids is baked into every image and the
# host, checked at handshake (building on HELIX_STATUS / BOARD_SYSCALL_ABI
# / FRAMING_V2), and when a board is behind, the host offers the in-band
# *signed* flash that brings it into lockstep.  Auto-flash and
# protocol-correctness become one mechanism, not two features.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from .abi import (parse_core_ids, protocol_hash, host_protocol_hash,
                  abi_header, DEFAULT_CORE_IDS)
from .coherence import (BoardState, CoherenceReport, check_board,
                        check_fleet)
from .remediate import remediate_board

__all__ = [
    "parse_core_ids", "protocol_hash", "host_protocol_hash", "abi_header",
    "DEFAULT_CORE_IDS", "BoardState", "CoherenceReport", "check_board",
    "check_fleet",
    "remediate_board",
]
