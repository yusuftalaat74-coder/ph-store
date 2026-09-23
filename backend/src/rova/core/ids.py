"""A2.1 — TEXT primary keys as <prefix>_<ULID>.

A minimal, dependency-free, monotonic-within-process ULID (Crockford base32,
26 chars): 48-bit millisecond timestamp + 80 bits of randomness, with the
random part incremented by one when two ids are generated in the same
millisecond so ids sort monotonically within one process.
"""
import os
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _ulid() -> str:
    global _last_ms, _last_rand
    with _lock:
        ms = int(time.time() * 1000)
        if ms == _last_ms:
            _last_rand += 1
        else:
            _last_ms = ms
            _last_rand = int.from_bytes(os.urandom(10), "big") & ((1 << 80) - 1)
        rand = _last_rand
    return _encode(ms, 10) + _encode(rand, 16)


def new_id(prefix: str) -> str:
    """<prefix>_<26-char ULID>, e.g. new_id('usr') -> 'usr_01J...'"""
    return f"{prefix}_{_ulid()}"
