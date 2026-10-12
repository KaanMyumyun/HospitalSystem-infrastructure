# The backend's PostgreSQL database. It lives as long as the stack: destroy
# deletes it with its automated backups and takes no final snapshot. Every new
# instance starts empty, and the Ansible bootstrap loads seed.sql into it and
# runs the migrations (ansible/playbooks/database.yml).
resource "aws_db_subnet_group" "main" {
  name        = local.db_identifier
  description = "HospitalSystem PostgreSQL in the private subnets"
  subnet_ids  = [aws_subnet.private_a.id, aws_subnet.private_b.id]

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
  instance_class = "db.t4g.micro"

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

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = false

  # Point-in-time restore while the stack is up. Nothing outlives the
  # instance, so a teardown leaves nothing billing.
  backup_retention_period  = 1
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
