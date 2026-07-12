# intentproto host binding — cffi, API mode (RFC 0001 doc 10).
#
# This is the "thin Python binding generated from the headers" the
# host profile promises: it builds the freestanding C++ core plus the
# extern "C" shim (src/*.cpp + capi.cpp) into a Python extension via
# cffi in API mode, and re-exports the surface of
# include/intentproto/capi.h as Pythonic objects.
#
# It resolves doc 10's open question ("cffi against the installed
# headers or a generated ctypes shim; proposed: cffi, API mode") in
# favour of API mode: cffi compiles a real C++ extension that #includes
# capi.h, so the binding is checked against the actual declarations at
# build time — a compile error, not a silently wrong ctypes signature,
# is what a drifting ABI produces here. It mirrors klippy/chelper's
# build-on-demand pattern (an mtime check, a compile step, a cached
# module) but replaces chelper's stringly-typed ABI-mode cdef with the
# versioned header as the single source of truth.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# (The library it binds is MIT; this GPL binding links it, which is the
# fine direction — see doc 10 "Licensing".)

import glob
import importlib
import os
import sys

# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------
_HERE = os.path.dirname(os.path.realpath(__file__))
_PKG_ROOT = os.path.dirname(os.path.dirname(_HERE))          # lib/intentproto
_INCLUDE = os.path.join(_PKG_ROOT, "include")
_SRC = os.path.join(_PKG_ROOT, "src")
_BOOT = os.path.join(_PKG_ROOT, "boot")
_BUILD = os.path.join(_PKG_ROOT, "build", "python")

MODULE_NAME = "intentproto_capi"

# ---------------------------------------------------------------------
# The C declarations the binding needs (a subset of capi.h, verified
# against the real header at compile time by cffi API mode). Callbacks
# cross the boundary through cffi's extern "Python" mechanism.
# ---------------------------------------------------------------------
_CDEF = """
#define INTENTPROTO_ABI_VERSION_MAJOR ...
#define INTENTPROTO_ABI_VERSION_MINOR ...
#define INTENTPROTO_ABI_VERSION_PATCH ...
#define INTENTPROTO_ABI_VERSION ...
#define IP_MESSAGE_MAX ...
#define IP_PAYLOAD_MAX ...
#define IP_CLASS_SCHEDULED ...
#define IP_CLASS_PROMPT ...
#define IP_CLASS_TELEMETRY ...
#define IP_FRAMING_LEGACY ...
#define IP_FRAMING_PROBING ...

uint32_t intentproto_abi_version(void);
const char *intentproto_version_string(void);

typedef struct ip_class_stats {
    uint32_t tx_msgs, tx_bytes;
    uint32_t rx_msgs, rx_bytes;
    uint32_t dropped;
} ip_class_stats;

uint16_t ip_crc16_ccitt(const uint8_t *buf, size_t len);
size_t ip_vlq_encode(uint8_t *out, uint32_t v);
size_t ip_vlq_decode(const uint8_t *in, size_t len, uint32_t *out);
size_t ip_frame_v2_encode(uint8_t *out, const uint8_t *payload,
                          size_t payload_len, uint8_t seq);
int ip_frame_v2_decode(uint8_t *frame, size_t frame_len,
                       size_t *payload_off, uint8_t *seq, int *corrected);

typedef int (*ip_write_fn)(const uint8_t *data, size_t len, void *user);
typedef void (*ip_response_fn)(const uint8_t *payload, size_t len,
                               void *user);
typedef struct ip_host_session ip_host_session;

ip_host_session *ip_host_session_create(ip_write_fn write_fn, void *wuser,
                                        ip_response_fn response_fn,
                                        void *ruser, int desired_framing);
void ip_host_session_free(ip_host_session *h);
int ip_host_session_send_command(ip_host_session *h, const uint8_t *payload,
                                 size_t len, int cls);
void ip_host_session_on_rx(ip_host_session *h, const uint8_t *data,
                           size_t len);
int ip_host_session_need_retransmit(ip_host_session *h, uint64_t now_ticks,
                                    uint64_t rto_ticks);
int ip_host_session_enable_v2(ip_host_session *h);
size_t ip_host_session_inflight(const ip_host_session *h);
void ip_host_session_class_stats(const ip_host_session *h, int cls,
                                 ip_class_stats *out);
typedef struct ip_host_diag {
    uint32_t retransmits;
    uint32_t naks;
    uint32_t rx_crc_errors;
    uint32_t rx_bch_errors;
    uint32_t rx_framing_errors;
    uint32_t v2_frames_rx;
    int v2_rejected;
    int framing_v2;
} ip_host_diag;
void ip_host_session_diag(const ip_host_session *h, ip_host_diag *out);

typedef struct ip_datagram_tx ip_datagram_tx;
typedef struct ip_datagram_rx ip_datagram_rx;
ip_datagram_tx *ip_datagram_tx_create(const uint8_t *psk, size_t psk_len,
                                      uint8_t fec_k);
void ip_datagram_tx_free(ip_datagram_tx *tx);
ip_datagram_rx *ip_datagram_rx_create(const uint8_t *psk, size_t psk_len);
void ip_datagram_rx_free(ip_datagram_rx *rx);
size_t ip_datagram_encode(ip_datagram_tx *tx, uint8_t *out,
                          const uint8_t *frames, size_t len, int cls);
size_t ip_datagram_parity_flush(ip_datagram_tx *tx, uint8_t *out);
int ip_datagram_decode(ip_datagram_rx *rx, uint8_t *data, size_t len,
                       size_t *frames_off, int *cls);
size_t ip_datagram_take_recovered(ip_datagram_rx *rx, uint8_t *out,
                                  size_t cap);

void ip_device_init(ip_write_fn write_fn, void *user, const char *version,
                    const char *build_version);
void ip_device_rx(const uint8_t *data, size_t len);
int ip_command_count(void);
int ip_response_count(void);
int ip_constant_count(void);
uint32_t ip_command_id(int idx);
uint32_t ip_response_id(int idx);
const char *ip_command_name(int idx);
const char *ip_response_name(int idx);
size_t ip_command_key(int idx, char *out, size_t cap);
size_t ip_response_key(int idx, char *out, size_t cap);
int ip_command_index_by_name(const char *name);

extern "Python" int ip_py_host_write(const uint8_t *, size_t, void *);
extern "Python" void ip_py_host_response(const uint8_t *, size_t, void *);
extern "Python" int ip_py_device_write(const uint8_t *, size_t, void *);
"""


def _source_files():
    srcs = sorted(glob.glob(os.path.join(_SRC, "*.cpp")))
    boot = os.path.join(_BOOT, "bootcore.cpp")
    if os.path.exists(boot):
        srcs.append(boot)
    return srcs


def _needs_build(module_path):
    if not os.path.exists(module_path):
        return True
    target = os.path.getmtime(module_path)
    watched = _source_files() + [
        os.path.join(_INCLUDE, "intentproto", "capi.h"), __file__]
    for f in watched:
        try:
            if os.path.getmtime(f) > target:
                return True
        except OSError:
            pass
    return False


def _find_built_module():
    if not os.path.isdir(_BUILD):
        return None
    for fn in os.listdir(_BUILD):
        if fn.startswith(MODULE_NAME) and (fn.endswith(".so")
                                           or fn.endswith(".pyd")):
            return os.path.join(_BUILD, fn)
    return None


def build(force=False):
    # Build the cffi API-mode extension (idempotent: skipped when the
    # cached module is newer than every source it was built from).
    import cffi
    if not os.path.isdir(_BUILD):
        os.makedirs(_BUILD)
    existing = _find_built_module()
    if existing is not None and not force and not _needs_build(existing):
        return existing
    ffibuilder = cffi.FFI()
    ffibuilder.cdef(_CDEF)
    ffibuilder.set_source(
        MODULE_NAME,
        '#include "intentproto/capi.h"',
        sources=_source_files(),
        include_dirs=[_INCLUDE],
        source_extension=".cpp",
        extra_compile_args=["-std=gnu++17", "-O2"],
    )
    ffibuilder.compile(tmpdir=_BUILD, verbose=False)
    return _find_built_module()


_ffi = None
_lib = None


def get_ffi():
    # Return (ffi, lib), building/loading the extension on first use.
    global _ffi, _lib
    if _lib is not None:
        return _ffi, _lib
    build()
    if _BUILD not in sys.path:
        sys.path.insert(0, _BUILD)
    module = importlib.import_module(MODULE_NAME)
    _ffi, _lib = module.ffi, module.lib
    # Refuse a major-version ABI mismatch (the whole point of the
    # versioned header).
    got = _lib.intentproto_abi_version()
    want = _lib.INTENTPROTO_ABI_VERSION
    if (got >> 16) != (want >> 16):
        raise RuntimeError(
            "intentproto ABI major mismatch: header %#x, library %#x"
            % (want, got))
    return _ffi, _lib


# ---------------------------------------------------------------------
# Callback dispatch. The C shim's void* user carries an integer token;
# the extern "Python" trampolines route each callback to the Python
# object that registered under that token. One host + one device is the
# common case, but the registry supports any number of live sessions.
# ---------------------------------------------------------------------
_write_sinks = {}       # token -> callable(bytes)
_response_sinks = {}     # token -> callable(bytes)
_device_write_sink = [None]


def _install_callbacks():
    from intentproto_capi import ffi as _f

    @_f.def_extern()
    def ip_py_host_write(data, length, user):
        cb = _write_sinks.get(int(_f.cast("intptr_t", user)))
        if cb is not None:
            cb(bytes(_f.buffer(data, length)))
        return length

    @_f.def_extern()
    def ip_py_host_response(payload, length, user):
        cb = _response_sinks.get(int(_f.cast("intptr_t", user)))
        if cb is not None:
            cb(bytes(_f.buffer(payload, length)))

    @_f.def_extern()
    def ip_py_device_write(data, length, user):
        cb = _device_write_sink[0]
        if cb is not None:
            cb(bytes(_f.buffer(data, length)))
        return length


_callbacks_installed = [False]


def _ensure_ready():
    ffi, lib = get_ffi()
    if not _callbacks_installed[0]:
        _install_callbacks()
        _callbacks_installed[0] = True
    return ffi, lib


# ---------------------------------------------------------------------
# Pythonic surface
# ---------------------------------------------------------------------
FRAMING_LEGACY = 0
FRAMING_PROBING = 1
CLASS_SCHEDULED = 0
CLASS_PROMPT = 1
CLASS_TELEMETRY = 2


def abi_version():
    _, lib = _ensure_ready()
    return lib.intentproto_abi_version()


def version_string():
    ffi, lib = _ensure_ready()
    return ffi.string(lib.intentproto_version_string()).decode()


def crc16_ccitt(data):
    ffi, lib = _ensure_ready()
    return lib.ip_crc16_ccitt(data, len(data))


def vlq_encode(v):
    ffi, lib = _ensure_ready()
    out = ffi.new("uint8_t[8]")
    n = lib.ip_vlq_encode(out, v & 0xffffffff)
    return bytes(ffi.buffer(out, n))


def vlq_decode(data, pos=0):
    ffi, lib = _ensure_ready()
    buf = bytes(data)
    out = ffi.new("uint32_t *")
    n = lib.ip_vlq_decode(buf[pos:], len(buf) - pos, out)
    if n == 0:
        raise ValueError("truncated VLQ")
    return out[0], pos + n


class HostSession(object):
    # A retransmit-window host session (host.hpp) behind the C ABI.
    #   on_write(frame_bytes)     - transport transmit hook (required)
    #   on_response(payload_bytes)- one call per received message frame
    _next_token = [1]

    def __init__(self, on_write, on_response=None,
                 desired_framing=FRAMING_LEGACY):
        self._ffi, self._lib = _ensure_ready()
        self._token = HostSession._next_token[0]
        HostSession._next_token[0] += 1
        _write_sinks[self._token] = on_write
        if on_response is not None:
            _response_sinks[self._token] = on_response
        user = self._ffi.cast("void *", self._token)
        self._h = self._lib.ip_host_session_create(
            self._lib.ip_py_host_write, user,
            self._lib.ip_py_host_response, user, desired_framing)
        if self._h == self._ffi.NULL:
            raise MemoryError("ip_host_session_create failed")

    def send_command(self, payload, cls=CLASS_SCHEDULED):
        payload = bytes(payload)
        return bool(self._lib.ip_host_session_send_command(
            self._h, payload, len(payload), cls))

    def on_rx(self, data):
        data = bytes(data)
        self._lib.ip_host_session_on_rx(self._h, data, len(data))

    def need_retransmit(self, now_ticks, rto_ticks):
        return bool(self._lib.ip_host_session_need_retransmit(
            self._h, now_ticks, rto_ticks))

    def enable_v2(self):
        return bool(self._lib.ip_host_session_enable_v2(self._h))

    @property
    def inflight(self):
        return self._lib.ip_host_session_inflight(self._h)

    def class_stats(self, cls):
        out = self._ffi.new("ip_class_stats *")
        self._lib.ip_host_session_class_stats(self._h, cls, out)
        return {"tx_msgs": out.tx_msgs, "tx_bytes": out.tx_bytes,
                "rx_msgs": out.rx_msgs, "rx_bytes": out.rx_bytes,
                "dropped": out.dropped}

    def diag(self):
        out = self._ffi.new("ip_host_diag *")
        self._lib.ip_host_session_diag(self._h, out)
        return {"retransmits": out.retransmits, "naks": out.naks,
                "rx_crc_errors": out.rx_crc_errors,
                "rx_bch_errors": out.rx_bch_errors,
                "rx_framing_errors": out.rx_framing_errors,
                "v2_frames_rx": out.v2_frames_rx,
                "v2_rejected": bool(out.v2_rejected),
                "framing_v2": bool(out.framing_v2)}

    def close(self):
        if self._h is not None and self._h != self._ffi.NULL:
            self._lib.ip_host_session_free(self._h)
            self._h = None
        _write_sinks.pop(self._token, None)
        _response_sinks.pop(self._token, None)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Device(object):
    # The library's device singleton (proto.hpp init()/rx()) behind the
    # C ABI. Only one is live at a time (it is a process global).
    def __init__(self, on_write, version="intentproto-py",
                 build_version=""):
        self._ffi, self._lib = _ensure_ready()
        _device_write_sink[0] = on_write
        self._lib.ip_device_init(
            self._lib.ip_py_device_write, self._ffi.NULL,
            version.encode(), build_version.encode())

    def rx(self, data):
        data = bytes(data)
        self._lib.ip_device_rx(data, len(data))

    def commands(self):
        # [(index, id, name, key)] for every registered command.
        out = []
        for i in range(self._lib.ip_command_count()):
            out.append((i, self._lib.ip_command_id(i),
                        self._ffi.string(self._lib.ip_command_name(i)).decode(),
                        self._key(self._lib.ip_command_key, i)))
        return out

    def responses(self):
        out = []
        for i in range(self._lib.ip_response_count()):
            out.append((i, self._lib.ip_response_id(i),
                        self._ffi.string(
                            self._lib.ip_response_name(i)).decode(),
                        self._key(self._lib.ip_response_key, i)))
        return out

    def command_id(self, name):
        idx = self._lib.ip_command_index_by_name(name.encode())
        if idx < 0:
            raise KeyError(name)
        return self._lib.ip_command_id(idx)

    def _key(self, fn, idx):
        buf = self._ffi.new("char[128]")
        n = fn(idx, buf, 128)
        return bytes(self._ffi.buffer(buf, n)).decode()


__all__ = [
    "get_ffi", "build", "abi_version", "version_string",
    "crc16_ccitt", "vlq_encode", "vlq_decode",
    "HostSession", "Device",
    "FRAMING_LEGACY", "FRAMING_PROBING",
    "CLASS_SCHEDULED", "CLASS_PROMPT", "CLASS_TELEMETRY",
]
