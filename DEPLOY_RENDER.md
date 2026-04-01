# Claim Helper Render Deploy

## What this gives you

- A fixed public `onrender.com` URL outsiders can open
- HTTPS handled by Render
- Optional custom domain later

## What is already prepared

This folder already includes:

- app code
- insurer blank-form PDFs
- Dockerfile
- requirements
- `render.yaml`

## What you still need

- A Render account
- A GitHub, GitLab, or Bitbucket repository

## Fastest path

1. Create a new Git repository from this folder.
2. Push the repository to GitHub, GitLab, or Bitbucket.
3. In Render, create a new Web Service from that repository.
4. Keep the Docker runtime.
5. Deploy.

After deployment, Render assigns a stable `onrender.com` subdomain to the service.

## If you want your own domain

After the first deploy succeeds, add a custom domain in Render and point your DNS to it.

## Important note

This app handles health-related and financial documents. Before public launch, make sure you publish a privacy policy and complete any platform compliance work you need.
