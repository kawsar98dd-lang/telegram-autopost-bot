"""Seller-controlled branding.

These values live in source code (not in .env) on purpose: .env belongs to the
customer, this file belongs to the seller. Edit before packaging the product.

The footer is appended by the application before every send. The customer
dashboard has no option to edit or remove it. Note that Telegram itself cannot
make part of a personal account's message immutable; see README "Limitations".
"""

APP_NAME = "Telegram Auto Poster"
APP_DESCRIPTION = "Schedule marketing posts to your Telegram groups from your own account."
SUPPORT_URL = ""

FOOTER_SEPARATOR = "━━━━━━━━━━━━━━━━"
FOOTER_USERNAME = "@YourService"
FOOTER_TEXT = "🤖 Auto Posted by {username}"


def render_footer() -> str:
    """Return the fixed footer block (separator line + text). Contains no secrets."""
    return f"{FOOTER_SEPARATOR}\n{FOOTER_TEXT.format(username=FOOTER_USERNAME)}"
