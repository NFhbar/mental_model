import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent import MentalModel
from tools import prepare_repo


def source_key(item):
    return f"{item['source_type']}:{item['source_id']}"


def source_matches(expected, actual):
    expected_type, expected_id = expected.split(":", 1)
    actual_type, actual_id = actual.split(":", 1)
    if expected_type != actual_type:
        return False
    if expected_type == "commit":
        return expected_id.startswith(actual_id) or actual_id.startswith(expected_id)
    return expected_id == actual_id


async def evaluate_case(repo, case):
    agent = MentalModel(repo)
    answer = None
    retries = 0
    tool_calls = 0
    async for event in agent.ask(case["question"]):
        if event["kind"] == "tool_call":
            tool_calls += 1
        elif event["kind"] == "verification_failed":
            retries += 1
        elif event["kind"] == "answer":
            answer = event
    if answer is None:
        return {
            "id": case["id"],
            "passed": False,
            "error": "agent produced no answer",
        }
    actual_sources = [source_key(item) for item in answer["evidence"]]
    expectations = []
    for expected_group in case["expected_any"]:
        matches = [
            actual
            for actual in actual_sources
            if any(source_matches(expected, actual) for expected in expected_group)
        ]
        expectations.append(
            {
                "expected_any": expected_group,
                "matched": sorted(set(matches)),
                "passed": bool(matches),
            }
        )
    return {
        "id": case["id"],
        "question": case["question"],
        "passed": answer["verified"] and all(item["passed"] for item in expectations),
        "verified": answer["verified"],
        "expectations": expectations,
        "actual_sources": sorted(set(actual_sources)),
        "tool_calls": tool_calls,
        "verification_retries": retries,
    }


async def run(args):
    with open(args.cases) as file:
        cases = json.load(file)
    if args.limit:
        cases = cases[: args.limit]
    repo = prepare_repo(args.repo)
    try:
        results = []
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
            try:
                result = await evaluate_case(repo, case)
            except Exception as exc:
                result = {
                    "id": case["id"],
                    "passed": False,
                    "error": str(exc),
                }
            results.append(result)
            status = "PASS" if result["passed"] else "FAIL"
            print(
                f"  {status} · {result.get('tool_calls', 0)} tools · "
                f"{result.get('verification_retries', 0)} retries",
                flush=True,
            )
        summary = {
            "passed": sum(result["passed"] for result in results),
            "total": len(results),
            "results": results,
        }
        if args.output:
            Path(args.output).write_text(json.dumps(summary, indent=2) + "\n")
        print(f"\n{summary['passed']}/{summary['total']} evals passed")
        return 0 if summary["passed"] == summary["total"] else 1
    finally:
        repo.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--cases",
        default=str(Path(__file__).with_name("evals") / "click.json"),
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
