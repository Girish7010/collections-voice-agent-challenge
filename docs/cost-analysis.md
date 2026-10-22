# Cost analysis: Fargate configuration A vs B (ap-south-1)

**These are estimates, not a measured bill.** Prices come from the AWS Price List API for
ap-south-1 (Linux/x86, on-demand), fetched 2026-10-21: Fargate $0.04256/vCPU-hr and
$0.004655/GB-hr; ALB $0.0239/hr + $0.008/LCU-hr; NAT gateway $0.056/hr + $0.056/GB;
public IPv4 $0.005/hr. To reproduce, run `python scripts/cost_estimate.py`. The script
documents every price and assumption. Month = 30 days. Traffic follows Appendix C:
20 baseline hours plus 4 campaign hours per day. Task counts during the spike follow the
challenge's worked example (average 3 of 5 for A, 6 of 10 for B).

| Per month (USD) | A: 1 vCPU / 2 GB, 1→5 | B: 0.5 vCPU / 1 GB, 1→10 |
|---|---:|---:|
| Price per task-hour | 0.0519 | 0.0259 |
| Compute, typical (floor 1, spike average) | **49.80** | **34.23** |
| Compute, worst case (every spike hour at max tasks) | 62.24 | 46.68 |
| Compute with a 2-task high-availability floor | 80.92 | 49.80 |
| Fixed: ALB 20.09 + NAT 41.44 + 3 public IPv4 10.80 | 72.33 | 72.33 |
| **Total (typical compute + fixed)** | **≈ 122** | **≈ 107** |

These figures sit just below the PDF's worked example ($54 compute for A), because the
current Mumbai rates are slightly lower than its rounded $0.046 and $0.0051.

**Reasoning.**
- **Fixed overhead dominates.** The ALB, the NAT gateway and their public IPs cost about
  $72/month whether there are 10 calls or 0. That is more than the compute itself.
  Running one NAT per AZ for high availability adds another $44 or so. Choosing A or B
  moves the total by only ~13%, so the bigger savings lever is the network design.
  For example, add a **free S3 gateway endpoint** so ECR image layers don't go through
  the NAT. Interface endpoints cost $0.01/hr per AZ each, so four of them across two AZs
  (~$58) would cost more than the NAT they replace at this traffic level.
- **Baseline floor.** For 20 of every 24 hours the service runs at its minimum. B's
  minimum costs half of A's. This gap widens with a production floor of 2 tasks (one per
  AZ, which you need anyway to survive an AZ outage): 2 × 0.5 vCPU costs the same as a
  single A task, so B gets multi-AZ redundancy for the price of A's single point of
  failure.
- **Scale-out granularity.** B adds capacity in $0.026/hr steps instead of $0.052/hr.
  During the 15-minute ramp it tracks demand more closely and wastes less idle capacity
  at each step. The trade-off is more tasks to start during a ramp (each Fargate task
  takes about 30–60 s to start). The scaling policy therefore has to react to a leading
  signal (calls in progress), not CPU.
- **What actually sizes the tasks.** This service mostly waits on the speech/LLM pipeline;
  it does little computing itself. Little's law (calls in progress = arrival rate × time
  each call lasts) gives about 50 calls/min × ~80 s average call ≈ 67 calls in progress
  at peak, and about 13 at baseline. Either task size can handle that, so the spike
  task counts above are the PDF's planning assumption. Round 2 should replace them with
  the measured number of calls in progress per task. GPU model hosting is a separate cost
  and is **not** included here (Fargate does not offer GPUs).

**Recommendation: configuration B** (0.5 vCPU / 1 GB, minimum 2 tasks across 2 AZs, maximum
10, scaling on calls in progress). It has a lower and redundant floor, finer scaling, and
about the same peak cost. Choose A only if a single task needs more than 1 GB of memory
(for example, in-process model inference) or if task start-up time during ramps proves
to be the bottleneck.
