# Claim Helper

Claim Helper is a web app that reads claim forms, diagnosis certificates, and bank-book images, then produces a filled claim PDF for multiple Taiwan insurers.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/liang741107/claim-helper)

## Highlights

- Mobile-friendly upload flow
- Preview before export
- Multi-insurer form support
- OCR for diagnosis certificates and bank-book images
- Docker-ready deployment for a fixed public URL

## Deploy

This repository is prepared for both Google Cloud Run and Render.

For Taiwan, prefer Google Cloud Run in region `asia-east1`.

- Cloud Run: [DEPLOY_CLOUD_RUN.md](DEPLOY_CLOUD_RUN.md)
- Render: [DEPLOY_RENDER.md](DEPLOY_RENDER.md)
