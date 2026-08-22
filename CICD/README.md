# CI/CD Workflow Reference

This directory contains a copy of the GitHub Actions workflows from the
application repository:

[KaanMyumyun/HospitalSystem](https://github.com/KaanMyumyun/HospitalSystem)

The files are kept here for documentation and review only. They are stored under
`CICD/.github/workflows/`, not the repository root `.github/workflows/`, so they
do not run from this infrastructure repository.

## Workflow Order

1. `.NET`
   - restores dependencies
   - builds the solution
   - runs tests

2. `Docker Image CI`
   - runs after `.NET` succeeds on `main`
   - builds backend and frontend Docker images
   - tags each image as `latest`, `YYYY-MM-DD-shortsha`, and `full-commit-sha`
   - pushes images to Docker Hub and Amazon ECR
   - uses GitHub Actions OIDC to assume the AWS ECR push role

3. `Deploy to EKS`
   - runs after image build and push succeeds
   - assumes the AWS EKS deploy role through OIDC
   - updates kubeconfig for `eks-pr1`
   - sets backend and frontend Deployment images to the `YYYY-MM-DD-shortsha` tag
   - waits for rollout completion when deployments are scaled above zero

If the app is scaled down to zero, the deploy workflow still updates the
Deployment image fields to the new `YYYY-MM-DD-shortsha` tag. It skips waiting for rollout
completion because no pods are running. The next manual scale-up starts pods
from that exact image tag.
