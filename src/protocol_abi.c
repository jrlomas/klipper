// Protocol contract advertisement for Atlas fleet coherence (FD-0002 doc 06).
//
// The build generates protocol_abi_hash.h from intentproto/core_ids.hpp.  Its
// DECL_CONSTANT_STR is served in the ordinary Klipper data dictionary, so the
// host can determine lockstep during the existing identify handshake without
// adding a command or a round trip.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "protocol_abi_hash.h"
