# Terraform: ECS Fargate stack (ap-south-1)

Infrastructure for the Round 2 plan in [`docs/architecture.md`](../../docs/architecture.md),
sized per **configuration B** in [`docs/cost-analysis.md`](../../docs/cost-analysis.md)
(0.5 vCPU / 1 GB, max 10 tasks).

**Nothing here has been applied.** The configuration passes `terraform validate`, but no
`terraform plan` has been run, because that needs credentials. Review the plan yourself
before the first apply.

## Storage

The dead-letter queue and the idempotency ledger live in **DynamoDB**, shared by every
task, which is what makes `desired_count = 2` safe. `app/stores.py` defines the contract;
`app/dynamo_dlq.py` and `app/dynamo_idempotency.py` implement it with conditional writes
so claims stay atomic across tasks. `tests/unit/test_store_parity.py` runs the same
assertions against both the SQLite and DynamoDB backends.

`docs/architecture.md` originally proposed SQS for the dead-letter queue. That cannot
work: SQS has no lookup by key, no filter or count by status and no in-place update, so
it cannot serve `GET /dlq`, `GET /dlq/{event_id}` or the claim/resolve lifecycle. Both
stores are therefore DynamoDB tables. SQS remains available as an optional replay
trigger (`enable_replay_queue`), off by default because nothing consumes it yet.

`store_backend = "sqlite"` is still selectable for a deliberate single-task deployment.
`guard.tf` refuses `desired_count > 1` in that case, because the SQLite file lives on the
task's own ephemeral disk: two tasks would double-process events, split the dead-letter
queue, and lose both on the next deployment.

## What it creates

| Area | Resources |
|---|---|
| Network | VPC (`10.40.0.0/16`), 2 public + 2 private subnets across 2 AZs, IGW, 1 NAT gateway, free S3 + DynamoDB gateway endpoints |
| Compute | ECS cluster (Container Insights on), Fargate task definition, service with deployment circuit breaker and rollback |
| Ingress | Internet-facing ALB, target group on 8000 health-checking `/health` |
| Scaling | Application Auto Scaling, target tracking on `ALBRequestCountPerTarget` |
| Registry | ECR repository, scan on push, lifecycle policy |
| State | DynamoDB dead-letters table (GSI `status-seq-index`) and ledger table, both with PITR; ledger has TTL |
| Optional | SQS replay queue + redrive queue, only when `enable_replay_queue = true` |
| Observability | CloudWatch log group `/ecs/collections-agent`, 30-day retention |
| IAM | Task execution role (managed policy) and task role scoped to exactly those two tables and the one index |

43 resources with the defaults: 41 AWS resources plus two `terraform_data` guard
checks. 3 more if `enable_replay_queue` is set. (Counts instances, not blocks — the
subnets, route tables and associations each expand per AZ.)

## Deploying

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: allowed_account_id and dependency_url are required.
```

Authenticate however you normally do — SSO, a named profile, or an assumed role.
**Do not paste long-lived access keys into a shell or a file.** If you must use a key,
use a fresh one and delete it afterwards.

```bash
export AWS_PROFILE=<your-profile>
terraform init
terraform plan -out=tf.plan     # read this before applying
```

The ECS service cannot stabilise until an image exists, so push one first:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-south-1
REPO="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/collections-agent"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

# The image is linux/amd64; the task definition pins X86_64. Build for that platform
# explicitly if you are on an ARM machine.
docker build --platform linux/amd64 -t "$REPO:v0.1.0" ../..
docker push "$REPO:v0.1.0"
```

ECR is created by this stack, so the first run is two passes: apply to create the
repository, push the image, then set `image_tag` and apply again.

```bash
terraform apply tf.plan
curl "$(terraform output -raw health_url)"
```

## Safety rails

- **Account guard** (`guard.tf`): a precondition compares `aws_caller_identity` against
  `allowed_account_id` and fails the plan on a mismatch, so an apply cannot land in the
  wrong account when several profiles are configured.
- **Remote state is commented out** in `versions.tf`. Local state is fine for a first
  `plan`; set up the S3 bucket and lock table before anyone else runs an apply.
- `.gitignore` here excludes `*.tfstate`, `terraform.tfvars` and `*.tfplan`. State holds
  resource attributes and can contain secrets.

## Known gaps

1. **HTTP only.** The listener is port 80. HTTPS needs an ACM certificate and a domain,
   which this stack cannot guess. Add it before carrying real call data.
2. **Scaling signal is a proxy.** `docs/cost-analysis.md` is explicit that scaling should
   track *calls in progress*, not CPU or request count. The app exposes that at
   `/metrics`, but nothing publishes it to CloudWatch, so target tracking cannot read it.
   `ALBRequestCountPerTarget` is used instead — fine while call duration is stable, wrong
   when it is not. `target_value = 600` is a placeholder; calibrate it with `load_tests/`.
3. **Single NAT gateway.** ~$41/month and a single point of failure. Set
   `single_nat_gateway = false` for one per AZ, roughly doubling that line.
4. **No GPU inference service.** Out of scope by choice; see `docs/architecture.md` for
   the ECS-on-EC2 `g5.xlarge` design.
5. **The mock dependency is not deployed.** `dependency_url` has no default and must
   point at a real upstream. A mock that injects 20% 503s does not belong in a live
   account.
6. **Ledger TTL puts a horizon on idempotency.** A duplicate arriving more than
   `ledger_ttl_days` after the original is treated as new. Set it to 0 to keep records
   forever.
7. **`list()` and `counts()` read a global secondary index**, which cannot be strongly
   consistent, so they may briefly miss a very recent write. Correctness does not depend
   on them: `claim()` is a conditional write, so a replayer acting on a stale listing
   still cannot double-process a record.
