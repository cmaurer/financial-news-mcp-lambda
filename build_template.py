"""Writes build/template.json: CloudFormation for the Lambda, with src/server.py embedded inline (Code.ZipFile)."""
import json
import pathlib

root = pathlib.Path(__file__).parent
code = (root / "src" / "server.py").read_text()
compile(code, "index.py", "exec")

P = "Fn"  # logical-id prefix
template = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "Financial News MCP server: public Lambda function URL with bearer-token auth",
    "Resources": {
        f"{P}Secret": {
            "Type": "AWS::SecretsManager::Secret",
            "Properties": {
                "Name": "financial-news-mcp/config",
                "Description": "bearer_token (generated) guarding the Financial News MCP endpoint",
                "GenerateSecretString": {"SecretStringTemplate": "{}", "GenerateStringKey": "bearer_token",
                                         "PasswordLength": 48, "ExcludePunctuation": True},
            },
        },
        f"{P}LogGroup": {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": "/aws/lambda/financial-news-mcp", "RetentionInDays": 30},
        },
        f"{P}Role": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [
                    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
                "Policies": [{"PolicyName": "financial-news-mcp-minimal", "PolicyDocument": {
                    "Version": "2012-10-17", "Statement": [
                        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                         "Resource": {"Fn::Sub": f"${{{P}LogGroup.Arn}}"}},
                        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                         "Resource": {"Ref": f"{P}Secret"}}]}}],
            },
        },
        f"{P}Function": {
            "Type": "AWS::Lambda::Function",
            "DependsOn": [f"{P}LogGroup"],
            "Properties": {
                "FunctionName": "financial-news-mcp",
                "Description": "Financial News MCP server (stateless streamable HTTP), port of cmaurer/financial-news-mcp",
                "Runtime": "python3.13",
                "Architectures": ["arm64"],
                "Handler": "index.handler",
                "MemorySize": 512,
                "Timeout": 60,
                "Role": {"Fn::GetAtt": [f"{P}Role", "Arn"]},
                "Environment": {"Variables": {"SECRET_ARN": {"Ref": f"{P}Secret"}}},
                "Code": {"ZipFile": code},
            },
        },
        f"{P}Url": {
            "Type": "AWS::Lambda::Url",
            "Properties": {"AuthType": "NONE", "TargetFunctionArn": {"Fn::GetAtt": [f"{P}Function", "Arn"]}},
        },
        f"{P}UrlInvokePermission": {
            "Type": "AWS::Lambda::Permission",
            "Properties": {"FunctionName": {"Ref": f"{P}Function"}, "Action": "lambda:InvokeFunctionUrl",
                           "Principal": "*", "FunctionUrlAuthType": "NONE"},
        },
        f"{P}InvokeViaUrlPermission": {
            "Type": "AWS::Lambda::Permission",
            "Properties": {"FunctionName": {"Ref": f"{P}Function"}, "Action": "lambda:InvokeFunction",
                           "Principal": "*", "InvokedViaFunctionUrl": True},
        },
    },
    "Outputs": {
        "FunctionUrl": {"Value": {"Fn::GetAtt": [f"{P}Url", "FunctionUrl"]}},
        "SecretArn": {"Value": {"Ref": f"{P}Secret"}},
    },
}

(root / "build").mkdir(exist_ok=True)
(root / "build" / "template.json").write_text(json.dumps(template, indent=2) + "\n")
compact = json.dumps(template, separators=(",", ":"), ensure_ascii=True).replace("'", "\\u0027")
(root / "build" / "template.min.json").write_text(compact)
print(f"code {len(code)} chars; compact template {len(compact)} bytes (CLI inline limit 51200)")
