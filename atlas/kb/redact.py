# A8 redaction pass — the settled "numeric-only unredacted" policy
# (FD-0002 §6; decided 2026-07-12).
#
# Redact by default. Three tiers, enforced here and unit-tested as part
# of the deterministic floor (nothing leaves the Pi without passing this):
#   (a) always-share, raw: numeric values (the diagnostic signal, no PII)
#       and a small allowlist of safe structural strings (mcu/board
#       family, kinematics, event name, fault class, versions, ABI hash).
#   (b) transform-then-share: file paths -> basename; other free-text
#       strings -> dropped; absolute wall-clock -> dropped (leaks
#       location/schedule); relative machine-time is kept.
#   (c) never-share, no allowlist override possible: secrets, keys, PSKs,
#       tokens, hostnames/IPs/MACs, serials/UUIDs, account identifiers.
#
# The key rule: a field key is split on non-alphanumerics and, if any
# token is a sensitive word, the field is dropped whatever its type — so
# a secret can never be shared even by naming it in the allowlist.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import re
from dataclasses import dataclass, field

# Tier (c): tokens that mark a field as never-shareable. Matched against
# the key's word-split so 'api_key' and 'wifi_ssid' drop while
# 'queue_depth' and 'mcu_clock' do not.
_SENSITIVE_TOKENS = frozenset({
    "password", "passwd", "pass", "psk", "secret", "token", "key",
    "apikey", "wifi", "ssid", "mac", "ip", "ipaddr", "hostname",
    "serial", "uuid", "url", "uri", "account", "user", "username",
    "email", "gps", "lat", "lon", "latitude", "longitude", "path",
    "file", "filename", "dir", "home",
})

# Tier (a): structural strings safe to share verbatim.
_SAFE_STRING_KEYS = frozenset({
    "mcu", "board", "kinematics", "event", "fault_class", "kind",
    "severity", "sub", "version", "mcu_version", "sw_version",
    "abi", "protocol_hash", "exc_type", "time_basis",
})

# Tier (b): absolute wall-clock keys that leak location/schedule.
_WALLCLOCK_KEYS = frozenset({"wall", "systime", "wall_time", "epoch"})

_RE_SPLIT = re.compile(r"[^a-z0-9]+")


def _is_sensitive_key(key: str) -> bool:
    tokens = set(_RE_SPLIT.split(str(key).lower()))
    return bool(tokens & _SENSITIVE_TOKENS)


@dataclass
class RedactionPolicy:
    """Tunable knobs; the defaults are the settled policy."""
    keep_numeric: bool = True
    basename_paths: bool = True     # path strings -> basename (else drop)
    safe_string_keys: frozenset = _SAFE_STRING_KEYS
    drop_wallclock: bool = True

    def value(self, key, val):
        """Redact one (key, value); returns (keep: bool, value)."""
        if _is_sensitive_key(key):
            return (False, None)                     # tier (c): never
        if self.drop_wallclock and str(key).lower() in _WALLCLOCK_KEYS:
            return (False, None)                     # tier (b): drop absolute time
        if isinstance(val, bool) or isinstance(val, (int, float)):
            return (self.keep_numeric, val)          # tier (a): numeric raw
        if isinstance(val, str):
            if key in self.safe_string_keys:
                return (True, val)                   # tier (a): safe structural
            if "/" in val:                           # tier (b): path -> basename
                return (self.basename_paths, val.rsplit("/", 1)[-1]) \
                    if self.basename_paths else (False, None)
            return (False, None)                     # tier (b): free-text dropped
        if isinstance(val, dict):
            return (True, self.fields(val))
        if isinstance(val, (list, tuple)):
            out = []
            for item in val:
                keep, red = self.value(key, item)
                if keep:
                    out.append(red)
            return (True, out)
        return (False, None)                          # unknown type: drop

    def fields(self, d: dict) -> dict:
        out = {}
        for key, val in d.items():
            keep, red = self.value(key, val)
            if keep:
                out[key] = red
        return out


DEFAULT_POLICY = RedactionPolicy()


def redact_value(key, val, policy=DEFAULT_POLICY):
    return policy.value(key, val)


def redact_fields(d: dict, policy=DEFAULT_POLICY) -> dict:
    return policy.fields(d)


def _scrub_summary(summary: str) -> str:
    # A rendered structural summary is kept, but any path-like token is
    # reduced to its basename so a stray filename never rides along.
    return " ".join(
        tok.rsplit("/", 1)[-1] if "/" in tok else tok
        for tok in summary.split())


def redact_event(event, policy=DEFAULT_POLICY) -> dict:
    """Redact a timeline Event into a shareable dict.

    Keeps the structural spine (order, kind, severity, source subsystem,
    relative machine-time) and redacted numeric fields; drops the raw
    source text entirely (it can carry paths and secrets).
    """
    return {
        "seq": event.seq,
        "kind": event.kind,
        "severity": event.severity,
        "source": event.source,
        "mtime": event.mtime,          # relative machine-time, kept
        "time_basis": event.time_basis,
        "summary": _scrub_summary(event.summary),
        "fields": policy.fields(event.fields),
        # note: event.raw is deliberately NOT included.
    }
