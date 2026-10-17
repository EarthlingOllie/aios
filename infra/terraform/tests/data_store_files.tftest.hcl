# Applies the whole configuration to a mocked AWS (no account or credentials needed), and
# checks the uploaded files' bucket, who may reach it and how. Run: terraform test

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  # The policy documents' JSON comes from the provider, so a mock has none to check; these
  # tests read the documents' statements instead.
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  # Arguments that must look like ARNs (the mocks' are random strings otherwise).
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/mock/1" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mock/1" }
  }
  mock_resource "aws_lb_listener" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/mock/1/1" }
  }
  mock_resource "aws_service_discovery_service" {
    defaults = { arn = "arn:aws:servicediscovery:us-east-1:123456789012:service/srv-mock" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/data-store-files" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::aios-data-prod" }
  }
}

override_resource {
  target = aws_iam_role.task
  values = { arn = "arn:aws:iam::123456789012:role/aios-ecs-task" }
}

override_resource {
  target = aws_iam_role.worker
  values = { arn = "arn:aws:iam::123456789012:role/aios-ecs-worker" }
}

run "data_store_files" {
  # Applied to the mocks (nothing real is made), so ids and ARNs are known.
  command = apply

  # The bucket the app's tasks are told to use.
  assert {
    condition     = aws_s3_bucket.data_store_files.bucket == "aios-data-prod"
    error_message = "The files bucket should be aios-data-<environment>."
  }
  assert {
    condition = contains(
      aws_ecs_task_definition.api.container_definitions == null ? [] : [
        for e in jsondecode(aws_ecs_task_definition.api.container_definitions)[0].environment :
        e.value if e.name == "DATA_STORE_FILES_BUCKET"
      ],
      "aios-data-prod",
    )
    error_message = "The API should be given the bucket's name."
  }

  # SSE-KMS, with the bucket's own key.
  assert {
    condition = one([
      for r in aws_s3_bucket_server_side_encryption_configuration.data_store_files.rule :
      one(r.apply_server_side_encryption_by_default).sse_algorithm
    ]) == "aws:kms"
    error_message = "The files bucket should be encrypted with SSE-KMS."
  }

  # Block Public Access, all four settings.
  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.data_store_files.block_public_acls,
      aws_s3_bucket_public_access_block.data_store_files.block_public_policy,
      aws_s3_bucket_public_access_block.data_store_files.ignore_public_acls,
      aws_s3_bucket_public_access_block.data_store_files.restrict_public_buckets,
    ])
    error_message = "The files bucket should block all public access."
  }

  # Versioning off: a delete must really delete (user deletion).
  assert {
    condition = one(
      aws_s3_bucket_versioning.data_store_files.versioning_configuration
    ).status == "Disabled"
    error_message = "The files bucket should keep no versions."
  }

  # The worker has a task role of its own, no longer the shared, empty one.
  assert {
    condition     = aws_ecs_task_definition.worker.task_role_arn != aws_ecs_task_definition.web.task_role_arn
    error_message = "The ingest worker should have a task role of its own."
  }

  # The API and the worker may get, put and delete objects, under stores/ only.
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.data_store_files_access.statement :
      alltrue([for r in s.resources : endswith(r, "/stores/*")])
      if length(setintersection(s.actions, ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"])) > 0
    ])
    error_message = "Object access should be limited to stores/*."
  }
  assert {
    condition = length(setsubtract(
      ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      flatten([for s in data.aws_iam_policy_document.data_store_files_access.statement : s.actions]),
    )) == 0
    error_message = "The API and worker should be able to get, put and delete their files."
  }
  assert {
    condition = (
      aws_iam_role_policy.api_data_store_files.role == aws_iam_role.api.id
      && aws_iam_role_policy.worker_data_store_files.role == aws_iam_role.worker.id
    )
    error_message = "The API's and the worker's roles should have the files policy."
  }

  # The app subnets reach S3 through the gateway endpoint.
  assert {
    condition     = contains(aws_vpc_endpoint.app_s3.route_table_ids, aws_route_table.private.id)
    error_message = "The app subnets should have a route to S3's gateway endpoint."
  }
  # ... without opening the sandboxes' endpoint to the files bucket.
  assert {
    condition     = !contains(aws_vpc_endpoint.s3.route_table_ids, aws_route_table.private.id)
    error_message = "The sandboxes' S3 endpoint should stay theirs alone."
  }
}
