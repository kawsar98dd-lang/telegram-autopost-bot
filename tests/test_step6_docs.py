import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "STEP6_SCHEDULER.md").read_text(encoding="utf-8")


class Step6DocumentationTests(unittest.TestCase):
    def test_covers_the_required_topics(self):
        for needle in ("Architecture", "Job life cycle", "Duplicate prevention", "Residual risk", "FloodWait", "Retry and FloodWait policy",
                       "Deployment", "Background Worker", "0006_scheduler", "Manual verification", "Manual Telegram acceptance test",
                       "Known limitations", "NOT VERIFIED", "uncertain", "SKIP LOCKED", "Asia/Dhaka", "composer.compose()", "Bot API"):
            self.assertIn(needle, DOC, needle)

    def test_manual_checklists_are_numbered(self):
        for title, count in (("Manual verification on Render", 7), ("Manual Telegram acceptance test", 6)):
            part = DOC.split(title)[1].split("\n## ")[0]
            self.assertEqual(re.findall(r"(?m)^(\d+)\. ", part), [str(i) for i in range(1, count + 1)], title)

    def test_no_secret_like_values_and_asks_for_none(self):
        self.assertNotRegex(DOC, r"[0-9a-f]{32}")
        self.assertNotRegex(DOC, r"(?i)(send|paste|give|share) (it |them |this )?(to |with )?(me|the developer|claude)\b")

    def test_readme_and_bengali_guide_exist(self):
        self.assertIn("docs/STEP6_SCHEDULER.md", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "docs" / "STEP6_GITHUB_ANDROID_GUIDE_BN.md").is_file())

    def test_no_new_environment_variable_is_undocumented(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        for name in ("WORKER_POLL_SECONDS", "POST_MIN_INTERVAL_SECONDS", "MAX_POSTS_PER_HOUR"):
            self.assertIn(name, env)


if __name__ == "__main__":
    unittest.main()
