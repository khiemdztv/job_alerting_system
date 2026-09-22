"""Store the You.com key in AWS Secrets Manager and enable web search safely."""

from __future__ import annotations

import argparse
import getpass
import json

import boto3
from botocore.exceptions import ClientError


def configure(profile: str, secret_name: str, function_name: str) -> dict:
    api_key = getpass.getpass("You.com API key (input hidden): ").strip()
    if not api_key.startswith("ydc-sk-") or len(api_key) < 30:
        raise ValueError("The value does not look like a You.com API key")

    session = boto3.Session(profile_name=profile, region_name="ap-southeast-1")
    secrets = session.client("secretsmanager")
    secret_value = json.dumps({"YDC_API_KEY": api_key})
    try:
        created = secrets.create_secret(Name=secret_name, SecretString=secret_value)
        secret_arn = created["ARN"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceExistsException":
            raise
        secret_arn = secrets.describe_secret(SecretId=secret_name)["ARN"]
        secrets.put_secret_value(SecretId=secret_name, SecretString=secret_value)
    finally:
        api_key = ""
        secret_value = ""

    lambda_client = session.client("lambda")
    configuration = lambda_client.get_function_configuration(FunctionName=function_name)
    role_arn = configuration["Role"]
    role_name = role_arn.rsplit("/", 1)[-1]
    session.client("iam").put_role_policy(
        RoleName=role_name,
        PolicyName="ViecLamBotYouSearchSecret",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "secretsmanager:GetSecretValue",
                        "Resource": secret_arn,
                    }
                ],
            }
        ),
    )

    variables = dict(configuration.get("Environment", {}).get("Variables", {}))
    variables.pop("VIECLAMBOT_YOU_API_KEY", None)
    variables.update(
        {
            "VIECLAMBOT_YOU_API_KEY_SECRET_ARN": secret_arn,
            "VIECLAMBOT_YOU_SEARCH_ENABLED": "true",
            "VIECLAMBOT_YOU_SEARCH_COUNT": "25",
            "VIECLAMBOT_YOU_SEARCH_TIMEOUT_SECONDS": "8",
            "VIECLAMBOT_YOU_SEARCH_FRESHNESS": "week",
        }
    )
    lambda_client.update_function_configuration(
        FunctionName=function_name,
        Environment={"Variables": variables},
        RevisionId=configuration["RevisionId"],
    )
    lambda_client.get_waiter("function_updated_v2").wait(FunctionName=function_name)
    verified = lambda_client.get_function_configuration(FunctionName=function_name)
    verified_vars = verified.get("Environment", {}).get("Variables", {})
    if verified_vars.get("VIECLAMBOT_YOU_API_KEY_SECRET_ARN") != secret_arn:
        raise RuntimeError("Lambda environment verification failed")
    if "VIECLAMBOT_YOU_API_KEY" in verified_vars:
        raise RuntimeError("Plaintext You.com key remained in Lambda environment")
    return {
        "function": function_name,
        "secret": secret_name,
        "enabled": verified_vars.get("VIECLAMBOT_YOU_SEARCH_ENABLED") == "true",
        "plaintext_key_in_environment": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="vieclambot")
    parser.add_argument("--secret-name", default="vieclambot/you-api-key")
    parser.add_argument("--function", default="vieclambot-webhook")
    args = parser.parse_args()
    print(
        json.dumps(
            configure(args.profile, args.secret_name, args.function),
            ensure_ascii=False,
        )
    )
