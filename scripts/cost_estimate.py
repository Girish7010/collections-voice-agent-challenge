"""Monthly AWS cost estimate for the two Fargate configurations (docs/cost-analysis.md).

Prices: AWS Price List API, ap-south-1, Linux/x86 on-demand, fetched 2026-10-21:
  Fargate  AmazonECS offer (published 2026-10-19)  vCPU-hr 0.04256, GB-hr 0.004655
  ALB      AWSELB offer    (published 2026-09-11)  0.0239/hr + 0.008/LCU-hr
  NAT GW   AmazonEC2 offer (effective 2026-10-01)  0.056/hr + 0.056/GB processed
  IPv4     AmazonVPC offer (published 2026-10-16)  0.005/hr per public address
Re-check before relying on them:
  curl -s https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/current/ap-south-1/index.json

Usage:  python scripts/cost_estimate.py
"""

VCPU_HR = 0.04256
GB_HR = 0.004655
ALB_HR = 0.0239
ALB_LCU_HR = 0.008
NAT_HR = 0.056
NAT_GB = 0.056
IPV4_HR = 0.005

DAYS = 30
HOURS = 24 * DAYS
BASE_H, SPIKE_H = 20, 4        # Appendix C: 2 campaign windows x 2 h per day

CONFIGS = {
    "A: 1 vCPU / 2 GB, 1->5 tasks": dict(vcpu=1.0, gb=2.0, floor=1, spike_avg=3, peak=5),
    "B: 0.5 vCPU / 1 GB, 1->10 tasks": dict(vcpu=0.5, gb=1.0, floor=1, spike_avg=6, peak=10),
}

# Fixed overhead assumptions (single NAT gateway, light traffic)
ALB_LCU_AVG = 0.5              # 50 calls/min is far below 1 LCU (25 new conns/s); rounded up
NAT_GB_MONTH = 20              # image pulls, logs, metrics; mock-only stack
FIXED = {
    "ALB (hourly)": ALB_HR * HOURS,
    "ALB (LCU, ~0.5 avg)": ALB_LCU_HR * ALB_LCU_AVG * HOURS,
    "NAT gateway (hourly, 1 AZ)": NAT_HR * HOURS,
    f"NAT data ({NAT_GB_MONTH} GB)": NAT_GB * NAT_GB_MONTH,
    "Public IPv4 x3 (NAT EIP + ALB in 2 AZs)": IPV4_HR * 3 * HOURS,
}


def task_hour(vcpu: float, gb: float) -> float:
    return vcpu * VCPU_HR + gb * GB_HR


def compute_month(c: dict, floor: int | None = None, spike_tasks: float | None = None) -> float:
    floor = c["floor"] if floor is None else floor
    spike = c["spike_avg"] if spike_tasks is None else spike_tasks
    daily_task_hours = BASE_H * floor + SPIKE_H * max(spike, floor)
    return daily_task_hours * task_hour(c["vcpu"], c["gb"]) * DAYS


def main() -> None:
    fixed = sum(FIXED.values())
    print(f"{'config':<34} {'$/task-hr':>9} {'compute':>9} {'peak-all-4h':>11} {'HA floor=2':>10} {'total*':>8}")
    for name, c in CONFIGS.items():
        th = task_hour(c["vcpu"], c["gb"])
        typical = compute_month(c)
        worst = compute_month(c, spike_tasks=c["peak"])
        ha = compute_month(c, floor=2)
        print(f"{name:<34} {th:>9.4f} {typical:>9.2f} {worst:>11.2f} {ha:>10.2f} {typical + fixed:>8.2f}")
    print("\nfixed overhead per month:")
    for k, v in FIXED.items():
        print(f"  {k:<32} {v:>7.2f}")
    print(f"  {'total fixed':<32} {fixed:>7.2f}")
    print(f"\n* total = typical compute + fixed. {DAYS}-day month. "
          f"Excludes CloudWatch, ECR storage, data transfer out, GPU inference.")


if __name__ == "__main__":
    main()
