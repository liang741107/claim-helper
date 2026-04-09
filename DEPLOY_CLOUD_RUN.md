# Claim Helper on Google Cloud Run

This project is prepared for Google Cloud Run deployment and works well in Taiwan using region `asia-east1`.

## What you need

- A Google Cloud project
- Billing enabled on that project
- Cloud Run Admin, Cloud Build, and Artifact Registry APIs enabled
- `gcloud` installed locally, or Google Cloud Shell

## Fastest deploy

From this repository root, run:

```powershell
.\deploy_cloud_run.ps1 -ProjectId YOUR_PROJECT_ID
```

Default settings:

- service name: `claim-helper`
- region: `asia-east1`
- public access: enabled
- max instances: `1`

## Why max instances is 1

This app currently stores preview and upload state on the container filesystem. Cloud Run instances do not share local files, so `max-instances=1` helps keep preview and export requests on the same instance.

For higher-scale production use, move temporary state to shared storage such as Cloud Storage, Firestore, or Memorystore.

## Manual deploy command

```powershell
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
gcloud run deploy claim-helper `
  --source . `
  --region asia-east1 `
  --allow-unauthenticated `
  --cpu 1 `
  --memory 2Gi `
  --timeout 300 `
  --concurrency 8 `
  --max-instances 1 `
  --session-affinity `
  --set-env-vars APP_BUILD=cloudrun-prod
```

After deployment, get the service URL:

```powershell
gcloud run services describe claim-helper --region asia-east1 --format="value(status.url)"
```

## Custom domain later

Cloud Run gives you a stable `run.app` URL first. Later, you can map your own domain to the same service.
