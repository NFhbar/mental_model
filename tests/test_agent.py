import os
import subprocess
import tempfile
import unittest

from agent import MentalModel, parse_evidence_answer, verify_evidence
from tools import GitRepo


class EvidenceVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = self.temp_dir.name
        subprocess.run(["git", "init", "-q", root], check=True)
        with open(os.path.join(root, "README.md"), "w") as file:
            file.write("Mental Model explains repository history with evidence.\n")
        subprocess.run(["git", "-C", root, "add", "README.md"], check=True)
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
                "Add README",
            ],
            check=True,
        )
        self.repo = GitRepo(root)
        output = self.repo.read_file_at_ref("README.md", start_line=1, end_line=1)
        self.sources = {"file:README.md@HEAD": [output]}

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_accepts_quote_retrieved_from_file(self):
        raw = """Mental Model explains repository history [e1].
<evidence>
[{"id":"e1","claim":"Mental Model explains repository history","kind":"direct","source_type":"file","source_id":"README.md@HEAD","quote":"Mental Model explains repository history with evidence."}]
</evidence>"""
        answer, evidence, parse_problems = parse_evidence_answer(raw)
        problems, report = verify_evidence(
            self.repo, answer, evidence, self.sources, parse_problems
        )
        self.assertEqual(problems, [])
        self.assertTrue(report[0]["verified"])
        self.assertTrue(all(check["ok"] for check in report[0]["checks"]))
        self.assertEqual(
            report[0]["quote"],
            "Mental Model explains repository history with evidence.",
        )

    def test_rejects_quote_absent_from_source(self):
        raw = """Mental Model performs semantic verification [e1].
<evidence>
[{"id":"e1","claim":"Mental Model performs semantic verification","kind":"direct","source_type":"file","source_id":"README.md@HEAD","quote":"This sentence is fabricated."}]
</evidence>"""
        answer, evidence, parse_problems = parse_evidence_answer(raw)
        problems, report = verify_evidence(
            self.repo, answer, evidence, self.sources, parse_problems
        )
        self.assertIn(
            "e1 quote does not occur in file:README.md@HEAD", problems
        )
        self.assertFalse(report[0]["verified"])
        self.assertFalse(
            next(
                check["ok"]
                for check in report[0]["checks"]
                if check["name"] == "exact quote matched"
            )
        )

    def test_rejects_missing_ledger_record(self):
        answer, evidence, parse_problems = parse_evidence_answer(
            "An unsupported claim [e1]."
        )
        problems, report = verify_evidence(
            self.repo, answer, evidence, self.sources, parse_problems
        )
        self.assertIn("the answer has no <evidence> ledger", problems)
        self.assertIn("inline citation [e1] has no ledger record", problems)
        self.assertEqual(report, [])

    def test_agent_state_can_be_restored_after_cancellation(self):
        agent = MentalModel.__new__(MentalModel)
        agent.input_items = [{"role": "user", "content": "prior question"}]
        agent.seen_sources = {"file:README.md@HEAD": ["evidence"]}
        agent.runtime = {"questions": 1}
        agent.last_investigation = {"verified": True}
        agent.last_verification_report = [{"id": "e1"}]
        snapshot = agent.snapshot_state()
        agent.input_items.append({"role": "user", "content": "cancelled"})
        agent.seen_sources.clear()
        agent.runtime["questions"] = 2
        agent.last_investigation = None
        agent.last_verification_report = []
        agent.restore_state(snapshot)
        self.assertEqual(
            agent.input_items,
            [{"role": "user", "content": "prior question"}],
        )
        self.assertEqual(
            agent.seen_sources,
            {"file:README.md@HEAD": ["evidence"]},
        )
        self.assertEqual(agent.runtime, {"questions": 1})
        self.assertEqual(agent.last_investigation, {"verified": True})
        self.assertEqual(agent.last_verification_report, [{"id": "e1"}])


if __name__ == "__main__":
    unittest.main()
