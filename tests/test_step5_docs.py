import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "STEP5_POSTS.md").read_text(encoding="utf-8")


class Step5DocumentationTests(unittest.TestCase):
    def test_covers_the_required_topics(self):
        for needle in ("Post lifecycle", "Mandatory footer", "Storage abstraction", "Target groups", "Security model", "Tests",
                       "Known limitations", "Manual verification", "app/branding.py", "composer.py", "no download from URLs",
                       "cannot make part of a message immutable", "Step 6", "0005_posts", "MEDIA_STORAGE"):
            self.assertIn(needle, DOC, needle)

    def test_manual_checklist_is_numbered_1_to_10(self):
        self.assertEqual(re.findall(r"(?m)^(\d+)\. ", DOC.split("Manual verification")[1])[:10], [str(i) for i in range(1, 11)])

    def test_no_secret_like_values_and_asks_for_none(self):
        self.assertNotRegex(DOC, r"[0-9a-f]{32}")
        self.assertNotRegex(DOC, r"(?i)(send|paste|give|share) (it |them |this )?(to |with )?(me|the developer|claude)\b")

    def test_readme_links_to_it(self):
        self.assertIn("docs/STEP5_POSTS.md", (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
