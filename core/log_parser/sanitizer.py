"""BYOND inline HTML sanitizer, color bucket classifier, and IP/CID privacy masking."""

from __future__ import annotations

from html import escape as html_escape
from html import unescape
from html.parser import HTMLParser
from ipaddress import IPv6Address
import re
from typing import List, Optional, Tuple

from core.log_parser.models import (
    _COLOR_CLASS_MAP,
    _HEX_COLOR_RE,
    _IMPORTANT_SUFFIX_RE,
    _IPV4_RE,
    _IP_CID_RE,
    _LOC_COORDS_RE,
    _RGB_COLOR_RE,
    _STYLE_COLOR_RE,
)
from core.text_encoding import decode_byond_text

_IPV6_CANDIDATE = re.compile(
    r"(?<![\w:.])(?P<address>(?:[\da-f]{0,4}:){1,8}[\da-f:.]*[\da-f:])(?P<cid>-[0-9]{6,14}\b)?",
    re.IGNORECASE | re.ASCII,
)
_LABELLED_CID = re.compile(r"(\b(?:CID|computer[ _-]?id)\s*[:=]?\s*)[0-9]+\b", re.IGNORECASE)


def _classify_rgb_bucket(r: int, g: int, b: int) -> Optional[str]:
    if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
        return None
    if r > 200 and 100 <= g <= 190 and b < 80:
        return "log-c-orange"
    if r > 170 and g < 90 and b < 90:
        return "log-c-red"
    if g > 150 and r < 110 and b < 110:
        return "log-c-green"
    if r > 180 and g > 160 and b < 100:
        return "log-c-amber"
    if g > 140 and b > 140 and r < 100:
        return "log-c-cyan"
    if b > 160 and r < 110:
        return "log-c-blue"
    if r > 130 and b > 130 and g < 120:
        return "log-c-purple"
    if max(r, g, b) - min(r, g, b) <= 18 and 64 <= r <= 192:
        return "log-c-gray"
    return None


def _bucket_color_class(raw_color: str) -> Optional[str]:
    color = _IMPORTANT_SUFFIX_RE.sub("", (raw_color or "")).strip().lower()
    if not color:
        return None
    if color in _COLOR_CLASS_MAP:
        return _COLOR_CLASS_MAP[color]
    m_hex = _HEX_COLOR_RE.match(color)
    if m_hex:
        hex_val = m_hex.group(1)
        if len(hex_val) == 3:
            hex_val = "".join(ch * 2 for ch in hex_val)
        return _classify_rgb_bucket(int(hex_val[0:2], 16), int(hex_val[2:4], 16), int(hex_val[4:6], 16))
    m_rgb = _RGB_COLOR_RE.match(color)
    if m_rgb:
        return _classify_rgb_bucket(int(m_rgb.group(1)), int(m_rgb.group(2)), int(m_rgb.group(3)))
    return None


class _ByondSanitizer(HTMLParser):
    """CSP-safe sanitizer for inline BYOND HTML in SS13 logs (zero inline styles, zero script/hrefs)."""

    _SIMPLE_ALLOWED = {"b": "strong", "strong": "strong", "i": "em", "em": "em", "u": "u"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_parts: List[str] = []
        self.plain_parts: List[str] = []
        self._tag_stack: List[Tuple[str, str]] = []
        self._in_byond_link: Optional[str] = None
        self._link_href: str = ""
        self._link_text: List[str] = []

    def _emit_literal(self, raw: str) -> None:
        if self._in_byond_link is not None:
            self._link_text.append(raw)
            return
        self.html_parts.append(html_escape(raw))
        self.plain_parts.append(raw)

    def _flush_link_as_text(self) -> None:
        leftover = "".join(self._link_text)
        self.html_parts.append(html_escape(leftover))
        self.plain_parts.append(leftover)
        self._in_byond_link = None
        self._link_href = ""
        self._link_text = []

    def _close_tag_kind(self, target_kind: str) -> None:
        if not any(kind == target_kind for kind, _ in self._tag_stack):
            return
        while self._tag_stack:
            kind, emitted = self._tag_stack.pop()
            if emitted:
                self.html_parts.append(f"</{emitted}>")
            if kind == target_kind:
                break

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if t == "a":
            if self._in_byond_link is not None:
                self._flush_link_as_text()
            href = attr_map.get("href", "").strip()
            if href.lower().startswith("byond://"):
                self._in_byond_link = "byond"
                self._link_href = href
                self._link_text = []
            else:
                self._in_byond_link = "other"
                self._link_href = ""
                self._link_text = []
            return

        if self._in_byond_link is not None:
            if t in self._SIMPLE_ALLOWED or t in ("span", "font"):
                return
            raw = self.get_starttag_text() or f"<{tag}>"
            self._link_text.append(raw)
            return

        if t in self._SIMPLE_ALLOWED:
            mapped = self._SIMPLE_ALLOWED[t]
            self.html_parts.append(f"<{mapped}>")
            self._tag_stack.append((mapped, mapped))
            return

        if t in ("span", "font"):
            raw_color = attr_map.get("color", "")
            if not raw_color and "style" in attr_map:
                m = _STYLE_COLOR_RE.search(attr_map["style"])
                if m:
                    raw_color = m.group(1)
            cls = _bucket_color_class(raw_color)
            if cls:
                self.html_parts.append(f'<span class="{cls}">')
                self._tag_stack.append(("span", "span"))
            else:
                self._tag_stack.append(("span", ""))
            return

        # Unknown / dangerous tag -> render safely escaped as literal text so admins see raw chat payloads
        raw = self.get_starttag_text() or f"<{tag}>"
        self._emit_literal(raw)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in self._SIMPLE_ALLOWED or t in ("span", "font", "a"):
            return
        raw = self.get_starttag_text() or f"<{tag}/>"
        self._emit_literal(raw)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "a" and self._in_byond_link is not None:
            link_label = "".join(self._link_text).strip()
            href = self._link_href
            mode = self._in_byond_link
            self._in_byond_link = None
            self._link_href = ""
            self._link_text = []

            if mode == "byond":
                coords = _LOC_COORDS_RE.search(href)
                if coords and link_label.upper() == "LOC":
                    badge = f"[{coords.group(1)}, {coords.group(2)}, {coords.group(3)}]"
                    self.html_parts.append(f'<span class="log-loc-badge">{html_escape(badge)}</span>')
                    self.plain_parts.append(badge)
                    return
                if link_label.upper() == "MOB":
                    self.html_parts.append('<span class="log-mob-badge">[MOB]</span>')
                    self.plain_parts.append("[MOB]")
                    return
            escaped_label = html_escape(link_label)
            self.html_parts.append(escaped_label)
            self.plain_parts.append(link_label)
            return

        if self._in_byond_link is not None:
            if t in self._SIMPLE_ALLOWED or t in ("span", "font"):
                return
            self._link_text.append(f"</{tag}>")
            return

        if t in self._SIMPLE_ALLOWED:
            self._close_tag_kind(self._SIMPLE_ALLOWED[t])
            return

        if t in ("span", "font"):
            self._close_tag_kind("span")
            return

        self._emit_literal(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._emit_literal(data)

    def handle_comment(self, data: str) -> None:
        self._emit_literal(f"<!--{data}-->")

    def close(self) -> None:
        super().close()
        if self._in_byond_link is not None:
            self._flush_link_as_text()
        while self._tag_stack:
            _kind, emitted = self._tag_stack.pop()
            if emitted:
                self.html_parts.append(f"</{emitted}>")


def sanitize_byond_html(raw_text: str) -> Tuple[str, str]:
    """Sanitize BYOND log text into (safe_html, plain_text) with zero XSS and zero inline styles."""
    if "<" not in raw_text and "&" not in raw_text:
        escaped = html_escape(raw_text)
        return escaped, raw_text
    parser = _ByondSanitizer()
    parser.feed(raw_text)
    parser.close()
    return "".join(parser.html_parts), "".join(parser.plain_parts)


def mask_sensitive_text(text: str) -> str:
    """Redact addresses and labelled or address-paired BYOND computer IDs."""
    def mask_ipv6(match: re.Match[str]) -> str:
        try:
            IPv6Address(match["address"])
        except ValueError:
            return match.group()
        return "[IP-СКРЫТ]-[CID-СКРЫТ]" if match["cid"] else "[IP-СКРЫТ]"

    masked = _IPV6_CANDIDATE.sub(mask_ipv6, text)
    masked = _IP_CID_RE.sub("[IP-СКРЫТ]-[CID-СКРЫТ]", masked)
    masked = _IPV4_RE.sub("[IP-СКРЫТ]", masked)
    return _LABELLED_CID.sub(r"\g<1>[CID-СКРЫТ]", masked)


def prepare_log_message(raw_text: str, *, mask_ips: bool) -> Tuple[str, Tuple[str, str]]:
    """Prepare both display representations from the same redacted message."""
    if not mask_ips:
        return raw_text, sanitize_byond_html(raw_text)
    safe_html, plain_text = sanitize_byond_html(mask_sensitive_text(raw_text))
    masked = mask_sensitive_text(unescape(plain_text))
    if masked != plain_text:
        safe_html = html_escape(masked)
    # Export and plain view must not carry hidden attributes from the original markup.
    return masked, (safe_html, masked)


def decode_log_bytes(data: bytes) -> str:
    """Decode raw log bytes preferring UTF-8 with CP1251/CP1252/latin-1 fallback."""
    return decode_byond_text(data)
