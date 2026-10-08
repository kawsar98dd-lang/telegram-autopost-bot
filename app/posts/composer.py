"""The ONE place that turns what a user typed into the message that is previewed and (from Step 6 on) sent.

    final = compose(body, has_image=...)      # used by the preview now, and by the Step 6 worker at send time

* The footer comes from app/branding.py (seller-controlled source code). There is no form field, query parameter, JSON key
  or database column through which a request could change, disable or replace it, and the browser never supplies the final
  text: it is always rebuilt on the server from the stored body.
* The body is stored WITHOUT footer. Whatever footer text the user typed or pasted (at the end, in the middle, partly) is
  removed, and exactly one footer is appended, so a footer can neither be doubled nor dropped.
* Plain text only: nothing is parsed as Markdown/HTML, and the message is sent without a parse mode, so no markup can be
  injected into Telegram either.
* The Telegram length limits (app/posts/limits.py) are checked on the FINAL text, footer included.

Telegram cannot make a footer immutable for the owner of a personal account; this is enforcement inside the application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import branding
from .limits import MAX_BODY_INPUT_CHARS, TELEGRAM_MAX_CAPTION_UNITS, TELEGRAM_MAX_TEXT_UNITS

SEPARATOR_FROM_BODY = "\n\n"
# C0 controls except tab/newline, DEL, and the bidirectional override/isolate characters (they can disguise text).
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069\u200b-\u200d\u2060\ufeff]")


class MessageError(Exception):
    """The message cannot be composed; ``code`` is a stable machine value, ``message`` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(code)


@dataclass(frozen=True)
class FinalMessage:
    text: str          # exactly what would be sent (body + blank line + footer)
    body: str          # the user's part, normalised, without footer
    footer: str        # the footer block that was appended
    units: int         # length in UTF-16 code units (how Telegram counts)
    limit: int         # the limit that applies (text or caption)
    kind: str          # "text" | "caption"

    @property
    def remaining(self) -> int:
        return self.limit - self.units


def footer_block() -> str:
    """The complete footer exactly as it is appended. Single source: branding.render_footer()."""
    return branding.render_footer()


def _footer_text_line() -> str:
    return branding.FOOTER_TEXT.format(username=branding.FOOTER_USERNAME)


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def limit_for(has_image: bool) -> tuple[int, str]:
    return (TELEGRAM_MAX_CAPTION_UNITS, "caption") if has_image else (TELEGRAM_MAX_TEXT_UNITS, "text")


def _flex(text: str) -> "re.Pattern[str]":
    """Pattern that matches ``text`` ignoring case and differences in the amount of white space."""
    parts = [re.escape(p) for p in text.split()]
    return re.compile(r"\s*".join(parts) if parts else r"(?!x)x", re.IGNORECASE)


def strip_footer(text: str) -> str:
    """Remove every occurrence of the footer (block, text line, or just the text) from ``text``."""
    line = _footer_text_line()
    separator = branding.FOOTER_SEPARATOR
    block = _flex(separator + "\n" + line)
    inline = _flex(line)
    for _ in range(20):  # removing text can join its neighbours into a new occurrence; repeat until stable
        before = text
        text = block.sub("", text)
        text = inline.sub("", text)
        if text == before:
            break
    else:  # pragma: no cover - a 20-deep nesting is not a real post
        raise MessageError("footer_nesting", "The text contains too many repetitions of the automatic footer.")
    # a separator line that lost its footer (the user deleted only the text line) is removed as well
    kept = [ln for ln in text.split("\n") if ln.strip() != separator.strip()] if separator.strip() else text.split("\n")
    return "\n".join(kept)


def normalize_body(raw: str) -> str:
    """Canonical stored form of the user's text: LF newlines, no control characters, no footer, trimmed."""
    if not isinstance(raw, str):
        raise MessageError("bad_text", "The text is not valid.")
    if len(raw) > MAX_BODY_INPUT_CHARS:
        raise MessageError("too_long", "The text is too long.")
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _UNSAFE.sub("", text)
    text = strip_footer(text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{4,}", "\n\n\n", text)  # at most two blank lines in a row
    return text.strip("\n").strip()


def compose(body: str, *, has_image: bool) -> FinalMessage:
    """Build the final message from a stored (or freshly submitted) body. Never raises for length: see ``check``."""
    clean = normalize_body(body)
    footer = footer_block()
    text = f"{clean}{SEPARATOR_FROM_BODY}{footer}" if clean else footer
    limit, kind = limit_for(has_image)
    message = FinalMessage(text=text, body=clean, footer=footer, units=utf16_units(text), limit=limit, kind=kind)
    if text.count(_footer_text_line()) != 1:  # invariant: the footer text is present exactly once
        raise MessageError("footer_invariant", "The message could not be composed safely.")
    return message


def check(message: FinalMessage) -> None:
    """Raise MessageError if the FINAL text (footer included) does not fit into a Telegram message / caption."""
    if message.units > message.limit:
        what = "photo caption" if message.kind == "caption" else "message"
        over = message.units - message.limit
        raise MessageError(
            "too_long",
            f"The {what} is {over} character{'' if over == 1 else 's'} too long. Telegram allows {message.limit} "
            f"characters including the automatic footer.")


def max_body_units(has_image: bool) -> int:
    """How many UTF-16 units the user's own text may use (the limit minus the footer and the blank line)."""
    limit, _ = limit_for(has_image)
    return limit - utf16_units(footer_block()) - utf16_units(SEPARATOR_FROM_BODY)
