# Uploaded files (aios.datastores.files): each document's at
# stores/<store id>/documents/<document id>. Only the API and the ingest worker reach them, and
# users download them through presigned links that last a minute.

resource "aws_kms_key" "data_store_files" {
  description         = "${var.name}: uploaded files (aios-data-${var.environment})"
  enable_key_rotation = true
}

resource "aws_kms_alias" "data_store_files" {
  name          = "alias/${var.name}-data-store-files"
  target_key_id = aws_kms_key.data_store_files.key_id
}

resource "aws_s3_bucket" "data_store_files" {
  bucket = "${var.name}-data-${var.environment}"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data_store_files" {
  bucket = aws_s3_bucket.data_store_files.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data_store_files.arn
    }
    # One KMS call per bucket key rather than per object.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "data_store_files" {
  bucket                  = aws_s3_bucket.data_store_files.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "data_store_files" {
  bucket = aws_s3_bucket.data_store_files.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# No versions: deleting a document (or, later, a user) must really delete its files.
resource "aws_s3_bucket_versioning" "data_store_files" {
  bucket = aws_s3_bucket.data_store_files.id
  versioning_configuration {
    status = "Disabled"
  }
}

# A failed multipart upload leaves parts behind, which are billed but never listed.
resource "aws_s3_bucket_lifecycle_configuration" "data_store_files" {
  bucket = aws_s3_bucket.data_store_files.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "data_store_files_tls" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.data_store_files.arn, "${aws_s3_bucket.data_store_files.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "data_store_files" {
  bucket = aws_s3_bucket.data_store_files.id
  policy = data.aws_iam_policy_document.data_store_files_tls.json
  # A bucket policy can't be put while Block Public Access is being set up.
  depends_on = [aws_s3_bucket_public_access_block.data_store_files]
}

# The API's and the ingest worker's: get, put and delete objects under stores/, nothing else in
# the bucket (no listing), and the key to encrypt and decrypt them.
data "aws_iam_policy_document" "data_store_files_access" {
  statement {
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      # A large upload goes up in parts; this cleans up after one that fails.
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.data_store_files.arn}/stores/*"]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.data_store_files.arn]
  }
}

resource "aws_iam_role_policy" "api_data_store_files" {
  name   = "data-store-files"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.data_store_files_access.json
}

resource "aws_iam_role_policy" "worker_data_store_files" {
  name   = "data-store-files"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.data_store_files_access.json
}

# --- Network ---

# The app subnets reach S3 through a gateway endpoint of their own (free, and no NAT charges),
# apart from the sandboxes', whose policy stays limited to ECR's image layers. With the route,
# all of the region's S3 traffic from these subnets goes through it: the egress proxy's too
# (sandboxes downloading from allowed sites hosted on S3), so it lets everything through, as the
# NAT did. The task roles' policies limit what the app itself may do.
data "aws_iam_policy_document" "app_s3_endpoint" {
  statement {
    actions   = ["s3:*"]
    resources = ["*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_vpc_endpoint" "app_s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  policy            = data.aws_iam_policy_document.app_s3_endpoint.json
  tags              = { Name = "${var.name}-app-s3" }
}
