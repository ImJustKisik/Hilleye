"""Decode modern and legacy BYOND text without discarding invalid UTF-8 bytes."""


def decode_byond_text(data: bytes, legacy_encoding: str = "cp1251") -> str:
    """Prefer UTF-8; use the declared legacy charset or the Russian Hub default.

    Single-byte encodings are ambiguous without metadata. CP1251 is preferred
    for unlabelled legacy responses; CP1252 also covers its undefined byte 0x98.
    """
    for encoding in ("utf-8", legacy_encoding, "cp1251", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise AssertionError("latin-1 decodes every byte")
