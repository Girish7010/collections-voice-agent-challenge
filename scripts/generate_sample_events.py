"""Write ~500 synthetic Appendix A call events (no real borrower data).

Timestamps are spread across the two campaign windows (Appendix C); ~20% of rows
are marked outcome=failed_downstream to mirror the injected failure rate.

Usage: python scripts/generate_sample_events.py [--count 500] [--seed 2026] [--out sample_data/call_events.jsonl]
"""

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from load_tests.traffic_profile import CAMPAIGN_WINDOWS, generate_event  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=500)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--out", default="sample_data/call_events.jsonl")
    a = p.parse_args()

    rng = random.Random(a.seed)
    windows = list(CAMPAIGN_WINDOWS.values())
    minutes = sorted(rng.uniform(*rng.choice(windows)) for _ in range(a.count))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for m in minutes:
            f.write(json.dumps(generate_event(rng, m, include_failures=True)) + "\n")
    print(f"wrote {a.count} events to {out}")


if __name__ == "__main__":
    main()
