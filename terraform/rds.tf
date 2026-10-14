# The backend's PostgreSQL database. It lives as long as the stack: destroy
# deletes it with its automated backups and takes no final snapshot, and
# scripts/teardown.sh saves it into seed.sql first (dump-database.yml). Every
# new instance starts empty, and the Ansible bootstrap loads seed.sql into it
# and runs the migrations (ansible/playbooks/database.yml).
resource "aws_db_subnet_group" "main" {
  name        = local.db_identifier
  description = "HospitalSystem PostgreSQL in the private subnets"
  subnet_ids  = [aws_subnet.private_a.id, aws_subnet.private_b.id]

  tags = {
    Name = "${local.db_identifier}-Terraform"
  }
}

# random_page_cost: PostgreSQL's default of 4 assumes spinning disks, so with
# two years of data it read all 25,000 patients to name the 300 rows of an
# appointment list (8.4 ms). 1.1, the usual value for SSD, makes it look them
# up by key (1.5 ms) and keeps the cost tied to the rows returned.
# rds.force_ssl is already 1 by default on PostgreSQL 15+; set here so the
# requirement is in the code.
resource "aws_db_parameter_group" "main" {
  name        = local.db_identifier
  family      = "postgres18"
  description = "HospitalSystem PostgreSQL settings"

  parameter {
    name  = "random_page_cost"
    value = "1.1"
  }

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = {
    Name = "${local.db_identifier}-Terraform"
  }
}

resource "aws_db_instance" "main" {
  identifier = local.db_identifier
  engine     = "postgres"
  # Major version only: RDS installs minor versions itself
  # (auto_minor_version_upgrade), and Terraform doesn't plan them back.
  engine_version = "18"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp2"
  storage_encrypted = true
  kms_key_id        = aws_kms_key.main.arn

  db_name  = local.db_name
  username = local.db_username
  # Write-only: Terraform sends the password only when password_wo_version
  # changes, and keeps it out of the state and the plan. scripts/tf.sh sets it
  # from HOSPITALSYSTEM_DB_PASSWORD.
  password_wo         = var.db_password
  password_wo_version = 1

  parameter_group_name   = aws_db_parameter_group.main.name
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  # Point-in-time restore while the stack is up. Nothing outlives the
  # instance, so a teardown leaves nothing billing.
  backup_retention_period  = var.db_backup_retention_days
  delete_automated_backups = true
  copy_tags_to_snapshot    = true
  skip_final_snapshot      = true
  deletion_protection      = false

  auto_minor_version_upgrade = true
  apply_immediately          = true

  # Database Insights Standard: seven days of Performance Insights, free.
  database_insights_mode                = "standard"
  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  performance_insights_kms_key_id       = aws_kms_key.main.arn

  tags = {
    Name = "${local.db_identifier}-Terraform"
  }
}
