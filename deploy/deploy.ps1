# Build, push and (re)deploy the PORC Automation Lambda. Idempotent: safe to re-run after code changes.
#
#   aws sso login --profile paybot-admin
#   .\deploy\deploy.ps1                 # build + push + update function code
#   .\deploy\deploy.ps1 -SkipBuild      # only point the function at the image already in ECR
#
# One-time prerequisites already created on 2026-10-28 (kept here for reference):
#   ECR repo      porc-automation
#   S3 bucket     porc-automation-<account-id>   (porc/state/processed_loads.json + porc/runs/<ts>/ artifacts, 30-day expiry)
#   IAM roles     porc-automation-lambda-role, porc-automation-scheduler-role
#   SSM params    /porc-automation/*  (python deploy/put_parameters.py)
#   Lambda        porc-automation  (2048 MB, 900 s, 1024 MB /tmp)
#   Schedule      porc-automation-hourly (EventBridge Scheduler)
param([switch]$SkipBuild)

$ErrorActionPreference = "Stop"
$env:AWS_PROFILE = "paybot-admin"
$env:AWS_PAGER = ""
$Region   = "us-east-1"
$Account  = (aws sts get-caller-identity --query Account --output text)
if ($LASTEXITCODE -ne 0 -or -not $Account) { throw "No AWS session: run 'aws sso login --profile $env:AWS_PROFILE' first" }
$Registry = "$Account.dkr.ecr.$Region.amazonaws.com"
$Repo     = "porc-automation"
$Function = "porc-automation"
$Image    = "$Registry/${Repo}:latest"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not $SkipBuild) {
    Write-Host "== building image =="
    # --provenance/--sbom off: Lambda rejects image indexes that carry BuildKit attestation manifests
    docker build --platform linux/amd64 --provenance=false --sbom=false -t "${Repo}:latest" .
    if ($LASTEXITCODE -ne 0) { throw "docker build failed" }
    Write-Host "== pushing to ECR =="
    aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $Registry
    docker tag "${Repo}:latest" $Image
    docker push $Image
    if ($LASTEXITCODE -ne 0) { throw "docker push failed" }
}

$digest = aws ecr describe-images --region $Region --repository-name $Repo --image-ids imageTag=latest --query 'imageDetails[0].imageDigest' --output text
Write-Host "== updating function code to $digest =="
aws lambda update-function-code --region $Region --function-name $Function --image-uri "$Registry/${Repo}@$digest" --query 'LastUpdateStatus' --output text
aws lambda wait function-updated --region $Region --function-name $Function
Write-Host "== done. Test with: =="
Write-Host "aws lambda invoke --region $Region --function-name $Function --cli-binary-format raw-in-base64-out --payload '{""args"":[""--dry-run"",""--limit"",""3""]}' out.json; Get-Content out.json"
