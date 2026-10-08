"""Every size limit of Step 5 in ONE place (nothing else in the code base repeats these numbers).

Telegram limits (documented by Telegram for messages sent through the API, counted in UTF-16 code units; Telethon does not
export them as constants):
* a text message may hold up to 4096 characters,
* the caption of a photo may hold up to 1024 characters. Telegram Premium accounts may use more, but the application cannot
  know which kind of account is connected, so the lower limit that works for everybody is used,
* a photo may be at most 10 MB, width + height at most 10000 pixels, and a ratio of at most 20:1.

The mandatory footer is part of the message, so it is counted against these limits.
"""

from __future__ import annotations

TELEGRAM_MAX_TEXT_UNITS = 4096
TELEGRAM_MAX_CAPTION_UNITS = 1024
TELEGRAM_MAX_PHOTO_BYTES = 10 * 1024 * 1024
TELEGRAM_MAX_PHOTO_DIMENSION_SUM = 10_000
TELEGRAM_MAX_PHOTO_RATIO = 20

# Application limits
MAX_TITLE_CHARS = 120
MAX_BODY_INPUT_CHARS = 12_000         # raw input accepted before normalisation; the Telegram limit is checked afterwards
MAX_TARGETS_PER_POST = 200            # same ceiling as the Step 4 group selection (app.telegram.group_sync.MAX_SELECTED_GROUPS)
MAX_POSTS_PER_USER = 500
MAX_MEDIA_BYTES_PER_USER = 256 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 512 * 1024  # text fields + group checkboxes + multipart framing, on top of the image limit
ORPHAN_GRACE_SECONDS = 3600            # a stored file without a database row is deleted only after this long


def max_image_bytes(configured_mb: int) -> int:
    """The configured MAX_UPLOAD_MB, but never more than Telegram accepts for a photo."""
    return min(int(configured_mb) * 1024 * 1024, TELEGRAM_MAX_PHOTO_BYTES)
