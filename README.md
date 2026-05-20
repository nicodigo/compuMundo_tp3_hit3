# Sobel Distributed Image Processing System

**Academic Project** — Distributed Systems and Parallel Programming (Universidad Nacional)

A scalable, cloud-native image processing platform that applies the Sobel edge-detection filter to PNG images using distributed workers. The system demonstrates container orchestration (GKE), resilient asynchronous messaging (RabbitMQ), infrastructure as code (Terraform), dynamic compute scaling (MIG), and observable operations (Cloud Monitoring).

## Architecture Overview

| Component | Role |
|---|---|
| **Frontend** | Web UI for image upload and result download |
| **Backend** | REST API orchestrates upload, status polling, and result delivery |
| **Split** | Splits uploaded images into a 4x4 fragment grid |
| **Joiner** | Reassembles processed fragments into the final edge-map image |
| **Worker** | Applies Sobel filter to individual fragments (runs on external VMs) |
| **DLQ Monitor** | Handles failed messages via Dead Letter Queue with exponential backoff |
| **Worker Autoscaler** | Dynamically scales worker VMs based on queue depth |

## Prerequisites

For a full list of prerequisite tools and installation instructions, see the
detailed [Deployment Guide](DEPLOYMENT.md#2-local-setup-install-required-tools).

| Tool | Minimum Version | Purpose |
|---|---|---|
| [Terraform](https://developer.hashicorp.com/terraform/downloads) | >= 1.7 | Infrastructure as Code |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | >= 1.28 | Kubernetes management |
| [Docker](https://docs.docker.com/engine/install/) | >= 24 | Container builds |
| [gcloud CLI](https://cloud.google.com/sdk/docs/install) | >= 460 | GCP authentication |
| [Python](https://www.python.org/downloads/) | >= 3.11 | Application development |
| [GCP Account](https://cloud.google.com/free) | With billing enabled | Cloud infrastructure |

## Quick Deploy

For the complete step-by-step deployment walkthrough, see **[DEPLOYMENT.md](DEPLOYMENT.md)**.
This section is a high-level summary.

### 1. GCP Project Setup

```bash
gcloud auth login
gcloud auth application-default login

export PROJECT_ID="sobel-processing-YOUR_INITIALS"
gcloud projects create "$PROJECT_ID" --name="Sobel Processing"
gcloud config set project "$PROJECT_ID"
gcloud auth application-default set-quota-project "$PROJECT_ID"

# Link billing (required before enabling APIs)
gcloud billing projects link "$PROJECT_ID" --billing-account="BILLING_ACCOUNT_ID"

# Enable all required APIs
gcloud services enable container.googleapis.com
gcloud services enable compute.googleapis.com
gcloud services enable storage.googleapis.com
gcloud services enable monitoring.googleapis.com
gcloud services enable iamcredentials.googleapis.com
gcloud services enable iam.googleapis.com
gcloud services enable cloudresourcemanager.googleapis.com

# Set defaults (after APIs are enabled)
gcloud config set compute/region us-central1
gcloud config set compute/zone us-central1-a
```

### 2. Create a Terraform Service Account

```bash
gcloud iam service-accounts create terraform-sa \
  --display-name="Terraform Service Account"
SA_EMAIL="terraform-sa@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/container.admin"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/compute.admin"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.admin"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountAdmin"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountKeyAdmin"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/resourcemanager.projectIamAdmin"

mkdir -p ~/.gcp
gcloud iam service-accounts keys create ~/.gcp/sobel-terraform-key.json \
  --iam-account="${SA_EMAIL}"
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.gcp/sobel-terraform-key.json"
```

### 3. Deploy Infrastructure

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your project_id, bucket names, passwords, and worker image
terraform init
terraform plan -out=tfplan
terraform apply tfplan

# Save Terraform outputs
export UPLOAD_BUCKET=$(terraform output -raw uploads_bucket_name)
export RESULT_BUCKET=$(terraform output -raw results_bucket_name)
export GCS_SA_KEY=$(terraform output -raw gcs_service_account_key)
export TF_RABBIT_PW=$(terraform output -raw rabbitmq_password)
export TF_REDIS_PW=$(terraform output -raw redis_password)
cd ..
```

This creates the GKE cluster, VPC, GCS buckets, and worker MIG (scaled to 0).

### 4. Build and Push Docker Images

```bash
gcloud auth configure-docker gcr.io --quiet
for service in backend split joiner frontend dlq_monitor worker_autoscaler; do
  docker build -f "applications/${service}/Dockerfile" -t "gcr.io/${PROJECT_ID}/${service}:latest" .
  docker push "gcr.io/${PROJECT_ID}/${service}:latest"
done
docker build -f applications/worker/Dockerfile -t "gcr.io/${PROJECT_ID}/worker:latest" .
docker push "gcr.io/${PROJECT_ID}/worker:latest"
```

### 5. Deploy Services to Kubernetes

```bash
gcloud container clusters get-credentials sobel-cluster --region=us-central1

kubectl apply -f kubernetes/namespaces.yaml

# Create secrets
for ns in infra apps; do
  kubectl create secret generic rabbitmq-secret -n "$ns" \
    --from-literal=rabbitmq-password="$TF_RABBIT_PW" --dry-run=client -o yaml | kubectl apply -f -
  kubectl create secret generic redis-secret -n "$ns" \
    --from-literal=redis-password="$TF_REDIS_PW" --dry-run=client -o yaml | kubectl apply -f -
done
kubectl create secret generic sobel-secrets -n apps \
  --from-literal=GCS_SERVICE_ACCOUNT_KEY="$GCS_SA_KEY" --dry-run=client -o yaml | kubectl apply -f -

# ConfigMap
kubectl apply -f kubernetes/configmaps-secrets.yaml
kubectl patch configmap sobel-config -n apps --type merge \
  -p "{\"data\":{\"GCS_UPLOAD_BUCKET\":\"${UPLOAD_BUCKET}\",\"GCS_RESULT_BUCKET\":\"${RESULT_BUCKET}\"}}"

# Infrastructure services
kubectl apply -f kubernetes/rabbitmq-deployment.yaml
kubectl apply -f kubernetes/redis-deployment.yaml
kubectl wait --for=condition=ready pod -l app=rabbitmq -n infra --timeout=300s
kubectl wait --for=condition=ready pod -l app=redis -n infra --timeout=300s

# Application services (uses Kustomize — no sed -i on source files)
sed -i "s|REPLACE_PROJECT_ID|${PROJECT_ID}|g" kubernetes/kustomization.yaml
kubectl apply -k kubernetes/

# Wait for deployments
for svc in backend split joiner frontend dlq-monitor worker-autoscaler; do
  kubectl rollout status deployment/"${svc}" -n apps --timeout=120s
done

# Revert kustomization.yaml (optional)
git checkout kubernetes/kustomization.yaml

# Get frontend IP
kubectl get svc frontend -n apps
```

### 6. Deploy Worker VMs and Autoscaler

Worker VMs and the autoscaler require additional setup (RabbitMQ LB IP
discovery, MIG template update, autoscaler manifest creation). See the
detailed [Deployment Guide](DEPLOYMENT.md#12-deploy-worker-vms) for
these steps.

> **Note:** Workers are external Compute Engine VMs managed by a MIG.
> They scale from zero automatically when the autoscaler detects queued
> fragments.

## Accessing the System

- **Frontend**: `http://<FRONTEND_EXTERNAL_IP>` -- upload images and download results
- **RabbitMQ Management UI**: `kubectl port-forward svc/rabbitmq 15672:15672 -n infra` then `http://localhost:15672`
- **Redis CLI**: `kubectl exec -it svc/redis -n infra -- redis-cli`

### Architecture Data Flow

```
User Browser
     |
     v Upload PNG
  +--------+     +----------+     +------------+
  |Frontend|---->| Backend  |---->| GCS Bucket |
  +--------+     +----+-----+     +------------+
                      |
                      | publish image.new
                      v
                 +----------+
                 | RabbitMQ |
                 | sobel.*  |
                 +----+-----+
                      |
                      v
                 +----------+      split image -> publish 16 fragments
                 |  Split   |------------------------------------------+
                 +----------+                                          |
                                                                       v
                                                             +-------------------+
                                                             |fragments.queue    |
                                                             +---------+---------+
                                                                       |
                                                  +--------------------+
                                                  v                    v
                                          +---------------+   +-------------------+
                                          |Worker (MIG)   |   | DLQ (on failure)  |
                                          |(xN workers)   |   | ---> dlq-monitor  |
                                          +-------+-------+   +-------------------+
                                                  |
                                                  | publish fragment.result
                                                  v
                                          +----------------+
                                          | sobel.results  |
                                          | (fanout)       |
                                          +--------+-------+
                                                   |
                              +--------------------+
                              v                    v
                       +----------+        +------------+
                       |  Joiner  |        | Frontend   |
                       | (track 16|        | (SSE push) |
                       | fragments|        +------------+
                       +-----+----+
                             |
                             | publish image.completed
                             v
                       +----------+
                       | Backend  |----> GCS (final result)
                       +----------+
                             |
                             v
                       +----------+
                       | Frontend |----> User downloads result
                       +----------+
```

## Troubleshooting

| Issue | Likely Cause | Solution |
|---|---|---|
| `terraform apply` fails with permission error | Service account lacks a required role | Check IAM roles in section "GCP Project Setup" |
| Pods stuck in `ImagePullBackOff` | Container image not found or tag doesn't exist | Verify image names in deployment YAML and Artifact Registry |
| RabbitMQ connection refused | RabbitMQ pod not ready, or credentials mismatch | Check `kubectl logs -n infra deploy/rabbitmq`, verify secrets |
| Workers not scaling | Queue depth metric not being reported | Check worker-autoscaler logs, verify MIG is configured |
| Fragment timeout | Worker VM too slow, or fragment lost due to crash | Check DLQ for the fragment, verify worker health |
| Job stuck at "partial processing" | Joiner waiting for fragment that was never published | Check Redis for image fragment set, publish missing fragments |

## Cleanup

See the detailed [Deployment Guide](DEPLOYMENT.md#16-cleanup--teardown) for
complete cleanup procedures, including worker deletion, MIG teardown, and
optional project deletion.

Quick reference:
```bash
kubectl delete namespace apps
kubectl delete namespace infra

cd terraform
terraform destroy -auto-approve
cd ..
```
