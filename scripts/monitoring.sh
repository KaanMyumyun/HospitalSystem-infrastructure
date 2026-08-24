#!/bin/bash
kubectl get hpa -n hospitalsystem
kubectl top pods -n hospitalsystem
kubectl get pods -n hospitalsystem -o wide
kubectl logs -n hospitalsystem deployment/hospital-frontend
kubectl logs -n hospitalsystem deployment/hospital-backend