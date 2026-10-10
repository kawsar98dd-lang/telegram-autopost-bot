import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "LICENSING.md").read_text(encoding="utf-8")
SELLER = (ROOT / "docs" / "SELLER_GUIDE.md").read_text(encoding="utf-8")
INSTALL = (ROOT / "docs" / "INSTALLATION_GUIDE.md").read_text(encoding="utf-8")


class LicensingDocsTests(unittest.TestCase):
    def test_customer_document_covers_installing_replacing_and_limits(self):
        for needle in ("LICENSE_FILE_CONTENT", "LICENSE_FILE", "Install your license", "Replace or renew", "expired", "Honest limits",
                       "machine fingerprint", "unbreakable", "LICENSE_ENFORCEMENT=false", "APP_ENV=production", "chmod 644"):
            self.assertIn(needle, DOC, needle)
        for seller_only in ("keygen.py", "issue_license.py", "private key"):
            self.assertNotIn(seller_only, DOC.replace("signed by the seller", ""), seller_only)    # no seller procedures for customers

    def test_seller_guide_covers_keys_issuing_packaging_backup_and_limits(self):
        for needle in ("keygen.py", "issue_license.py", "build_customer_zip.py", "--perpetual", "--expires", "Back it up", "outside the project",
                       "Release checklist", "Seller only", "cannot do", "Leaked key", "Lost key", "NEVER in the customer ZIP"):
            self.assertIn(needle, SELLER, needle)

    def test_installation_guide_covers_the_beginner_topics(self):
        for needle in ("docker compose up -d --build", "generate_keys.py", "SETUP_TOKEN", "DATABASE_URL", "python -m app.db.migrate", "pg_dump",
                       "SESSION_ENCRYPTION_KEY", "Troubleshooting", "FloodWait", "Uncertain", "duplicate", "ban", "worker", "sha256sum",
                       "Backups", "Known limitations", "MAX_POSTS_PER_HOUR"):
            self.assertIn(needle, INSTALL, needle)

    def test_env_example_and_readme_mention_the_license_settings(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("LICENSE_FILE_CONTENT", env)
        self.assertRegex(env, r"(?m)^#\s*LICENSE_FILE=")
        self.assertNotRegex(env, r"(?m)^LICENSE_FILE")                       # nothing active by default
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/LICENSING.md", readme)
        self.assertIn("docs/INSTALLATION_GUIDE.md", readme)
        self.assertNotIn("STEP3_REAL", readme)                                # README must not link seller-only guides

    def test_gitignore_blocks_keys_and_issued_licenses(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("license_private*.key", "*.license.json", "license.json", "licenses/"):
            self.assertIn(pattern, text)


if __name__ == "__main__":
    unittest.main()
