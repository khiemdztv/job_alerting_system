"""
Deployment script to deploy the ViecLamBot Lambda functions to AWS.
Packages code, downloads Linux dependencies, updates Lambda functions,
and configures triggers (EventBridge, SQS, Function URL for Telegram webhook).
"""
import os
import sys
import shutil
import zipfile
import subprocess
import boto3
from botocore.exceptions import ClientError

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import get_settings

def build_package(zip_path, temp_dir):
    """Download Linux dependencies and build ZIP archive."""
    print("Preparing deployment package...")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)

    # Minimal required dependencies (excluding boto3 which is native to Lambda)
    dependencies = [
        "requests",
        "beautifulsoup4",
        "pydantic>=2.0",
        "pydantic-settings",
        "jsonschema",
        "python-dateutil"
    ]

    print("Downloading Linux-compatible binary wheels for packages...")
    try:
        subprocess.run([
            sys.executable, "-m", "pip", "install",
            "--platform", "manylinux2014_x86_64",
            "--only-binary=:all:",
            "-t", temp_dir,
            *dependencies
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to download dependencies: {e}")
        return False

    print("Zipping package, lambdas/ and src/ directories...")
    if os.path.exists(zip_path):
        os.remove(zip_path)

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Add dependency packages
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, temp_dir)
                zip_file.write(full_path, rel_path)

        # Add local lambdas folder
        lambdas_path = os.path.join(project_root, "lambdas")
        for root, dirs, files in os.walk(lambdas_path):
            for file in files:
                if file.endswith(".py") and "__pycache__" not in root:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, project_root)
                    zip_file.write(full_path, rel_path)

        # Add local src folder
        src_path = os.path.join(project_root, "src")
        for root, dirs, files in os.walk(src_path):
            for file in files:
                if file.endswith(".py") and "__pycache__" not in root:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, project_root)
                    zip_file.write(full_path, rel_path)

    print(f"Deployment ZIP archive built: {zip_path} ({os.path.getsize(zip_path) / 1024 / 1024:.2f} MB)")
    return True

def deploy_lambdas():
    settings = get_settings()
    region = settings.aws_region
    print(f"Deploying to AWS Region: {region}")

    # Initialize AWS clients
    sts = boto3.client("sts", region_name=region)
    lambda_client = boto3.client("lambda", region_name=region)
    sqs_client = boto3.client("sqs", region_name=region)
    events_client = boto3.client("events", region_name=region)

    try:
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        print(f"Account: {account_id}")
    except Exception as e:
        print(f"AWS authentication failed: {e}")
        return

    # Constants
    role_arn = f"arn:aws:iam::{account_id}:role/job-alert-lambda-role"
    zip_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist", "deployment.zip")
    temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist", "package")
    
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)

    # Step 1: Package
    if not build_package(zip_path, temp_dir):
        print("Packaging step failed.")
        return

    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    # Clean up temp build folder
    shutil.rmtree(temp_dir, ignore_errors=True)

    # Get SQS Queue attributes
    queue_url = sqs_client.get_queue_url(QueueName=settings.sqs_raw_jobs_queue)["QueueUrl"]
    queue_arn = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    # Lambda definitions
    lambda_functions = {
        "vieclambot-scraper": {
            "handler": "lambdas.scraper_handler.handler",
            "timeout": 900,
            "memory": 256,
            "description": "Scrapes jobs from sources and pushes raw data to SQS"
        },
        "vieclambot-etl": {
            "handler": "lambdas.etl_handler.handler",
            "timeout": 30,
            "memory": 256,
            "description": "Transforms, deduplicates, and loads jobs from SQS to DB & S3"
        },
        "vieclambot-matcher": {
            "handler": "lambdas.matcher_handler.handler",
            "timeout": 120,
            "memory": 256,
            "description": "Matches new jobs against subscriptions and alerts users"
        },
        "vieclambot-webhook": {
            "handler": "lambdas.bot_webhook_handler.handler",
            "timeout": 30,
            "memory": 128,
            "description": "Telegram Bot webhook handler to process user commands"
        }
    }

    # Environment variables
    env_vars = {
        "VIECLAMBOT_AWS_REGION": region,
        "VIECLAMBOT_DYNAMODB_JOBS_TABLE": settings.dynamodb_jobs_table,
        "VIECLAMBOT_DYNAMODB_USERS_TABLE": settings.dynamodb_users_table,
        "VIECLAMBOT_DYNAMODB_SUBSCRIPTIONS_TABLE": settings.dynamodb_subscriptions_table,
        "VIECLAMBOT_S3_DATA_LAKE_BUCKET": settings.s3_data_lake_bucket,
        "VIECLAMBOT_SQS_RAW_JOBS_QUEUE": settings.sqs_raw_jobs_queue,
        "VIECLAMBOT_LOG_LEVEL": settings.log_level,
    }
    if settings.telegram_bot_token:
        env_vars["VIECLAMBOT_TELEGRAM_BOT_TOKEN"] = settings.telegram_bot_token
    if settings.jooble_api_key:
        env_vars["VIECLAMBOT_JOOBLE_API_KEY"] = settings.jooble_api_key

    deployed_arns = {}

    for name, conf in lambda_functions.items():
        print(f"\nDeploying function: {name}...")
        
        # Check if already exists
        exists = False
        try:
            lambda_client.get_function(FunctionName=name)
            exists = True
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise

        if exists:
            print(f"Function {name} exists. Updating code & configurations...")
            # Update Code
            lambda_client.update_function_code(
                FunctionName=name,
                ZipFile=zip_bytes
            )
            # Wait for update
            waiter = lambda_client.get_waiter("function_updated")
            waiter.wait(FunctionName=name)
            
            # Update Config
            res = lambda_client.update_function_configuration(
                FunctionName=name,
                Runtime="python3.12",
                Handler=conf["handler"],
                Role=role_arn,
                Timeout=conf["timeout"],
                MemorySize=conf["memory"],
                Environment={"Variables": env_vars},
                Description=conf["description"]
            )
            deployed_arns[name] = res["FunctionArn"]
        else:
            print(f"Function {name} does not exist. Creating...")
            res = lambda_client.create_function(
                FunctionName=name,
                Runtime="python3.12",
                Role=role_arn,
                Handler=conf["handler"],
                Code={"ZipFile": zip_bytes},
                Description=conf["description"],
                Timeout=conf["timeout"],
                MemorySize=conf["memory"],
                Publish=True,
                Environment={"Variables": env_vars}
            )
            deployed_arns[name] = res["FunctionArn"]
            
        print(f"Successfully deployed: {name}")

    # --- SQS Trigger Event Mapping for ETL ---
    print("\nSetting up SQS Trigger for ETL...")
    try:
        # List existing event sources for ETL
        mappings = lambda_client.list_event_source_mappings(
            FunctionName="vieclambot-etl",
            EventSourceArn=queue_arn
        )["EventSourceMappings"]
        
        if not mappings:
            lambda_client.create_event_source_mapping(
                EventSourceArn=queue_arn,
                FunctionName="vieclambot-etl",
                Enabled=True,
                BatchSize=10
            )
            print("Created Event Source Mapping from SQS to ETL Lambda.")
        else:
            print("Event Source Mapping already configured.")
    except Exception as e:
        print(f"Failed to configure SQS mapping: {e}")

    # --- EventBridge Rules for Scraper & Matcher ---
    print("\nSetting up EventBridge Rules...")
    
    # 1. Scraper rule (Runs every 6 hours)
    try:
        scraper_rule = events_client.put_rule(
            Name="vieclambot-scraper-schedule",
            ScheduleExpression="rate(6 hours)",
            State="ENABLED",
            Description="Triggers scraper every 6 hours"
        )
        events_client.put_targets(
            Rule="vieclambot-scraper-schedule",
            Targets=[{"Id": "scraper-target", "Arn": deployed_arns["vieclambot-scraper"]}]
        )
        
        # Add permission
        try:
            lambda_client.add_permission(
                FunctionName="vieclambot-scraper",
                StatementId="EventBridgeScraperInvoke",
                Action="lambda:InvokeFunction",
                Principal="events.amazonaws.com",
                SourceArn=scraper_rule["RuleArn"]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceConflictException":
                raise
        print("Scraper schedule configured.")
    except Exception as e:
        print(f"Failed to configure Scraper schedule: {e}")

    # 2. Matcher rule (Runs every 6 hours, offset by 15 mins to let ETL finish)
    try:
        matcher_rule = events_client.put_rule(
            Name="vieclambot-matcher-schedule",
            ScheduleExpression="cron(15 */6 * * ? *)",
            State="ENABLED",
            Description="Triggers matcher 15 minutes after scraper"
        )
        events_client.put_targets(
            Rule="vieclambot-matcher-schedule",
            Targets=[{"Id": "matcher-target", "Arn": deployed_arns["vieclambot-matcher"]}]
        )
        
        # Add permission
        try:
            lambda_client.add_permission(
                FunctionName="vieclambot-matcher",
                StatementId="EventBridgeMatcherInvoke",
                Action="lambda:InvokeFunction",
                Principal="events.amazonaws.com",
                SourceArn=matcher_rule["RuleArn"]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceConflictException":
                raise
        print("Matcher schedule configured.")
    except Exception as e:
        print(f"Failed to configure Matcher schedule: {e}")

    # --- API Gateway setup for Bot Webhook ---
    print("\nConfiguring Webhook API Gateway...")
    try:
        apg_client = boto3.client("apigateway", region_name=region)
        
        # Check if API already exists
        api_id = None
        apis = apg_client.get_rest_apis()["items"]
        for api in apis:
            if api["name"] == "vieclambot-api":
                api_id = api["id"]
                break

        if api_id:
            print(f"API Gateway 'vieclambot-api' (ID: {api_id}) already exists. Re-deploying...")
        else:
            print("Creating API Gateway 'vieclambot-api'...")
            api = apg_client.create_rest_api(
                name="vieclambot-api",
                description="ViecLamBot Webhook API Gateway",
                endpointConfiguration={"types": ["REGIONAL"]}
            )
            api_id = api["id"]

        # Get root resource ID
        resources = apg_client.get_resources(restApiId=api_id)["items"]
        root_id = [r["id"] for r in resources if r["path"] == "/"][0]

        # Setup POST method on root
        try:
            apg_client.put_method(
                restApiId=api_id,
                resourceId=root_id,
                httpMethod="POST",
                authorizationType="NONE"
            )
            print("Configured POST method.")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConflictException":
                raise

        # Setup integration to Lambda
        webhook_arn = deployed_arns["vieclambot-webhook"]
        lambda_uri = f"arn:aws:apigateway:{region}:lambda:path/2015-03-31/functions/{webhook_arn}/invocations"
        apg_client.put_integration(
            restApiId=api_id,
            resourceId=root_id,
            httpMethod="POST",
            type="AWS_PROXY",
            integrationHttpMethod="POST",
            uri=lambda_uri
        )
        print("Integrated POST method with Lambda.")

        # Create Deployment
        apg_client.create_deployment(
            restApiId=api_id,
            stageName="prod"
        )
        print("Created API Gateway deployment to 'prod' stage.")

        # Add invoke permission to Lambda
        source_arn = f"arn:aws:execute-api:{region}:{account_id}:{api_id}/*/POST/"
        try:
            lambda_client.add_permission(
                FunctionName="vieclambot-webhook",
                StatementId="APIGatewayInvokeWebhook",
                Action="lambda:InvokeFunction",
                Principal="apigateway.amazonaws.com",
                SourceArn=source_arn
            )
            print("Granted invocation permission to API Gateway.")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceConflictException":
                raise

        # Webhook URL
        webhook_url = f"https://{api_id}.execute-api.{region}.amazonaws.com/prod/"
        print(f"Webhook public URL: {webhook_url}")

        # Register with Telegram API
        if settings.telegram_bot_token:
            print("Registering webhook URL with Telegram API...")
            import requests
            tg_res = requests.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
                json={"url": webhook_url},
                timeout=10
            )
            if tg_res.status_code == 200:
                print("Webhook registered successfully with Telegram API!")
            else:
                print(f"Telegram webhook registration returned status {tg_res.status_code}: {tg_res.text}")
        else:
            print("Warning: Telegram Bot Token not set, skipping webhook registration.")

    except Exception as e:
        print(f"Failed to configure Webhook Function URL: {e}")

    print("\n" + "=" * 50)
    print("ALL LAMBDAS SUCCESSFULLY DEPLOYED TO AWS!")
    print("=" * 50)

if __name__ == "__main__":
    deploy_lambdas()
