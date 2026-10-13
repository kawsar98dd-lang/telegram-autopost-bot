"""The official footer: exact text, appended exactly once, counted against the limits, no placeholder left anywhere."""

import re
import unittest
from pathlib import Path

from app import branding
from app.posts import composer
from app.posts.limits import TELEGRAM_MAX_CAPTION_UNITS, TELEGRAM_MAX_TEXT_UNITS

ROOT = Path(__file__).resolve().parent.parent
OFFICIAL = "━━━━━━━━━━━━━━━━\n🤖 Auto Posted by @AutoPosterHub"


class FooterTextTests(unittest.TestCase):
    def test_footer_is_exactly_the_official_text(self):
        self.assertEqual(branding.render_footer(), OFFICIAL)
        self.assertEqual(branding.FOOTER_SEPARATOR, "━━━━━━━━━━━━━━━━")
        self.assertEqual(branding.FOOTER_USERNAME, "@AutoPosterHub")
        self.assertEqual(composer.footer_block(), OFFICIAL)

    def test_the_old_placeholder_is_gone_from_code_docs_and_tests_data(self):
        for folder in ("app", "docs", "migrations"):
            for path in (ROOT / folder).rglob("*"):
                if path.is_file() and path.suffix in {".py", ".md", ".html", ".sql", ".txt", ".css", ".js"}:
                    self.assertNotIn("YourService", path.read_text(encoding="utf-8", errors="ignore"), str(path))
        self.assertNotIn("YourService", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_the_footer_is_plain_text_without_markup_that_could_break_composition(self):
        self.assertIsNone(re.search(r"[<>\[\]()`*_~|]", branding.render_footer()))   # nothing a markdown/HTML parser would act on
        self.assertTrue(branding.FOOTER_USERNAME.startswith("@"))
        self.assertRegex(branding.FOOTER_USERNAME, r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")   # a valid public Telegram username


class FooterOnceTests(unittest.TestCase):
    def test_appended_exactly_once_to_text_and_photo_posts(self):
        for has_image in (False, True):
            for body in ("Hello", "", "Line 1\n\nLine 2", "emoji 🎉 text"):
                text = composer.compose(body, has_image=has_image).text
                self.assertEqual(text.count("@AutoPosterHub"), 1)
                self.assertEqual(text.count("Auto Posted by"), 1)
                self.assertTrue(text.endswith(OFFICIAL))

    def test_user_pasted_or_repeated_footers_never_double_it(self):
        pasted = f"Hi\n\n{OFFICIAL}\n\n{OFFICIAL}"
        for body in (pasted, f"Hi\n{OFFICIAL.upper()}", "Hi\n🤖  Auto  Posted  by  @autoposterhub", "Hi\n━━━━━━━━━━━━━━━━"):
            text = composer.compose(body, has_image=False).text
            self.assertEqual(text.count("@AutoPosterHub"), 1, body)
            self.assertEqual(text.count("━━━━━━━━━━━━━━━━"), 1, body)

    def test_recomposing_a_composed_message_is_stable(self):
        once = composer.compose("Hello", has_image=False)
        twice = composer.compose(once.text, has_image=False)
        self.assertEqual(once.text, twice.text)             # retries / edit + reschedule cannot stack footers


class FooterLimitTests(unittest.TestCase):
    def test_footer_is_counted_in_the_text_limit(self):
        room = composer.max_body_units(False)
        self.assertEqual(room, TELEGRAM_MAX_TEXT_UNITS - composer.utf16_units(OFFICIAL) - composer.utf16_units(composer.SEPARATOR_FROM_BODY))
        fits = composer.compose("a" * room, has_image=False)
        composer.check(fits)
        self.assertEqual(fits.units, TELEGRAM_MAX_TEXT_UNITS)
        with self.assertRaises(composer.MessageError):
            composer.check(composer.compose("a" * (room + 1), has_image=False))

    def test_footer_is_counted_in_the_caption_limit(self):
        room = composer.max_body_units(True)
        fits = composer.compose("a" * room, has_image=True)
        composer.check(fits)
        self.assertEqual(fits.units, TELEGRAM_MAX_CAPTION_UNITS)
        with self.assertRaises(composer.MessageError) as ctx:
            composer.check(composer.compose("a" * (room + 1), has_image=True))
        self.assertIn("including the automatic footer", ctx.exception.message)

    def test_units_are_utf16_so_emoji_count_twice(self):
        room = composer.max_body_units(True)
        fits = composer.compose("🎉" * (room // 2), has_image=True)
        composer.check(fits)
        with self.assertRaises(composer.MessageError):
            composer.check(composer.compose("🎉" * (room // 2 + 1), has_image=True))


if __name__ == "__main__":
    unittest.main()
