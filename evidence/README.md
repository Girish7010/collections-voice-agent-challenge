# Evidence

Recorded artifacts backing the claims in [`docs/results.md`](../docs/results.md).
Everything here was produced by an actual run — nothing is reconstructed or edited
for presentation.

`results/` (the raw run directory) stays gitignored because it contains SQLite
databases and verbose logs. This directory holds the curated, non-sensitive subset
that a reviewer needs.

```
evidence/
├── round1/
│   ├── test-run.txt                 full pytest output: 222 passed
│   ├── test-run-header.txt          interpreter, platform, exact command
│   └── load-tests/
│       ├── full-day-24h/            24 sim hours @ 240x, no outage
│       │   ├── report.json          machine-readable metrics
│       │   └── console.txt          live ramp trace, sim-clock vs calls sent
│       └── campaign-window-with-outage/
│           ├── report.json          includes circuit transitions and replay results
│           └── console.txt
└── aws-deployment/
    ├── aws-resources.txt            ECS, ALB targets, autoscaling, DynamoDB schema
    ├── live-service.txt             /health and /metrics through the ALB
    ├── terraform-outputs.json       applied stack outputs
    └── terraform-state-list.txt     every resource Terraform managed
```

## On the AWS evidence

Captured 2026-10-22 while the stack was live in ap-south-1, **before teardown**. The
12-digit AWS account ID is redacted to `1140XXXXXXXX` throughout; this repository is
published publicly under Apache 2.0.

Two honest qualifications, also stated in `live-service.txt` itself:

1. The deployed service ran with a **placeholder dependency URL**. `/health` and
   `/metrics` are genuine, but `POST /calls` exhausted its retries and dead-lettered.
   That is a real exercise of the resilience path on real infrastructure — it is **not**
   a working voice pipeline.
2. **No GPU and no open-weight model were ever deployed.** Round 2 is finalists-only
   and eligibility was not confirmed, so the GPU track was not attempted.

The infrastructure was destroyed after this evidence was captured. Teardown
confirmation is in [`teardown.txt`](aws-deployment/teardown.txt) once that step runs.
