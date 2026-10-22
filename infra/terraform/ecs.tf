resource "aws_ecs_cluster" "main" {
  name = var.project

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 100
    base              = 0
  }
}

locals {
  # APP_* variables consumed by app/config.py Settings.from_env.
  storage_env = var.store_backend == "dynamodb" ? {
    STORE_BACKEND   = "dynamodb"
    DLQ_TABLE       = aws_dynamodb_table.dead_letters.name
    LEDGER_TABLE    = aws_dynamodb_table.ledger.name
    AWS_REGION      = data.aws_region.current.name
    LEDGER_TTL_DAYS = var.ledger_ttl_days
    DB_THREADS      = var.db_threads
    } : {
    STORE_BACKEND = "sqlite"
    # The task filesystem is ephemeral. /srv/data exists in the image and is owned by
    # the unprivileged appuser (see the Dockerfile). Everything written here is LOST
    # when the task stops, which is why guard.tf pins this backend to one task.
    DB_PATH    = "/srv/data/collections.db"
    DB_THREADS = 1
  }

  app_env = merge(
    {
      DEPENDENCY_URL = var.dependency_url
      LOG_LEVEL      = "INFO"
    },
    local.storage_env,
    var.app_settings,
  )

  container_env = [for k, v in local.app_env : { name = "APP_${k}", value = tostring(v) }]
}

resource "aws_ecs_task_definition" "app" {
  family                   = var.project
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # the image is built FROM python:3.12-slim
  }

  container_definitions = jsonencode([
    {
      name      = "app"
      image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      essential = true

      portMappings = [
        {
          containerPort = 8000
          protocol      = "tcp"
        }
      ]

      environment = local.container_env

      # Mirrors the HEALTHCHECK in the Dockerfile. The ALB check decides whether
      # traffic is routed; this one lets ECS replace a wedged container.
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)\" || exit 1"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.tasks.name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "app"
        }
      }

      readonlyRootFilesystem = false # SQLite writes to /srv/data
    }
  ])
}

resource "aws_ecs_service" "app" {
  name            = var.project
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # Give the task time to boot before health checks count against it.
  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false # egress via NAT
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8000
  }

  # Roll back automatically instead of leaving a half-failed deployment up.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  lifecycle {
    # desired_count is owned by Application Auto Scaling once the stack is up,
    # so Terraform should not fight it on every plan.
    ignore_changes = [desired_count]
  }

  depends_on = [
    aws_lb_listener.http,
    aws_iam_role_policy_attachment.execution_managed,
  ]
}
