resource "aws_security_group" "wide_open" {
  name        = "wide-open-sg"
  description = "Allows all inbound traffic from anywhere"

  ingress {
    description = "all inbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
