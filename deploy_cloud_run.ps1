param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [string]$Region = "asia-east1",

    [string]$ServiceName = "claim-helper",

    [switch]$AuthenticatedOnly
)

$ErrorActionPreference = "Stop"

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

Require-Command "gcloud"

Write-Host "Using project: $ProjectId"
Write-Host "Using region:  $Region"
Write-Host "Service name:  $ServiceName"

gcloud config set project $ProjectId | Out-Host
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com | Out-Host

$deployArgs = @(
    "run", "deploy", $ServiceName,
    "--source", ".",
    "--region", $Region,
    "--cpu", "1",
    "--memory", "2Gi",
    "--timeout", "300",
    "--concurrency", "8",
    "--max-instances", "1",
    "--session-affinity",
    "--set-env-vars", "APP_BUILD=cloudrun-prod"
)

if ($AuthenticatedOnly) {
    $deployArgs += "--no-allow-unauthenticated"
} else {
    $deployArgs += "--allow-unauthenticated"
}

Write-Host "Deploying to Cloud Run..."
& gcloud @deployArgs
if ($LASTEXITCODE -ne 0) {
    throw "Cloud Run deploy failed."
}

$url = & gcloud run services describe $ServiceName --region $Region --format "value(status.url)"
if ($LASTEXITCODE -ne 0) {
    throw "Cloud Run deployed, but failed to fetch service URL."
}

Write-Host ""
Write-Host "Cloud Run URL:"
Write-Host $url
