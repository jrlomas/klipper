# A7 fleet coherence checker — compare a board's advertised contract to
# the host's and decide the lockstep action (FD-0002 §5).
#
# At handshake a board advertises (via HELIX_STATUS / the dictionary) its
# protocol hash, its BOARD_SYSCALL_ABI (major<<16 | minor), and whether it
# speaks FRAMING_V2.  This module turns that into a verdict and a single
# recommended action.  The keystone: when a board is behind, the action
# is an in-band **signed** flash to the host's release — auto-flash and
# protocol-correctness are the same mechanism.  The safety gate is
# deterministic (version math), never a judgement call.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from dataclasses import dataclass, field


@dataclass
class BoardState:
    """What a board advertised about the contract it was built against."""
    name: str
    protocol_hash: str            # PROTOCOL_ABI_HASH constant
    syscall_abi: int = 0          # BOARD_SYSCALL_ABI: major<<16 | minor
    framing_v2: bool = False      # FRAMING_V2 constant present
    fw_version: str = ""

    @property
    def abi_major(self) -> int:
        return self.syscall_abi >> 16

    @property
    def abi_minor(self) -> int:
        return self.syscall_abi & 0xFFFF


@dataclass
class CoherenceReport:
    board: str
    status: str                   # lockstep|board-behind|host-behind|
    #                               incompatible
    action: str                   # none|flash-board|update-host|manual
    reasons: list = field(default_factory=list)
    requires_signed_flash: bool = False   # any board flash must be signed

    @property
    def in_lockstep(self) -> bool:
        return self.status == "lockstep"


def check_board(host_hash, host_abi, board: BoardState,
                host_framing_v2=True) -> CoherenceReport:
    host_major, host_minor = host_abi >> 16, host_abi & 0xFFFF
    reasons = []

    # 1. Syscall ABI major is the hard compatibility boundary: a major
    #    bump is a breaking change, so mismatched majors are incompatible
    #    regardless of the protocol hash.
    if board.syscall_abi and board.abi_major != host_major:
        if board.abi_major < host_major:
            reasons.append("syscall ABI major %d < host %d (breaking): "
                           "reflash board to the host release"
                           % (board.abi_major, host_major))
            return CoherenceReport(board.name, "incompatible", "flash-board",
                                   reasons, requires_signed_flash=True)
        reasons.append("syscall ABI major %d > host %d: the host is older; "
                       "update the host software"
                       % (board.abi_major, host_major))
        return CoherenceReport(board.name, "host-behind", "update-host",
                               reasons)

    # 2. Protocol hash is the fine-grained wire-contract check.
    hash_match = board.protocol_hash == host_hash
    if hash_match and board.syscall_abi == host_abi:
        if host_framing_v2 and not board.framing_v2:
            # Not a lockstep failure — legacy framing is the permanent
            # fallback — but worth flagging as a link-capability gap.
            reasons.append("board lacks FRAMING_V2 (FEC); link falls back "
                           "to legacy CRC16 framing")
        else:
            reasons.append("protocol hash and syscall ABI match")
        return CoherenceReport(board.name, "lockstep", "none", reasons)

    # 3. Same major; decide direction by minor, else by the hash mismatch.
    if board.syscall_abi and board.abi_minor < host_minor:
        reasons.append("syscall ABI minor %d < host %d"
                       % (board.abi_minor, host_minor))
    if not hash_match:
        reasons.append("protocol hash %s != host %s"
                       % (board.protocol_hash or "<none>", host_hash))

    if board.syscall_abi and board.abi_minor > host_minor:
        reasons.append("syscall ABI minor %d > host %d: host is older"
                       % (board.abi_minor, host_minor))
        return CoherenceReport(board.name, "host-behind", "update-host",
                               reasons)

    # Board behind (older minor) or an ambiguous hash divergence: the
    # remedy is the same — bring the board into lockstep with a signed
    # flash of the host's matching release.
    return CoherenceReport(board.name, "board-behind", "flash-board",
                           reasons, requires_signed_flash=True)


def check_fleet(host_hash, host_abi, boards, host_framing_v2=True) -> list:
    return [check_board(host_hash, host_abi, b, host_framing_v2)
            for b in boards]
