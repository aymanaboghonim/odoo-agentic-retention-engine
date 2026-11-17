# ADR-0024 decision 1: private dual-stack subnet, IPv6-only egress, no NAT, no public IPv4.

resource "aws_vpc" "main" {
  cidr_block                       = "10.42.0.0/16"
  assign_generated_ipv6_cidr_block = true
  enable_dns_support               = true
  enable_dns_hostnames             = true
  tags                             = { Name = var.name_prefix }
}

# Lock the default security group: no rules at all.
resource "aws_default_security_group" "locked" {
  vpc_id = aws_vpc.main.id
}

# Required by CloudFront VPC origins; not routed (F-069).
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_egress_only_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "private" {
  vpc_id                          = aws_vpc.main.id
  availability_zone_id            = var.availability_zone_id
  cidr_block                      = "10.42.1.0/24"
  ipv6_cidr_block                 = cidrsubnet(aws_vpc.main.ipv6_cidr_block, 8, 1)
  assign_ipv6_address_on_creation = true
  map_public_ip_on_launch         = false
  tags                            = { Name = "${var.name_prefix}-private" }
}

# No 0.0.0.0/0 route: IPv4 has no internet path. IPv6 leaves via the egress-only gateway.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-private" }
}

resource "aws_route" "ipv6_egress" {
  route_table_id              = aws_route_table.private.id
  destination_ipv6_cidr_block = "::/0"
  egress_only_gateway_id      = aws_egress_only_internet_gateway.main.id
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

# INF-2: one ingress rule, the API port from the CloudFront prefix list (weight 55 of 60, F-064).
resource "aws_security_group" "host" {
  name        = "${var.name_prefix}-host"
  description = "ECS host: API port from CloudFront origin-facing only; no SSH"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name_prefix}-host" }
}

resource "aws_vpc_security_group_ingress_rule" "api_from_cloudfront" {
  security_group_id = aws_security_group.host.id
  description       = "Approval API from CloudFront (VPC origin)"
  ip_protocol       = "tcp"
  from_port         = var.api_port
  to_port           = var.api_port
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
}

resource "aws_vpc_security_group_egress_rule" "all_ipv6" {
  security_group_id = aws_security_group.host.id
  description       = "Egress over IPv6 (egress-only gateway)"
  ip_protocol       = "-1"
  cidr_ipv6         = "::/0"
}

resource "aws_vpc_security_group_egress_rule" "vpc_ipv4" {
  security_group_id = aws_security_group.host.id
  description       = "IPv4 inside the VPC only (DNS resolver, metadata, CloudFront return path)"
  ip_protocol       = "-1"
  cidr_ipv4         = aws_vpc.main.cidr_block
}
