resource "aws_ecr_repository" "backend" {
  name         = "hospital-backend"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_repository" "frontend" {
  name         = "hospital-frontend"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the latest 30 backend images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 30
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_ecr_lifecycle_policy" "frontend" {
  repository = aws_ecr_repository.frontend.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the latest 30 frontend images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 30
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "terraform_data" "initial_ecr_image_push" {
  triggers_replace = {
    enabled                 = var.push_initial_ecr_images
    script_sha              = filesha256("${path.module}/../scripts/push-initial-ecr-images.sh")
    backend_source_dir      = local.backend_source_dir
    frontend_source_dir     = local.frontend_source_dir
    backend_dockerfile      = var.backend_dockerfile
    frontend_dockerfile     = var.frontend_dockerfile
    backend_repository_url  = aws_ecr_repository.backend.repository_url
    frontend_repository_url = aws_ecr_repository.frontend.repository_url
    image_tag               = var.initial_image_tag
    frontend_api_url        = local.frontend_api_url
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/.."
    interpreter = ["/bin/bash", "-c"]
    command     = abspath("${path.module}/../scripts/push-initial-ecr-images.sh")

    environment = {
      PUSH_INITIAL_ECR_IMAGES = tostring(var.push_initial_ecr_images)
      AWS_REGION              = var.aws_region
      ECR_REGISTRY            = local.ecr_registry
      BACKEND_SOURCE_DIR      = local.backend_source_dir
      FRONTEND_SOURCE_DIR     = local.frontend_source_dir
      BACKEND_DOCKERFILE      = var.backend_dockerfile
      FRONTEND_DOCKERFILE     = var.frontend_dockerfile
      BACKEND_REPOSITORY_URL  = aws_ecr_repository.backend.repository_url
      FRONTEND_REPOSITORY_URL = aws_ecr_repository.frontend.repository_url
      IMAGE_TAG               = var.initial_image_tag
      FRONTEND_API_URL        = local.frontend_api_url
    }
  }

  depends_on = [
    aws_ecr_repository.backend,
    aws_ecr_repository.frontend
  ]
}
