"""
AWS client factory for ViecLamBot.

Centralizes boto3 client/resource creation with proper region configuration.
Uses caching to avoid creating duplicate clients.
"""

from __future__ import annotations

from functools import lru_cache

import boto3
from mypy_boto3_dynamodb import DynamoDBServiceResource
from mypy_boto3_s3 import S3Client
from mypy_boto3_sqs import SQSClient

from src.config import get_settings


@lru_cache(maxsize=1)
def get_dynamodb_resource() -> DynamoDBServiceResource:
    """Get cached DynamoDB resource."""
    settings = get_settings()
    return boto3.resource("dynamodb", region_name=settings.aws_region)


@lru_cache(maxsize=1)
def get_s3_client() -> S3Client:
    """Get cached S3 client."""
    settings = get_settings()
    return boto3.client("s3", region_name=settings.aws_region)


@lru_cache(maxsize=1)
def get_sqs_client() -> SQSClient:
    """Get cached SQS client."""
    settings = get_settings()
    return boto3.client("sqs", region_name=settings.aws_region)


def get_dynamodb_table(table_name: str):
    """Get a DynamoDB table resource."""
    dynamodb = get_dynamodb_resource()
    return dynamodb.Table(table_name)
