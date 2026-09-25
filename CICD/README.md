# CI/CD Workflow Reference

This directory contains a copy of the GitHub Actions workflows and the
Dependabot config from the application repository:

[KaanMyumyun/HospitalSystem](https://github.com/KaanMyumyun/HospitalSystem)

The files are kept here for documentation and review only. They are stored under
`CICD/.github/`, not the repository root `.github/`, so they do not run from
this infrastructure repository.

## Source of Truth

The real CI/CD pipeline lives in
[KaanMyumyun/HospitalSystem](https://github.com/KaanMyumyun/HospitalSystem)
under [`.github/workflows/`](https://github.com/KaanMyumyun/HospitalSystem/tree/main/.github/workflows).
That is where the workflows run and where changes should be made.

The files in this directory are only an example of that pipeline. They can fall
behind the application repository, so check there for the current version.

## Workflow Order

1. `CI`
   - runs on pull requests, pushes to `main`, and weekly (Mondays 03:00 UTC)
     so published images pick up base image security fixes
   - restores, builds and tests the backend
   - audits npm dependencies (fails on `high` or worse), then lints, tests and
     builds the frontend

2. `Docker Image CI`
   - runs after `CI` succeeds on `main`
   - builds the backend and frontend images once each with Buildx, reusing
     layers from the GitHub Actions cache; the weekly run skips the cache
   - scans those images with Trivy and stops before pushing anything on a
     fixable `HIGH` or `CRITICAL` vulnerability
   - tags each image as `latest` and `YYYY-MM-DD-shortsha-runnumber`, a tag
     unique to the build, so the weekly rebuild of an unchanged commit gets a
     new tag instead of overwriting the old image
   - pushes the scanned images to Docker Hub and Amazon ECR
   - uses GitHub Actions OIDC to assume the AWS ECR push role
   - saves the pushed tag and commit as a `release` artifact for the deploy

3. `Deploy to EKS`
   - runs after image build and push succeeds
   - waits for approval in the `production` GitHub environment
   - reads the tag and commit from the Docker run's `release` artifact
     instead of working the tag out again
   - skips the deploy if that commit is no longer the tip of `main`, so an
     older build approved late cannot overwrite a newer one
   - assumes the AWS EKS deploy role through OIDC
   - finds the ops instance named in the `DEPLOY_INSTANCE_NAME` repository
     variable and sends it the `DEPLOY_SSM_DOCUMENT` SSM document, because the
     EKS API endpoint is private
   - on the instance, the document sets backend and frontend Deployment images
     to that tag and waits for rollout completion when deployments are scaled
     above zero

If the app is scaled down to zero, the deploy workflow still updates the
Deployment image fields to the new tag. It skips waiting for rollout
completion because no pods are running. The next manual scale-up starts pods
from that exact image tag.
