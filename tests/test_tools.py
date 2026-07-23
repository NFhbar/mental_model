import os
import subprocess
import tempfile
import unittest

from tools import GitRepo, GitToolError, canonical_repo_source


class GitToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = self.temp_dir.name
        subprocess.run(["git", "init", "-q", root], check=True)
        os.mkdir(os.path.join(root, "src"))
        with open(os.path.join(root, "README.md"), "w") as file:
            file.write("A repository archaeology agent.\n")
        with open(os.path.join(root, "src", "engine.py"), "w") as file:
            file.write("def investigate_repository():\n    return 'evidence'\n")
        with open(os.path.join(root, "src", "large.txt"), "w") as file:
            file.write("SEARCHABLE_" + ("x" * 9000) + "\n")
        subprocess.run(["git", "-C", root, "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                root,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                "Initial implementation",
            ],
            check=True,
        )
        self.repo = GitRepo(root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_search_code_finds_content_at_ref(self):
        result = self.repo.search_code("investigate_repository", ref="HEAD")
        self.assertIn("src/engine.py:1:def investigate_repository():", result)

    def test_search_code_reports_no_matches(self):
        result = self.repo.search_code("does_not_exist", ref="HEAD")
        self.assertEqual(result, "no code matched")

    def test_search_code_caps_total_output(self):
        result = self.repo.search_code("SEARCHABLE_", ref="HEAD")
        self.assertLess(len(result), 8200)
        self.assertIn("[truncated", result)

    def test_prompt_context_contains_repository_profile(self):
        context = self.repo.prompt_context()
        self.assertIn("commits: 1", context)
        self.assertIn("README.md", context)
        self.assertIn("src", context)

    def test_cleanup_removes_temporary_repository(self):
        root = self.repo.root
        self.repo.temporary = True
        self.repo.cleanup()
        self.assertFalse(os.path.exists(root))

    def test_canonical_repo_source_accepts_only_https_github(self):
        self.assertEqual(
            canonical_repo_source("https://github.com/pallets/click"),
            "https://github.com/pallets/click.git",
        )
        for source in (
            "http://github.com/pallets/click",
            "https://example.com/pallets/click",
            "https://user:secret@github.com/pallets/click",
            "https://github.com/pallets/click?ref=main",
            "git@github.com:pallets/click.git",
        ):
            with self.subTest(source=source):
                with self.assertRaises(GitToolError):
                    canonical_repo_source(source)

    def test_local_repositories_can_be_disabled(self):
        with self.assertRaises(GitToolError):
            canonical_repo_source(self.temp_dir.name, allow_local_repos=False)


if __name__ == "__main__":
    unittest.main()
