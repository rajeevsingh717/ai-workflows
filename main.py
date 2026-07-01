"""CLI entry point: deep research on any topic via LangGraph."""
import argparse
import sys

from dotenv import load_dotenv

from research_graph import build_graph


def run(topic: str, max_iterations: int = 2) -> str:
    graph = build_graph()
    initial = {
        "topic": topic,
        "sub_questions": [],
        "web_results": [],
        "reddit_results": [],
        "notes": [],
        "iterations": 0,
        "max_iterations": max_iterations,
        "reflection": "",
        "final_report": "",
    }

    print(f"\nResearching: {topic}")
    print(f"max iterations: {max_iterations}\n")

    final_state = None
    for state in graph.stream(initial, {"recursion_limit": 50}, stream_mode="values"):
        final_state = state
        # cheap progress hint based on which keys just filled in
        if state.get("sub_questions") and not state.get("web_results"):
            print(f"[plan] {len(state['sub_questions'])} sub-questions")
        elif state.get("notes") and len(state["notes"]) > 0:
            print(f"[analyze] iteration {state.get('iterations', 0)}, "
                  f"{len(state.get('web_results', []))} web / "
                  f"{len(state.get('reddit_results', []))} reddit sources")

    return final_state["final_report"] if final_state else ""


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Deep internet research with LangGraph.")
    parser.add_argument("topic", nargs="+", help="Topic to research")
    parser.add_argument("--max-iterations", type=int, default=2,
                        help="Max plan→search→analyze→reflect loops (default: 2)")
    parser.add_argument("--out", type=str, default=None,
                        help="Optional path to write the final markdown report")
    args = parser.parse_args()

    topic = " ".join(args.topic)
    report = run(topic, max_iterations=args.max_iterations)

    print("\n" + "=" * 80)
    print("FINAL REPORT")
    print("=" * 80 + "\n")
    print(report)

    if args.out:
        with open(args.out, "w") as f:
            f.write(report)
        print(f"\n💾 Saved to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
