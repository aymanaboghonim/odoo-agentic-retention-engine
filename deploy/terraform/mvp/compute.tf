# One ECS container instance (ADR-0019), private subnet, IPv6 egress (ADR-0024 decision 3).

data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}

resource "aws_instance" "host" {
  ami                         = data.aws_ssm_parameter.ecs_ami.value
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.private.id
  vpc_security_group_ids      = [aws_security_group.host.id]
  iam_instance_profile        = aws_iam_instance_profile.instance.name
  associate_public_ip_address = false
  # One IPv6 address for egress (ADR-0024). Not marked primary: AWS reports
  # enable_primary_ipv6 as false after create, which drifted on every plan (#97).
  ipv6_address_count = 1
  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    cluster = aws_ecs_cluster.main.name
    region  = var.region
  })

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
    http_protocol_ipv6          = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gib
    encrypted             = true
    delete_on_termination = true
  }

  tags = { Name = var.name_prefix }

  lifecycle {
    # ADR-0024: a replace needs a VPC origin detach/update/re-attach, so AMI releases and
    # boot-script edits do not replace the host; the runbook covers a planned replace.
    ignore_changes = [ami, user_data]
  }
}
