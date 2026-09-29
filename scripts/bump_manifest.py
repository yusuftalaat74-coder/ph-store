#!/usr/bin/env python3
"""Read and rewrite apps/android/AndroidManifest.xml (binary AXML) without an
Android SDK.

    python3 scripts/bump_manifest.py                       # print what it says
    python3 scripts/bump_manifest.py --version-name 4.3 --version-code 43
    python3 scripts/bump_manifest.py --file-provider       # add the camera provider

Why this exists: the manifest is committed compiled, every build up to v4.2
shipped `versionCode=2 / versionName="2.0"`, and nothing in the repository
could change it. Android installs a build over the previous one only when the
signing key matches AND the versionCode is higher, so a version bump is not
cosmetic — it is what makes v4.3 an update rather than a refusal.

The file is parsed into its chunks (string pool, resource map, XML nodes),
located by walking them — nothing is found by byte offset — and written back
out whole. Writing it whole rather than patching bytes in place is what makes
adding an element possible: v4.3 adds a <provider> (the camera writes the
Alvará photo through it), which needs two attribute names the file did not
contain, and attribute names that carry a resource id have to sit in the
resource-mapped head of the string pool, which renumbers every string after
them. The writer interns every string again, so nothing refers to a stale
index, and it sorts each element's attributes by resource id, as aapt does
and as the framework's attribute lookup assumes.

Checked against `aapt dump xmltree` / `aapt dump badging` when aapt exists.
"""
from __future__ import annotations

import argparse
import pathlib
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "apps" / "android" / "AndroidManifest.xml"
ANDROID_NS = "http://schemas.android.com/apk/res/android"

RES_XML = 0x0003
RES_STRING_POOL = 0x0001
RES_XML_RESOURCE_MAP = 0x0180
XML_START_NS, XML_END_NS, XML_START_EL, XML_END_EL, XML_CDATA = 0x0100, 0x0101, 0x0102, 0x0103, 0x0104
UTF8_FLAG = 0x100
NO_INDEX = 0xFFFFFFFF

TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_BOOLEAN = 0x12

# android:* attribute resource ids used below (frameworks/base public.xml)
ATTR_ID = {
    "name": 0x01010003, "exported": 0x01010010, "authorities": 0x01010018,
    "grantUriPermissions": 0x0101001B, "versionCode": 0x0101021B, "versionName": 0x0101021C,
}


# ── reading ──────────────────────────────────────────────────────────────

def _read_pool(buf: bytes, off: int) -> list[str]:
    _, hsize, size, count, _styles, flags, strings_start, _ = struct.unpack_from("<HHIIIIII", buf, off)
    utf8 = bool(flags & UTF8_FLAG)
    offsets = struct.unpack_from(f"<{count}I", buf, off + hsize)
    base = off + strings_start
    out = []
    for o in offsets:
        p = base + o
        if utf8:
            n = buf[p]; p += 2 if n & 0x80 else 1           # utf-16 length, skipped
            n = buf[p]
            if n & 0x80:
                n = ((n & 0x7F) << 8) | buf[p + 1]; p += 2
            else:
                p += 1
            out.append(buf[p:p + n].decode("utf-8"))
        else:
            n = struct.unpack_from("<H", buf, p)[0]; p += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", buf, p)[0]; p += 2
            out.append(buf[p:p + 2 * n].decode("utf-16-le"))
    return out


class Attr:
    def __init__(self, ns, name, raw, vtype, data, res_id=None):
        self.ns, self.name, self.raw, self.type, self.data, self.res_id = ns, name, raw, vtype, data, res_id

    def __repr__(self):
        return f"Attr({self.name}={self.data!r}/{self.type:#x})"


class Node:
    """kind: 'ns-start' | 'ns-end' | 'start' | 'end' | 'cdata'"""
    def __init__(self, kind, line, **kw):
        self.kind, self.line = kind, line
        self.__dict__.update(kw)


def parse(buf: bytes):
    typ, hsize, total = struct.unpack_from("<HHI", buf, 0)
    if typ != RES_XML:
        raise ValueError("not a binary XML file")
    pos = hsize
    strings: list[str] = []
    res_map: list[int] = []
    nodes: list[Node] = []

    def s(i):
        return None if i == NO_INDEX else strings[i]

    while pos < total:
        ctype, chsize, csize = struct.unpack_from("<HHI", buf, pos)
        if ctype == RES_STRING_POOL:
            strings = _read_pool(buf, pos)
        elif ctype == RES_XML_RESOURCE_MAP:
            res_map = list(struct.unpack_from(f"<{(csize - chsize) // 4}I", buf, pos + chsize))
        elif ctype in (XML_START_NS, XML_END_NS):
            line = struct.unpack_from("<I", buf, pos + 8)[0]
            prefix, uri = struct.unpack_from("<II", buf, pos + chsize)
            nodes.append(Node("ns-start" if ctype == XML_START_NS else "ns-end", line,
                              prefix=s(prefix), uri=s(uri)))
        elif ctype == XML_START_EL:
            line = struct.unpack_from("<I", buf, pos + 8)[0]
            ns, name, astart, asize, count = struct.unpack_from("<IIHHH", buf, pos + chsize)
            attrs = []
            for k in range(count):
                a = pos + chsize + astart + k * asize
                ans, aname, raw, _vsize, _res0, vtype, data = struct.unpack_from("<IIIHBBI", buf, a)
                attrs.append(Attr(s(ans), strings[aname], s(raw), vtype,
                                  strings[data] if vtype == TYPE_STRING else data,
                                  res_map[aname] if aname < len(res_map) else None))
            nodes.append(Node("start", line, ns=s(ns), name=strings[name], attrs=attrs))
        elif ctype == XML_END_EL:
            line = struct.unpack_from("<I", buf, pos + 8)[0]
            ns, name = struct.unpack_from("<II", buf, pos + chsize)
            nodes.append(Node("end", line, ns=s(ns), name=strings[name]))
        elif ctype == XML_CDATA:
            line = struct.unpack_from("<I", buf, pos + 8)[0]
            idx = struct.unpack_from("<I", buf, pos + chsize)[0]
            nodes.append(Node("cdata", line, text=strings[idx]))
        else:
            raise ValueError(f"unexpected chunk {ctype:#x} at {pos}")
        pos += csize
    return nodes


# ── writing ──────────────────────────────────────────────────────────────

def _pool_bytes(strings: list[str]) -> bytes:
    data = bytearray()
    offsets = []
    for st in strings:
        offsets.append(len(data))
        u = st.encode("utf-16-le")
        n = len(u) // 2
        if n > 0x7FFF:
            data += struct.pack("<HH", 0x8000 | (n >> 16), n & 0xFFFF)
        else:
            data += struct.pack("<H", n)
        data += u + b"\x00\x00"
    while len(data) % 4:
        data += b"\x00"
    hsize = 28
    strings_start = hsize + 4 * len(strings)
    size = strings_start + len(data)
    return (struct.pack("<HHIIIIII", RES_STRING_POOL, hsize, size, len(strings), 0, 0, strings_start, 0) +
            struct.pack(f"<{len(strings)}I", *offsets) + bytes(data))


def write(nodes) -> bytes:
    # resource-mapped attribute names first, in id order, then the rest in
    # order of first use — the same layout aapt produces
    mapped: dict[str, int] = {}
    for n in nodes:
        if n.kind == "start":
            for a in n.attrs:
                if a.res_id is not None:
                    mapped[a.name] = a.res_id
    order = sorted(mapped, key=lambda k: mapped[k])
    strings = list(order)
    index = {k: i for i, k in enumerate(strings)}

    def intern(st):
        if st is None:
            return NO_INDEX
        if st not in index:
            index[st] = len(strings)
            strings.append(st)
        return index[st]

    def attr_name(a):
        return index[a.name] if a.res_id is not None else intern(a.name)

    body = bytearray()
    for n in nodes:
        if n.kind in ("ns-start", "ns-end"):
            t = XML_START_NS if n.kind == "ns-start" else XML_END_NS
            body += struct.pack("<HHIII", t, 16, 24, n.line, NO_INDEX) + \
                struct.pack("<II", intern(n.prefix), intern(n.uri))
        elif n.kind == "start":
            attrs = sorted(n.attrs, key=lambda a: (a.res_id is None, a.res_id or 0))
            ab = bytearray()
            for a in attrs:
                data = intern(a.data) if a.type == TYPE_STRING else a.data
                ab += struct.pack("<IIIHBBI", intern(a.ns), attr_name(a), intern(a.raw), 8, 0, a.type, data)
            ext = struct.pack("<IIHHHHHH", intern(n.ns), intern(n.name), 20, 20, len(attrs), 0, 0, 0)
            size = 16 + len(ext) + len(ab)
            body += struct.pack("<HHIII", XML_START_EL, 16, size, n.line, NO_INDEX) + ext + ab
        elif n.kind == "end":
            body += struct.pack("<HHIII", XML_END_EL, 16, 24, n.line, NO_INDEX) + \
                struct.pack("<II", intern(n.ns), intern(n.name))
        elif n.kind == "cdata":
            body += struct.pack("<HHIII", XML_CDATA, 16, 28, n.line, NO_INDEX) + \
                struct.pack("<IHBBI", intern(n.text), 8, 0, TYPE_STRING, intern(n.text))
    pool = _pool_bytes(strings)
    res_map = struct.pack("<HHI", RES_XML_RESOURCE_MAP, 8, 8 + 4 * len(order)) + \
        struct.pack(f"<{len(order)}I", *[mapped[k] for k in order])
    total = 8 + len(pool) + len(res_map) + len(body)
    return struct.pack("<HHI", RES_XML, 8, total) + pool + res_map + bytes(body)


# ── the edits ────────────────────────────────────────────────────────────

def _el(nodes, name):
    return next(n for n in nodes if n.kind == "start" and n.name == name)


def _get(el, name):
    return next((a for a in el.attrs if a.name == name), None)


def _android(name, vtype, data):
    return Attr(ANDROID_NS, name, data if vtype == TYPE_STRING else None, vtype, data, ATTR_ID[name])


def set_version(nodes, code: int | None, name: str | None) -> None:
    m = _el(nodes, "manifest")
    if code is not None:
        a = _get(m, "versionCode") or m.attrs.append(_android("versionCode", TYPE_INT_DEC, 0)) or _get(m, "versionCode")
        a.type, a.data, a.raw = TYPE_INT_DEC, code, None
    if name is not None:
        a = _get(m, "versionName") or m.attrs.append(_android("versionName", TYPE_STRING, "")) or _get(m, "versionName")
        a.type, a.data, a.raw = TYPE_STRING, name, name


PROVIDER_CLASS = "mz.rova.store.Files"
PROVIDER_AUTHORITY = "mz.rova.store.files"


def add_file_provider(nodes) -> bool:
    """<provider android:name="mz.rova.store.Files"
                  android:authorities="mz.rova.store.files"
                  android:exported="false" android:grantUriPermissions="true"/>
    as the last child of <application>. False when it is already there."""
    for n in nodes:
        if n.kind == "start" and n.name == "provider":
            nm = _get(n, "name")
            if nm and nm.data == PROVIDER_CLASS:
                return False
    app_end = next(i for i, n in enumerate(nodes) if n.kind == "end" and n.name == "application")
    line = max(n.line for n in nodes if n.kind in ("start", "end")) + 1
    start = Node("start", line, ns=None, name="provider", attrs=[
        _android("name", TYPE_STRING, PROVIDER_CLASS),
        _android("exported", TYPE_BOOLEAN, 0),
        _android("authorities", TYPE_STRING, PROVIDER_AUTHORITY),
        _android("grantUriPermissions", TYPE_BOOLEAN, 0xFFFFFFFF),
    ])
    end = Node("end", line, ns=None, name="provider")
    nodes[app_end:app_end] = [start, end]
    return True


def describe(nodes) -> str:
    out, depth = [], 0
    for n in nodes:
        if n.kind == "start":
            attrs = " ".join(f"{a.name}={a.data!r}" for a in n.attrs)
            out.append("  " * depth + f"<{n.name} {attrs}>")
            depth += 1
        elif n.kind == "end":
            depth -= 1
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(MANIFEST))
    ap.add_argument("--version-code", type=int)
    ap.add_argument("--version-name")
    ap.add_argument("--file-provider", action="store_true",
                    help="add the content provider the camera writes the Alvará photo through")
    args = ap.parse_args()

    path = pathlib.Path(args.file)
    nodes = parse(path.read_bytes())
    changed = False
    if args.version_code is not None or args.version_name is not None:
        set_version(nodes, args.version_code, args.version_name)
        changed = True
    if args.file_provider:
        changed = add_file_provider(nodes) or changed
    if changed:
        out = write(nodes)
        # what was written has to read back as what was meant
        again = parse(out)
        assert describe(again) == describe(nodes), "the rewritten manifest does not read back the same"
        path.write_bytes(out)
        print(f"  wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path} ({len(out)} bytes)")
    m = _el(nodes, "manifest")
    print(f"  package={_get(m, 'package').data} versionCode={_get(m, 'versionCode').data} "
          f"versionName={_get(m, 'versionName').data}")
    if not changed:
        print(describe(nodes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
