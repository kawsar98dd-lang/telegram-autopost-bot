import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "STEP4_GROUPS.md").read_text(encoding="utf-8")


class Step4DocumentationTests(unittest.TestCase):
    def test_covers_the_required_topics(self):
        for needle in ("How groups are discovered", "How posting permission is determined", "Selecting groups",
                       "Refresh / resync", "Security and privacy", "Known limitations", "Manual verification on the Render",
                       "FloodWait", "Cannot post", "Restricted", "Unavailable", "never tries to bypass", "composite foreign key",
                       "No groups loaded yet", "not part of this step", "never send codes"):
            self.assertIn(needle, DOC, needle)

    def test_documents_only_what_exists(self):
        self.assertNotRegex(DOC, r"(?i)\b(schedule[sd]? posts? are|auto-?join|bot api is used)\b")
        for forbidden in ("Bot API is used", "automatically joins"):
            self.assertNotIn(forbidden, DOC)
        self.assertEqual(re.findall(r"(?m)^(\d+)\. ", DOC.split("Manual verification")[1])[:10], [str(i) for i in range(1, 11)])

    def test_contains_no_secret_like_values_and_asks_for_none(self):
        self.assertNotRegex(DOC, r"[0-9a-f]{32}")
        self.assertNotRegex(DOC, r"gAAAA[A-Za-z0-9_-]{20,}")
        self.assertNotRegex(DOC, r"(?i)(send|paste|give|share) (it |them |this )?(to |with )?(me|the developer|claude)\b")

    def test_readme_links_to_it_and_the_placeholder_text_is_gone_from_the_code(self):
        self.assertIn("docs/STEP4_GROUPS.md", (ROOT / "README.md").read_text(encoding="utf-8"))
        for path in (ROOT / "app" / "web").rglob("*"):
            if path.is_file() and path.suffix in (".py", ".html"):
                text = path.read_text(encoding="utf-8")
                self.assertFalse("Choose the groups you are allowed to post in. Available in an upcoming release." in text, path.name)


if __name__ == "__main__":
    unittest.main()
