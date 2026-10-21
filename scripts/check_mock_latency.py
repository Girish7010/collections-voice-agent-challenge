"""Print latency percentiles of the mock's lognormal distribution vs Appendix B targets.

Usage:
    python scripts/check_mock_latency.py                     # default config
    python scripts/check_mock_latency.py --sigma 0.35        # the PDF's suggested sigma
    python scripts/check_mock_latency.py --samples 100000 --seed 1
"""

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mock_dependency.api import MockConfig, sample_latency_ms  # noqa: E402

TARGETS = {50: 300, 90: 700, 99: 1200}


def main() -> None:
    defaults = MockConfig()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mu", type=float, default=defaults.latency_mu)
    parser.add_argument("--sigma", type=float, default=defaults.latency_sigma)
    parser.add_argument("--samples", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    samples = sorted(sample_latency_ms(rng, args.mu, args.sigma) for _ in range(args.samples))

    print(f"mu={args.mu} sigma={args.sigma} samples={args.samples} seed={args.seed}")
    print(f"{'pct':>4} {'measured_ms':>12} {'target_ms':>10} {'diff':>7}")
    for p, target in TARGETS.items():
        value = samples[min(len(samples) - 1, int(p / 100 * len(samples)))]
        print(f"p{p:<3} {value:>12} {target:>10} {(value - target) / target:>+7.1%}")


if __name__ == "__main__":
    main()
