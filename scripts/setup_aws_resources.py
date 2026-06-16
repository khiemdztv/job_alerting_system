"""
Setup AWS resources (DynamoDB tables, S3 bucket, SQS queue) in the user's AWS account.
"""
from __future__ import annotations

import sys
import os
import boto3
from botocore.exceptions import ClientError

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import get_settings

def setup_resources():
    settings = get_settings()
    region = settings.aws_region
    print(f"Initializing AWS resources in region: {region}...")

    # Get Account ID to create a unique S3 bucket name
    sts = boto3.client("sts", region_name=region)
    try:
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        print(f"Connected to AWS Account: {account_id} (ARN: {identity['Arn']})")
    except Exception as e:
        print(f"Error: Unable to connect to AWS. Please check your credentials: {e}")
        return

    # S3 Bucket Setup
    s3 = boto3.client("s3", region_name=region)
    bucket_name = f"vieclambot-data-lake-{account_id}"
    print(f"Setting up S3 bucket: {bucket_name}...")
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region}
            )
        print(f"S3 bucket '{bucket_name}' created/verified successfully.")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code")
        if error_code in ["BucketAlreadyExists", "BucketAlreadyOwnedByYou"]:
            print(f"S3 bucket '{bucket_name}' already exists.")
        else:
            print(f"Error creating S3 bucket: {e}")
            return

    # DynamoDB Setup
    dynamodb = boto3.client("dynamodb", region_name=region)
    
    # 1. jobs table
    jobs_table = settings.dynamodb_jobs_table
    print(f"Setting up DynamoDB Table: {jobs_table}...")
    try:
        dynamodb.create_table(
            TableName=jobs_table,
            KeySchema=[
                {"AttributeName": "job_id", "KeyType": "HASH"}
            ],
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST"
        )
        print(f"DynamoDB table '{jobs_table}' is being created...")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"DynamoDB table '{jobs_table}' already exists.")
        else:
            print(f"Error creating DynamoDB table '{jobs_table}': {e}")

    # 2. users table
    users_table = settings.dynamodb_users_table
    print(f"Setting up DynamoDB Table: {users_table}...")
    try:
        dynamodb.create_table(
            TableName=users_table,
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"}
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST"
        )
        print(f"DynamoDB table '{users_table}' is being created...")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"DynamoDB table '{users_table}' already exists.")
        else:
            print(f"Error creating DynamoDB table '{users_table}': {e}")

    # SQS Setup
    sqs = boto3.client("sqs", region_name=region)
    queue_name = settings.sqs_raw_jobs_queue
    print(f"Setting up SQS queue: {queue_name}...")
    try:
        response = sqs.create_queue(QueueName=queue_name)
        queue_url = response["QueueUrl"]
        print(f"SQS queue '{queue_name}' created/verified successfully. URL: {queue_url}")
    except ClientError as e:
        print(f"Error creating SQS queue '{queue_name}': {e}")

    # Update .env file with the unique bucket name
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        new_lines = []
        for line in lines:
            if line.startswith("VIECLAMBOT_S3_DATA_LAKE_BUCKET="):
                new_lines.append(f"VIECLAMBOT_S3_DATA_LAKE_BUCKET={bucket_name}\n")
            else:
                new_lines.append(line)
        
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print("Updated .env file with the unique S3 bucket name.")

    print("\nAWS resources setup is complete! Let's wait a few seconds for DynamoDB tables to become ACTIVE.")
    
    # Wait for tables to exist
    waiter = dynamodb.get_waiter("table_exists")
    try:
        waiter.wait(TableName=jobs_table, WaiterConfig={"Delay": 2, "MaxAttempts": 10})
        waiter.wait(TableName=users_table, WaiterConfig={"Delay": 2, "MaxAttempts": 10})
        print("All DynamoDB tables are now ACTIVE and ready for use!")
    except Exception as e:
        print(f"Warning: Waiting for tables to become active timed out, but they should be active shortly: {e}")

if __name__ == "__main__":
    setup_resources()
