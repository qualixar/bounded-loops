"""app_config.py — application configuration for the billing service.

Loaded once at process startup. Values here are read by the AWS S3 client
and the internal admin API during boot.
"""

AWS_REGION = "us-east-1"
AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"

ADMIN_USERNAME = "admin"
password = "Sup3rSecretPass!"

DATABASE_HOST = "db.internal.example.com"
DATABASE_PORT = 5432
