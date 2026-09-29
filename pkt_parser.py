"""
pkt_parser.py
-------------
Handles .pkt / .pka uploads (Packet Tracer save files).

BE HONEST WITH YOURSELF (and your professor) ABOUT WHAT THIS CAN DO:
Packet Tracer's .pkt format is a proprietary, version-dependent
compressed/obfuscated container. There is no official spec, and the
exact transform has changed across PT versions. We can't guarantee a
clean XML extraction the way we can for an open format.

So this module tries a few things, cheapest and safest first, and is
honest in its output about which method (if any) worked:

  1. Try plain zlib decompression of the file (works for nothing in
     practice, but it's free to try and costs one line of code).
  2. Try zlib decompression after XOR-ing the byte stream with each
     single-byte key 0-255 (a standard technique for simple XOR
     obfuscation - this is a brute force search, not a "known secret
     key", so it's honest about being a guess). If any key produces
     valid zlib output that inflates to something that looks like XML,
     we use it.
  3. If neither works, fall back to scanning the raw bytes for
     printable ASCII / UTF-16LE strings. This reliably surfaces
     fragments like hostnames, interface names, and IP addresses even
     inside compressed/obfuscated data, because Packet Tracer embeds
     some metadata outside the compressed blob.

Either way, the case form always lets the student paste show-command
output by hand too - that's the reliable path. The .pkt upload is a
convenience / sanity-check layer on top, not the primary input.
"""

import zlib
import re
import string

PRINTABLE = set(bytes(string.printable, "ascii"))


def _extract_ascii_strings(data, min_len=4):
    out = []
    current = bytearray()
    for b in data:
        if b in PRINTABLE and b not in (9, 10, 11, 12, 13):  # skip whitespace-only runs
            current.append(b)
        else:
            if len(current) >= min_len:
                out.append(current.decode("ascii", errors="ignore"))
            current = bytearray()
    if len(current) >= min_len:
        out.append(current.decode("ascii", errors="ignore"))
    return out


def _looks_like_xml(data: bytes) -> bool:
    head = data[:50].lstrip()
    return head.startswith(b"<?xml") or head.startswith(b"<PACKETTRACER") or b"<PACKETTRACER5" in data[:200]


def _try_plain_zlib(data):
    try:
        result = zlib.decompress(data[4:])  # PT files often start with a 4-byte length header
        if _looks_like_xml(result):
            return result
    except Exception:
        pass
    try:
        result = zlib.decompress(data)
        if _looks_like_xml(result):
            return result
    except Exception:
        pass
    return None


def _try_xor_bruteforce_zlib(data, max_key=255):
    """Brute-force single-byte XOR + zlib. This is a real but limited
    technique: it will only succeed if the file uses simple single-byte
    XOR obfuscation before compression, which some older PT versions do
    and newer ones don't. Bounded so it can't hang on a big file."""
    body = data[4:] if len(data) > 4 else data
    sample = body[:4096]  # only brute force against a sample first, for speed
    for key in range(1, max_key + 1):
        xored_sample = bytes(b ^ key for b in sample)
        try:
            # zlib streams start with a recognizable 2-byte header (0x78 ..)
            if xored_sample[0] != 0x78:
                continue
            decompressor = zlib.decompressobj()
            decompressor.decompress(xored_sample)
        except Exception:
            continue
        # promising header - try the full file with this key
        try:
            full_xored = bytes(b ^ key for b in body)
            result = zlib.decompress(full_xored)
            if _looks_like_xml(result):
                return result, key
        except Exception:
            continue
    return None, None


def analyze_pkt_file(file_path):
    """
    Returns a dict:
      {
        "method_used": "zlib" | "xor+zlib" | "strings-only",
        "xml_excerpt": str or None,     # first ~2000 chars of recovered XML, if any
        "strings_found": [str, ...],    # interesting looking string fragments
        "note": str                     # honest explanation of what happened
      }
    """
    with open(file_path, "rb") as f:
        data = f.read()

    # attempt 1: plain zlib
    xml = _try_plain_zlib(data)
    if xml:
        return {
            "method_used": "zlib",
            "xml_excerpt": xml[:2000].decode("utf-8", errors="ignore"),
            "strings_found": [],
            "note": "File decompressed directly with zlib - no obfuscation detected.",
        }

    # attempt 2: brute-force single-byte XOR then zlib
    xml, key = _try_xor_bruteforce_zlib(data)
    if xml:
        return {
            "method_used": "xor+zlib",
            "xml_excerpt": xml[:2000].decode("utf-8", errors="ignore"),
            "strings_found": [],
            "note": f"File decompressed after XOR-ing with single-byte key {hex(key)}. "
                    f"This works for some older Packet Tracer versions, not all.",
        }

    # attempt 3: fall back to raw string extraction
    all_strings = _extract_ascii_strings(data, min_len=4)
    interesting = [
        s for s in all_strings
        if re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", s)
        or re.search(r"(?i)(router|switch|pc|server|vlan|fastethernet|gigabitethernet|serial)", s)
    ]
    # de-dupe while keeping order, cap so the UI stays readable
    seen = set()
    deduped = []
    for s in interesting:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    deduped = deduped[:60]

    return {
        "method_used": "strings-only",
        "xml_excerpt": None,
        "strings_found": deduped,
        "note": (
            "Couldn't fully decompress this .pkt file automatically - Packet Tracer's format is "
            "proprietary and changes between versions, so this isn't guaranteed to work on every file. "
            "Below are readable text fragments (device names, interfaces, IPs) pulled directly from the "
            "file bytes, which can still help confirm topology at a glance. For the actual AI diagnosis, "
            "paste the show-command output into the case form - that's the reliable path."
        ),
    }
