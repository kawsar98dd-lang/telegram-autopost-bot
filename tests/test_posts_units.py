"""Step 5 unit tests: footer/composer, image validation, storage backends, multipart parser, configuration."""

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path

from app import branding
from app.config import ConfigError, settings_from_env
from app.posts import composer
from app.posts.limits import TELEGRAM_MAX_CAPTION_UNITS, TELEGRAM_MAX_TEXT_UNITS, max_image_bytes
from app.posts.media import MediaError, check_filename, validate_image
from app.posts.storage import DatabaseMediaStorage, LocalMediaStorage, StorageError, check_key, new_key
from app.web.multipart import MultipartError, parse_multipart
from tests.posts_support import jpeg, png
from tests.support import migrated_db
from tests.web_support import BASE_ENV

FOOTER_LINE = "🤖 Auto Posted by @YourService"
BIG = 10 * 1024 * 1024


class FooterTests(unittest.TestCase):
    def test_footer_is_appended_exactly_once(self):
        m = composer.compose("Hello everyone!", has_image=False)
        self.assertEqual(m.text.count(FOOTER_LINE), 1)
        self.assertTrue(m.text.startswith("Hello everyone!\n\n"))
        self.assertTrue(m.text.endswith(FOOTER_LINE))

    def test_the_footer_comes_from_branding_only(self):
        self.assertEqual(composer.footer_block(), branding.render_footer())
        old = branding.FOOTER_USERNAME
        try:
            branding.FOOTER_USERNAME = "@Renamed"
            self.assertTrue(composer.compose("x", has_image=False).text.endswith("@Renamed"))
        finally:
            branding.FOOTER_USERNAME = old

    def test_a_footer_typed_by_the_user_is_never_doubled(self):
        for body in (f"Hi\n\n{branding.render_footer()}", f"Hi\n{FOOTER_LINE}", f"{FOOTER_LINE}\nHi", f"Hi {FOOTER_LINE} there",
                     f"Hi\n\n{FOOTER_LINE}\n\n{FOOTER_LINE}", f"Hi\n{FOOTER_LINE.upper()}", f"Hi\n🤖  Auto   Posted by  @YourService"):
            with self.subTest(body=body):
                m = composer.compose(body, has_image=False)
                self.assertEqual(m.text.lower().count("auto posted by @yourservice"), 1)
                self.assertEqual(m.text.count(branding.FOOTER_SEPARATOR), 1)

    def test_nested_footer_cannot_rebuild_itself(self):
        half = FOOTER_LINE[:10]
        body = "A" + half + FOOTER_LINE + FOOTER_LINE[10:]
        self.assertEqual(composer.compose(body, has_image=False).text.count(FOOTER_LINE), 1)

    def test_stored_body_never_contains_the_footer(self):
        self.assertNotIn("YourService", composer.normalize_body(f"Hi\n\n{branding.render_footer()}"))

    def test_trailing_blank_lines_are_normalised(self):
        m = composer.compose("Hello\n\n\n\n\n   \n", has_image=False)
        self.assertEqual(m.text, f"Hello\n\n{branding.render_footer()}")

    def test_line_breaks_are_kept_and_crlf_is_normalised(self):
        self.assertEqual(composer.normalize_body("a\r\nb\rc\n\nd"), "a\nb\nc\n\nd")

    def test_control_and_bidi_characters_are_removed(self):
        self.assertEqual(composer.normalize_body("a\x00b\u202ec\x07d\te"), "abcd\te")

    def test_empty_body_still_gets_the_footer(self):
        self.assertEqual(composer.compose("", has_image=True).text, branding.render_footer())

    def test_input_is_capped(self):
        with self.assertRaises(composer.MessageError):
            composer.normalize_body("x" * 100_000)
        with self.assertRaises(composer.MessageError):
            composer.normalize_body(b"bytes")  # type: ignore[arg-type]


class LimitTests(unittest.TestCase):
    def test_limits_are_the_documented_telegram_values(self):
        self.assertEqual((TELEGRAM_MAX_TEXT_UNITS, TELEGRAM_MAX_CAPTION_UNITS), (4096, 1024))

    def test_the_footer_counts_against_the_limit(self):
        fits = "a" * composer.max_body_units(False)
        composer.check(composer.compose(fits, has_image=False))
        with self.assertRaises(composer.MessageError) as ctx:
            composer.check(composer.compose(fits + "a", has_image=False))
        self.assertEqual(ctx.exception.code, "too_long")

    def test_caption_limit_applies_with_an_image(self):
        body = "a" * (composer.max_body_units(True) + 1)
        composer.check(composer.compose(body, has_image=False))
        with self.assertRaises(composer.MessageError):
            composer.check(composer.compose(body, has_image=True))

    def test_length_is_counted_in_utf16_units(self):
        self.assertEqual(composer.utf16_units("a😀"), 3)
        body = "😀" * (composer.max_body_units(True) // 2 + 1)
        with self.assertRaises(composer.MessageError):
            composer.check(composer.compose(body, has_image=True))

    def test_image_size_limit_never_exceeds_what_telegram_accepts(self):
        self.assertEqual(max_image_bytes(200), BIG)
        self.assertEqual(max_image_bytes(2), 2 * 1024 * 1024)


class ImageValidationTests(unittest.TestCase):
    def test_valid_images(self):
        a = validate_image("photo.PNG", png(16, 9), BIG)
        b = validate_image("photo.jpeg", jpeg(30, 20), BIG)
        self.assertEqual((a.content_type, a.width, a.height), ("image/png", 16, 9))
        self.assertEqual((b.content_type, b.width, b.height), ("image/jpeg", 30, 20))
        self.assertEqual(len(a.sha256), 64)

    def test_content_decides_not_the_browser(self):
        for data in (b"GIF89a" + b"\x00" * 40, b"<?php echo 1; ?>", b"<svg onload=alert(1)>", b"MZ" + b"\x00" * 50, b"%PDF-1.4"):
            with self.subTest(data=data[:6]), self.assertRaises(MediaError) as ctx:
                validate_image("x.png", data, BIG)
            self.assertEqual(ctx.exception.code, "bad_content")

    def test_extension_must_match_content(self):
        with self.assertRaises(MediaError) as ctx:
            validate_image("x.jpg", png(), BIG)
        self.assertEqual(ctx.exception.code, "extension_mismatch")

    def test_unsafe_file_names_are_rejected(self):
        for name in ("../../etc/passwd.png", "a/b.png", "a\\b.png", "..png", ".hidden.png", "x\x00.png", "x\n.png", "", " a.png",
                     "a" * 300 + ".png", "C:\\x\\y.png", "a..b.png"):
            with self.subTest(name=name), self.assertRaises(MediaError) as ctx:
                check_filename(name)
            self.assertEqual(ctx.exception.code, "bad_name")

    def test_other_extensions_are_rejected(self):
        for name in ("a.gif", "a.svg", "a.php", "a.png.php", "a.html", "noextension", "a.webp", "a.exe"):
            with self.subTest(name=name), self.assertRaises(MediaError):
                check_filename(name)

    def test_empty_and_oversized_files(self):
        with self.assertRaises(MediaError) as e1:
            validate_image("a.png", b"", BIG)
        self.assertEqual(e1.exception.code, "empty")
        with self.assertRaises(MediaError) as e2:
            validate_image("a.png", png(), 50)
        self.assertEqual(e2.exception.code, "too_large")

    def test_structurally_broken_files(self):
        good = png()
        for data in (good[:-12], good[:20], good + b"junk", good[:16] + b"\xff\xff\xff\xff" + good[20:],
                     b"\x89PNG\r\n\x1a\n" + b"\x00" * 40):
            with self.subTest(len=len(data)), self.assertRaises(MediaError):
                validate_image("a.png", data, BIG)
        for data in (b"\xff\xd8\xff", jpeg()[:-2], jpeg()[:30] + b"\xff\xd9", b"\xff\xd8\xff\xe0\x00\x02\xff\xd9"):
            with self.subTest(len=len(data)), self.assertRaises(MediaError):
                validate_image("a.jpg", data, BIG)

    def test_dimensions_telegram_would_refuse(self):
        for w, h in ((10_000, 10), (5001, 5001), (1000, 20), (20, 1000)):
            with self.subTest(size=(w, h)), self.assertRaises(MediaError) as ctx:
                validate_image("a.png", png(w, h) if w * h < 300_000 else self._header_only_png(w, h), BIG)
            self.assertIn(ctx.exception.code, ("bad_dimensions", "bad_content"))

    @staticmethod
    def _header_only_png(w, h):
        import struct, zlib
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        c = lambda k, d: struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d) & 0xFFFFFFFF)  # noqa: E731
        return b"\x89PNG\r\n\x1a\n" + c(b"IHDR", ihdr) + c(b"IDAT", b"x") + c(b"IEND", b"")


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_backend_round_trip_and_purge(self):
        db = await migrated_db()
        t = [1000.0]
        st = DatabaseMediaStorage(db, lambda: t[0])
        k1, k2 = new_key(), new_key()
        await st.put(k1, b"one")
        await st.put(k2, b"two")
        self.assertEqual(await st.get(k1), b"one")
        with self.assertRaises(Exception):  # an existing key is never overwritten
            await st.put(k1, b"other")
        self.assertEqual(await st.get(k1), b"one")
        self.assertEqual(await st.purge_unreferenced({k1}, 2000), 1)
        self.assertIsNone(await st.get(k2))
        self.assertEqual(await st.purge_unreferenced(set(), 500), 0)  # too young
        await st.delete(k1)
        self.assertIsNone(await st.get(k1))

    async def test_keys_are_validated_everywhere(self):
        db = await migrated_db()
        for st in (DatabaseMediaStorage(db, lambda: 1.0), LocalMediaStorage(tempfile.mkdtemp())):
            for bad in ("../../etc/passwd", "a" * 31, "A" * 32, "../" + "a" * 29, "", "a/b" + "c" * 29, "g" * 32):
                with self.subTest(st=st.backend, key=bad):
                    with self.assertRaises(StorageError):
                        await st.get(bad)
                    with self.assertRaises(StorageError):
                        await st.put(bad, b"x")
                    with self.assertRaises(StorageError):
                        await st.delete(bad)

    async def test_local_backend_is_private_exclusive_and_inside_its_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = LocalMediaStorage(tmp)
            key = new_key()
            await st.put(key, b"bytes")
            path = Path(tmp).resolve() / key[:2] / key
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.suffix, "")
            with self.assertRaises(StorageError):  # arbitrary overwrite prevented
                await st.put(key, b"evil")
            self.assertEqual(await st.get(key), b"bytes")
            self.assertEqual(sorted(p.name for p in Path(tmp).rglob("*") if p.is_file()), [key])
            os.utime(path, (1, 1))
            self.assertEqual(await st.purge_unreferenced({key}, 10**10), 0)
            self.assertEqual(await st.purge_unreferenced(set(), 10**10), 1)
            self.assertIsNone(await st.get(key))


class MultipartTests(unittest.TestCase):
    CT = "multipart/form-data; boundary=XyZ"

    def body(self, *parts):
        return b"".join(b"--XyZ\r\n" + p + b"\r\n" for p in parts) + b"--XyZ--\r\n"

    def test_fields_and_files(self):
        body = self.body(b'Content-Disposition: form-data; name="a"\r\n\r\nline1\r\nline2',
                         b'Content-Disposition: form-data; name="image"; filename="x.png"\r\nContent-Type: image/png\r\n\r\n\x00\xff\r\n\x01',
                         b'Content-Disposition: form-data; name="a"\r\n\r\nsecond')
        fields, files = parse_multipart(self.CT, body)
        self.assertEqual(fields, {"a": "line1\r\nline2"})
        self.assertEqual(files["image"].data[:2], b"\x00\xff")

    def test_empty_file_input_means_no_file_and_empty_field_works(self):
        body = self.body(b'Content-Disposition: form-data; name="image"; filename=""\r\nContent-Type: application/octet-stream\r\n\r\n',
                         b'Content-Disposition: form-data; name="e"\r\n\r\n')
        fields, files = parse_multipart(self.CT, body)
        self.assertEqual((fields, files), ({"e": ""}, {}))

    def test_malformed_bodies_are_rejected(self):
        for ct, body in ((self.CT, b"garbage"), ("multipart/form-data", b"x"), ("multipart/form-data; boundary=", b"x"),
                         (self.CT, b"--XyZ\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nno end"),
                         (self.CT, self.body(b"X-Other: 1\r\n\r\nv")),
                         (self.CT, self.body(b'Content-Disposition: attachment; name="a"\r\n\r\nv')),
                         (self.CT, self.body(b'Content-Disposition: form-data\r\n\r\nv')),
                         (self.CT, self.body(b'Content-Disposition: form-data; name="a"\r\n\r\n' + b"x" * 70_000)),
                         ("multipart/form-data; boundary=" + "a" * 100, b"x")):
            with self.subTest(body=body[:30]), self.assertRaises(MultipartError):
                parse_multipart(ct, body)

    def test_field_and_file_count_limits(self):
        many = self.body(*[b'Content-Disposition: form-data; name="f%d"\r\n\r\nv' % i for i in range(500)])
        with self.assertRaises(MultipartError):
            parse_multipart(self.CT, many)
        files = self.body(*[b'Content-Disposition: form-data; name="f%d"; filename="a.png"\r\n\r\nv' % i for i in range(5)])
        with self.assertRaises(MultipartError):
            parse_multipart(self.CT, files)


class SettingsTests(unittest.TestCase):
    def test_media_storage_setting(self):
        self.assertEqual(settings_from_env(BASE_ENV).media_storage, "database")
        self.assertEqual(settings_from_env({**BASE_ENV, "MEDIA_STORAGE": "LOCAL"}).media_storage, "local")
        with self.assertRaises(ConfigError):
            settings_from_env({**BASE_ENV, "MEDIA_STORAGE": "s3"})

    def test_the_footer_is_not_configurable_through_the_environment(self):
        import re
        text = Path("app/config.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"FOOTER", text))
        s = settings_from_env({**BASE_ENV, "FOOTER_TEXT": "x", "FOOTER_USERNAME": "@evil"})
        self.assertNotIn("evil", repr(s))
        self.assertEqual(composer.compose("a", has_image=False).text.count("@YourService"), 1)

    def test_literal_footer_text_lives_only_in_branding_and_tests(self):
        offenders = [str(p) for p in Path("app").rglob("*") if p.is_file() and p.suffix in (".py", ".html", ".js")
                     and "Auto Posted by" in p.read_text(encoding="utf-8") and p.name != "branding.py"]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
