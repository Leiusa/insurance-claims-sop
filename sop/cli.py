"""Play preset conversations against the real model and print what happened.

    uv run python -m sop.cli canonical                 # one scenario
    uv run python -m sop.cli frustrated january        # several
    uv run python -m sop.cli all --events              # every scenario, with the audit trail
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .config import ROOT, load_settings
from .insurance.agent import InsuranceAgent
from .insurance.data import FixtureRepo
from .llm.client import build_client


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", nargs="+", help="scenario ids from scenarios/presets.json, or 'all'")
    parser.add_argument("--events", action="store_true", help="print the harness audit events for each turn")
    args = parser.parse_args()

    settings = load_settings()
    llm = build_client(settings)
    if llm is None:
        sys.exit("No model configured: set LLM_API_KEY (or OPENAI_API_KEY / ANTHROPIC_API_KEY).")
    agent = InsuranceAgent(FixtureRepo(settings.fixtures_dir), settings, llm)
    presets = json.loads((ROOT / "scenarios" / "presets.json").read_text())
    chosen = [p for p in presets if "all" in args.scenario or p["id"] in args.scenario]
    if not chosen:
        sys.exit(f"Unknown scenario. Choose from: {', '.join(p['id'] for p in presets)}")

    print(f"model: {llm.provider}/{llm.model} (nlu: {llm.nlu_model}), as of {settings.today()}")
    for preset in chosen:
        session = agent.new_session(consent_scenario=preset.get("consent_scenario"))
        print(f"\n=== {preset['title']} ===\nAGENT: {session.transcript[0]['content']}")
        for text in preset["messages"]:
            seen = len(session.events)
            started = time.perf_counter()
            reply = agent.handle(session, text)
            elapsed = time.perf_counter() - started
            print(f"\nCALLER: {text}")
            if args.events:
                for event in session.events[seen:]:
                    print(f"   · [{event.phase}] {event.kind}: {event.detail}")
            print(f"AGENT [{session.phase}, {elapsed:.1f}s]: {reply}")


if __name__ == "__main__":
    main()
