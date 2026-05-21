# Deployment Guide — Sobel Distributed Image Processing System

Complete step-by-step instructions to deploy the system on Google Cloud
from nothing. Every step explains **what** it does and **why** it is
needed at that point in the sequence.

**Prerequisite knowledge**: Comfortable with a terminal. No prior GCP
or Kubernetes experience required.

---

## Table of Contents

- [0. Overview of What We Are Building](#0-overview-of-what-we-are-building)
- [1. Prerequisites](#1-prerequisites)
- [2. Local Setup: Install Required Tools](#2-local-setup-install-required-tools)
- [3. GCP Project Setup](#3-gcp-project-setup)
- [4. Create a Service Account for Terraform](#4-create-a-service-account-for-terraform)
- [5. Clone the Repository and Configure Terraform](#5-clone-the-repository-and-configure-terraform)
- [6. Deploy Infrastructure with Terraform](#6-deploy-infrastructure-with-terraform)
- [7. Build and Push Docker Images to Google Container Registry](#7-build-and-push-docker-images-to-google-container-registry)
- [8. Get Cluster Credentials](#8-get-cluster-credentials)
- [9. Deploy Kubernetes Infrastructure (Namespaces, Secrets, RabbitMQ, Redis)](#9-deploy-kubernetes-infrastructure-namespaces-secrets-rabbitmq-redis)
- [10. Configure the Application ConfigMap](#10-configure-the-application-configmap)
- [11. Deploy Application Services (Backend, Split, Joiner, Frontend, DLQ Monitor)](#11-deploy-application-services-backend-split-joiner-frontend-dlq-monitor)
- [12. Deploy Worker VMs](#12-deploy-worker-vms)
- [13. Deploy the Worker Autoscaler](#13-deploy-the-worker-autoscaler)
- [14. Validate the Deployment](#14-validate-the-deployment)
- [15. End-to-End Test](#15-end-to-end-test)
- [16. Cleanup / Teardown](#16-cleanup--teardown)
- [17. Troubleshooting](#17-troubleshooting)

---

## 0. Overview of What We Are Building

This system processes PNG images through a distributed Sobel edge-detection
pipeline. Here is what happens after you upload an image:

1. **Frontend** (web UI) receives your upload, sends it to the **Backend** API.
2. **Backend** saves the original image to Cloud Storage (GCS), records metadata
   in Redis, and publishes a "new image" event to **RabbitMQ**.
3. **Split** service picks up the event, downloads the image, divides it into
   a 4x4 grid (16 fragments), uploads each fragment to GCS, and publishes
   16 fragment processing tasks to RabbitMQ.
4. **Worker** VMs (running on Compute Engine, not inside Kubernetes) pull
   fragments from the queue, apply the Sobel edge-detection filter using
   SciPy, upload the result back to GCS, and publish a completion event.
5. **Joiner** service collects all 16 completed fragments from a fanout
   exchange, tracks progress in Redis, and reassembles the full edge-map
   image when all fragments arrive.
6. **Backend** publishes a completion event, and the **Frontend** shows
   a download link for the Sobel-processed result.

The system uses:
- **GKE** (Google Kubernetes Engine) to run the orchestrator services
- **RabbitMQ** with dead-letter queues for reliable async messaging
- **Redis** for in-memory fragment-tracking state (1-hour TTL)
- **GCS** (Google Cloud Storage) for image and result storage
- **Compute Engine MIG** (Managed Instance Group) for worker VMs
- **Terraform** for infrastructure-as-code
- **GitHub Actions** for CI/CD (optional — the guide covers manual
  deployment end-to-end)

---

## 1. Prerequisites

### 1.1 What you need before starting

**WHAT**: Verify you have the accounts, permissions, and credentials to
follow this guide.
**WHY**: Every step depends on these being in place. Missing prerequisites
cause errors partway through that are harder to debug later.

- **A Google Cloud Platform account** with billing enabled
  (the $300 free trial credits are sufficient).
- **A GitHub account** (only needed for the optional CI/CD pipelines).
- **A computer** running Linux, macOS, or Windows (with WSL2). You need:
  - Admin/sudo access to install software.
  - At least 2 GB free disk space for Docker images.
  - Internet access (no firewall blocking GCP, Docker Hub, or GitHub).

### 1.2 GCP quota you must have available

| Resource             | Quota Required | Default Free Tier Quota |
|----------------------|----------------|-------------------------|
| Compute Engine CPUs  | 8-10 vCPUs     | 8–24 (varies by region) |
| GKE Clusters         | 1              | 5                       |
| Cloud Storage buckets| 2              | 5 (per project)         |
| Firewall rules       | ~6             | 500                     |

> **Single-zone design**: This project pins all resources to `us-central1-b`
> (see `terraform/gke.tf` `node_locations`). This is intentional — it keeps
> CPU usage low (4 vCPUs baseline for GKE, leaving 8 vCPUs headroom for
> worker VMs), prevents zone-fragmentation of StatefulSet PVCs, and keeps
> RabbitMQ ↔ worker traffic within one zone (no cross-zone latency or
> egress charges). High availability across zones is not a requirement for
> this student project.

If you get a "Quota exceeded" error during `terraform apply`, request a
quota increase at: https://console.cloud.google.com/iam-admin/quotas

---

## 2. Local Setup: Install Required Tools

**WHAT**: Install the tools listed below on your local machine.
**WHY**: Each tool handles a different part of the deployment pipeline.
The versions listed are minimums; newer versions usually work too.

### Terraform (>= 1.7)

Infrastructure-as-code tool that creates the GKE cluster, VPC network,
GCS buckets, and worker VM templates.

```bash
# Linux (download binary)
wget https://releases.hashicorp.com/terraform/1.7.5/terraform_1.7.5_linux_amd64.zip
unzip terraform_1.7.5_linux_amd64.zip
sudo mv terraform /usr/local/bin/
terraform version

# macOS
brew install terraform
terraform version
```

### gcloud CLI (>= 460)

Command-line interface to Google Cloud.

```bash
# Download and install
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
gcloud init

# Verify
gcloud --version
```

### kubectl (>= 1.28)

Kubernetes command-line tool.

```bash
gcloud components install kubectl
kubectl version --client
```

### Docker (>= 24)

Container build engine.

```bash
docker --version

# If not installed:
#   Linux: https://docs.docker.com/engine/install/
#   macOS: https://docs.docker.com/desktop/mac/install/
#   Windows: https://docs.docker.com/desktop/windows/install/
```

### Python (>= 3.11) and Pillow

Only needed if you want to run the end-to-end test with a programmatic
upload. Not required for deployment.

```bash
python3 --version
```

### (Optional) OpenSSL

Used to generate random passwords. Most systems have it pre-installed.

```bash
openssl version
```

---

## 3. GCP Project Setup

### 3.1 Log in to your Google account

**WHAT**: Authenticate with your Google Cloud account.
**WHY**: All gcloud operations require authentication.

```bash
gcloud auth login
# A browser window opens. Log in with your GCP account.
```

### 3.2 Set up Application Default Credentials (ADC)

**WHAT**: Authenticate for programmatic access used by Terraform.
**WHY**: Terraform (and other SDK tools) use ADC instead of your personal
gcloud login session. This must run BEFORE creating the project so ADC is
ready when Terraform needs it.

```bash
gcloud auth application-default login
# A browser window opens again. Log in with the same account.
```

### 3.3 Create a GCP Project

**WHAT**: Create a new GCP project that isolates resources and billing.
**WHY**: A clean project avoids conflicts with existing resources and makes
cleanup easier.

```bash
# Replace YOUR_INITIALS with your actual initials (e.g. "jd" for John Doe)
# The project ID must be globally unique across all GCP.
export PROJECT_ID="sobel-processing-YOUR_INITIALS"

gcloud projects create "$PROJECT_ID" --name="Sobel Processing"
```

### 3.4 Set the active project

**WHAT**: Tell gcloud which project to target.
**WHY**: Without this, every gcloud command must include `--project=`.
Setting it once avoids typos and mismatch errors.

```bash
gcloud config set project "$PROJECT_ID"

# Verify
gcloud config get-value project
# Expected output: sobel-processing-YOUR_INITIALS

# Prevent "quota project" mismatch warnings from ADC
gcloud auth application-default set-quota-project "$PROJECT_ID"
```

### 3.5 Link billing

**WHAT**: Attach a billing account to the project.
**WHY**: GKE clusters and Compute Engine VMs are not free-tier resources;
they consume credits or real money. Billing must be active before enabling
APIs.

```bash
# List your billing accounts
gcloud billing accounts list

# Link the project to a billing account
# Replace BILLING_ACCOUNT_ID with the ID from the list above
gcloud billing projects link "$PROJECT_ID" \
  --billing-account="BILLING_ACCOUNT_ID"
```

### 3.6 Enable Required APIs

**WHAT**: Turn on all GCP services this project needs.
**WHY**: By default, APIs are disabled. Terraform and kubectl commands
will fail with "API not enabled" errors if these are off. Billing must
already be linked (step 3.5) before APIs can be enabled.

```bash
gcloud services enable container.googleapis.com
gcloud services enable compute.googleapis.com
gcloud services enable storage.googleapis.com
gcloud services enable monitoring.googleapis.com
gcloud services enable iamcredentials.googleapis.com
gcloud services enable iam.googleapis.com
gcloud services enable cloudresourcemanager.googleapis.com
```

> **Why these 7 APIs**: `container.googleapis.com` for GKE,
> `compute.googleapis.com` for Compute Engine (MIG, VMs, firewalls),
> `storage.googleapis.com` for Cloud Storage buckets,
> `monitoring.googleapis.com` for Cloud Monitoring,
> `iamcredentials.googleapis.com` for service account key creation,
> `iam.googleapis.com` for service account management,
> `cloudresourcemanager.googleapis.com` for project-level IAM bindings.

### 3.7 Set compute defaults

**WHAT**: Set default region and zone for Compute Engine resources.
**WHY**: Do this AFTER enabling the compute API (step 3.6) — setting
compute configuration before the API is enabled can cause unexpected
behavior.

```bash
gcloud config set compute/region us-central1
gcloud config set compute/zone us-central1-a
```

---

## 4. Create a Service Account for Terraform

**WHAT**: Create a dedicated service account (a non-human identity) with
permissions to create infrastructure. Terraform will use this account's
credentials instead of your personal login.
**WHY**: This is a security best practice — the service account gets only
the minimum permissions needed. If the key is compromised, you can revoke
it without affecting your personal account.

### 4.1 Create the service account

```bash
gcloud iam service-accounts create terraform-sa \
  --display-name="Terraform Service Account"

export SA_EMAIL="terraform-sa@${PROJECT_ID}.iam.gserviceaccount.com"
```

### 4.2 Grant permissions

**WHAT**: Assign IAM roles so Terraform can create and manage
infrastructure.
**WHY**: Without these roles, Terraform calls to create the VPC, GKE
cluster, GCS buckets, and MIG will fail with "permission denied" errors.
Terraform also needs permission to create service accounts and assign
them project-level IAM roles.

```bash
# Full control over GKE clusters (create, update, delete)
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/container.admin"

# Full control over Compute Engine (VMs, firewalls, networks, MIGs)
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/compute.admin"

# Full control over Cloud Storage (buckets, objects)
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.admin"

# Allows the SA to act as other SAs (needed for IAM bindings)
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser"

# Service Account Admin — needed to create the GCS access SA
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountAdmin"

# Service Account Key Admin — needed to create the GCS access SA key
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountKeyAdmin"

# Project IAM Admin — needed to grant the storage.objectAdmin role
# to the GCS service account (google_project_iam_member in gcs.tf)
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/resourcemanager.projectIamAdmin"
```

### 4.3 Download the service account key

**WHAT**: Create a JSON key file that Terraform will use to authenticate.
**WHY**: Terraform needs this file to call GCP APIs as the service account.
The key file is sensitive — treat it like a password.

```bash
mkdir -p ~/.gcp
gcloud iam service-accounts keys create ~/.gcp/sobel-terraform-key.json \
  --iam-account="${SA_EMAIL}"
```

### 4.4 Export the credential path

**WHAT**: Tell Terraform where to find the key file.
**WHY**: The Google provider for Terraform reads the
`GOOGLE_APPLICATION_CREDENTIALS` environment variable to find credentials.

Add this to your shell profile (`~/.bashrc`, `~/.zshrc`, or `~/.profile`):

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.gcp/sobel-terraform-key.json"
```

Then reload:

```bash
source ~/.bashrc
```

---

## 5. Clone the Repository and Configure Terraform

### 5.1 Clone the repository

**WHAT**: Download the project source code to your machine.
**WHY**: The repository contains the application code, Terraform configs,
Kubernetes manifests, Dockerfiles, and CI/CD pipeline definitions.

```bash
git clone <repository-url>
cd sobel-distributed-system
```

### 5.2 Configure Terraform variables

**WHAT**: Create your personal `terraform.tfvars` file from the example.
This file holds project-specific values like your project ID and passwords.
**WHY**: The `.tfvars` file is git-ignored (it is in `.gitignore`) so your
secrets stay local. The example file shows all the variables you need
to provide.

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with your values:

```hcl
project_id            = "sobel-processing-YOUR_INITIALS"
region                = "us-central1"

# GCS bucket names must be globally unique across all GCP.
upload_bucket_name    = "sobel-uploads-YOUR_INITIALS"
results_bucket_name   = "sobel-results-YOUR_INITIALS"

# Worker container image path (we will push this later in step 7)
worker_container_image = "gcr.io/sobel-processing-YOUR_INITIALS/worker:latest"

# RabbitMQ and Redis passwords
rabbitmq_password     = "replace-with-a-random-32-char-password"
redis_password        = "replace-with-a-random-32-char-password"
```

> **Tip**: Generate random passwords with:
> ```bash
> openssl rand -hex 16
> ```

Save the file.

---

## 6. Deploy Infrastructure with Terraform

### 6.1 Initialize Terraform

**WHAT**: Download the required Terraform providers (Google provider) and
set up the working directory.
**WHY**: `terraform init` must run once before any other Terraform commands.

```bash
terraform init
```

Expected output:
```
Initializing the backend...
Initializing provider plugins...
- Finding hashicorp/google versions matching "~> 5.0"...
- Installing hashicorp/google v5.x.x...
Terraform has been successfully initialized!
```

### 6.2 Preview the plan

**WHAT**: See what resources Terraform will create before actually
creating them.
**WHY**: Review the plan to catch misconfigurations early.

```bash
terraform plan -out=tfplan
```

Review the output. It will list:
- 1 VPC network + 1 subnet
- ~6 firewall rules
- 1 GKE cluster + 2 node pools
- 2 GCS buckets
- 1 service account + 1 service account key + 1 IAM binding
- 1 instance template + 1 regional MIG + 1 health check

### 6.3 Apply the plan

**WHAT**: Create all the infrastructure resources on GCP.
**WHY**: This is the actual deployment command. It provisions the VPC
network, GKE cluster (takes the longest), GCS buckets, service accounts,
instance template, and MIG.

```bash
terraform apply tfplan
```

Expected duration: **10–15 minutes** (most of this is GKE cluster creation).

**What gets created**:

- **VPC network** (`sobel-vpc`) with a subnet (`sobel-subnet`,
  CIDR 10.0.0.0/16) and secondary IP ranges for Kubernetes pods
  (10.1.0.0/16) and services (10.2.0.0/20).
- **Firewall rules** for GKE master access, node-to-node traffic,
  RabbitMQ (5672, 15672), Redis (6379), frontend HTTP/HTTPS, and
  optional SSH.
- **GKE cluster** (`sobel-cluster`) with two node pools:
  - `infra-pool` (1 node, e2-standard-2, tainted for RabbitMQ/Redis)
  - `app-pool` (1–4 nodes, autoscaled, e2-standard-2)
- **Two GCS buckets** for uploads and results (auto-delete after 7 days).
- **GCS service account** (`sobel-gcs-access`) with `storage.objectAdmin`
  role and a JSON key (output as `gcs_service_account_key`).
- **Worker instance template** + **Managed Instance Group**
  (`sobel-worker-mig`, initially scaled to 0).

### 6.4 Collect Terraform outputs

**WHAT**: Extract values Terraform created that you need for later steps.
**WHY**: The GCS bucket names, service account key, and passwords must be
passed to Kubernetes secrets and ConfigMaps.

```bash
export UPLOAD_BUCKET=$(terraform output -raw uploads_bucket_name)
export RESULT_BUCKET=$(terraform output -raw results_bucket_name)
export GCS_SA_KEY=$(terraform output -raw gcs_service_account_key)
export TF_RABBIT_PW=$(terraform output -raw rabbitmq_password)
export TF_REDIS_PW=$(terraform output -raw redis_password)

echo "Upload bucket: ${UPLOAD_BUCKET}"
echo "Result bucket: ${RESULT_BUCKET}"
```

### 6.5 Resource Budget

**WHAT**: Document the CPU allocation across GKE and worker VMs so
the deployer can verify quota headroom before applying.

**WHY**: The project operates under tight GCP quotas (12 vCPUs in
`us-central1` by default). Understanding the budget prevents
`QUOTA_EXCEEDED` errors during worker autoscaling.

| Component           | Instance Type  | vCPUs/node | Nodes (min) | Nodes (max) | vCPUs (min) | vCPUs (max) |
|---------------------|----------------|------------|-------------|-------------|-------------|-------------|
| infra-pool          | e2-standard-2  | 2          | 1 (static)  | 1           | 2           | 2           |
| app-pool            | e2-standard-2  | 2          | 1 (HPA)     | 4 (HPA)     | 2           | 8           |
| **GKE subtotal**    |                |            |             |             | **4**       | **10**      |
| Worker MIG          | e2-standard-2  | 2          | 0           | 4           | 0           | 8           |
| **Grand total**     |                |            |             |             | **4**       | **10–18¹**  |

¹ Worker MIG maximum is 10 (set in `worker_max_replicas`), but at GKE max
the quota headroom is only 2 vCPUs (12 − 10 = 2). The autoscaler should
target `MAX_WORKERS=4` to stay within quota during normal operation.

**Application pod CPU requests** (all pods fit on 2 vCPUs):

| Pod                 | Replicas | CPU req/pod | CPU total | Notes |
|---------------------|----------|-------------|-----------|-------|
| backend             | 2        | 100m        | 200m      | HPA 2-8; consider reducing to 1 for student project |
| frontend            | 2        | 100m        | 200m      | HPA 2-4; consider reducing to 1 for student project |
| split               | 1        | 100m        | 100m      | |
| joiner              | 1        | 100m        | 100m      | |
| dlq-monitor         | 1        | 50m         | 50m       | |
| worker-autoscaler   | 1        | 50m         | 50m       | |
| **apps subtotal**   | **8**    |             | **700m**   | Fits on one 2-CPU node |
| rabbitmq            | 1        | 250m        | 250m      | infra-pool (static) |
| redis               | 1        | 100m        | 100m      | infra-pool (static) |
| **infra subtotal**  | **2**    |             | **350m**   | Fits on one 2-CPU node |

### 6.6 Important: StatefulSet PVC Zone Binding

> **Warning**: RabbitMQ and Redis use StatefulSets with `volumeClaimTemplates`.
> Once a PVC is created, it is permanently bound to the zone where the pod
> was first scheduled. If you later change node pool zones (e.g., remove
> `us-central1-b` from `node_locations`), surviving PVCs will be in the
> wrong zone and pods will remain `Pending` with
> `"node(s) had volume node affinity conflict"`.
>
> **Do not change `node_locations` on `infra_pool` after the first
> `terraform apply`.** If you must move to a different zone, you need a
> full teardown (`terraform destroy`, delete PVCs, redeploy from scratch).
>
> Both StatefulSets also include explicit `requiredDuringScheduling` zone
> affinity for `us-central1-b` to prevent accidental rescheduling during
> node pool changes.

---

## 7. Build and Push Docker Images to Google Container Registry

**WHAT**: Package each application service into a Docker container and
upload the images to Google Container Registry (gcr.io) so GKE and
Compute Engine VMs can pull them.
**WHY**: Kubernetes Deployments reference images by their registry path.
The images must exist in the registry before we create the Deployments.

### 7.1 Authenticate Docker to GCR

```bash
gcloud auth configure-docker gcr.io --quiet
```

### 7.2 Build and push each service image

**WHAT**: Build all 7 service images from their Dockerfiles and push
them to GCR with the `latest` tag.
**WHY**: The Kubernetes manifests reference images like
`gcr.io/PROJECT_ID/backend:latest`. These must be pushed before the
pods try to pull them.

```bash
cd ..  # back to project root (sobel-distributed-system/)

# Application services (run inside Kubernetes)
for service in backend split joiner frontend dlq_monitor worker_autoscaler; do
  echo "=== Building ${service} ==="
  docker build \
    -f "applications/${service}/Dockerfile" \
    -t "gcr.io/${PROJECT_ID}/${service}:latest" \
    .
  docker push "gcr.io/${PROJECT_ID}/${service}:latest"
done

# Worker image (runs on Compute Engine VMs)
echo "=== Building worker ==="
docker build \
  -f applications/worker/Dockerfile \
  -t "gcr.io/${PROJECT_ID}/worker:latest" \
  .
docker push "gcr.io/${PROJECT_ID}/worker:latest"
```

> **Important**: The build context is the **project root** (`.`), not
> each service directory. The Dockerfiles use
> `COPY applications/ /app/applications/` which references the shared
> library at `applications/shared/`. Building from within
> `applications/worker/` would fail because `applications/shared/`
> would be outside the build context.

---

## 8. Get Cluster Credentials

**WHAT**: Download the GKE cluster's kubeconfig entry so `kubectl`
commands target your new cluster.
**WHY**: Without this step, `kubectl` does not know where your cluster
is or how to authenticate to it.

```bash
gcloud container clusters get-credentials sobel-cluster \
  --region=us-central1 \
  --project="${PROJECT_ID}"

# Verify connectivity
kubectl get nodes
# Expected: 3 nodes (1 infra-pool + 2 app-pool), all STATUS=Ready
```

---

## 9. Deploy Kubernetes Infrastructure (Namespaces, Secrets, RabbitMQ, Redis)

### 9.1 Create namespaces

**WHAT**: Create `infra` and `apps` namespaces to organize resources.
**WHY**: Namespaces provide logical isolation between infrastructure
services (RabbitMQ, Redis) and application services. The Deployment
manifests reference these namespaces.

```bash
kubectl apply -f kubernetes/namespaces.yaml
```

Verify:
```bash
kubectl get namespaces
# Expected: infra, apps (plus default, kube-system, etc.)
```

### 9.2 Create secrets

**WHAT**: Create Kubernetes Secrets to hold passwords and the GCS
service account key.
**WHY**: The Deployment manifests reference these secrets via
`secretKeyRef`. Without them, pods cannot start because required env
vars would be empty.

**IMPORTANT**: The RabbitMQ and Redis passwords in the secrets must
**match** the passwords you set in `terraform.tfvars` (stored in
`TF_RABBIT_PW` and `TF_REDIS_PW` from step 6.4).

```bash
# RabbitMQ password secret (in both namespaces)
for ns in infra apps; do
  kubectl create secret generic rabbitmq-secret \
    --namespace="${ns}" \
    --from-literal=rabbitmq-password="${TF_RABBIT_PW}" \
    --dry-run=client -o yaml | kubectl apply -f -
done

# Redis password secret (in both namespaces)
for ns in infra apps; do
  kubectl create secret generic redis-secret \
    --namespace="${ns}" \
    --from-literal=redis-password="${TF_REDIS_PW}" \
    --dry-run=client -o yaml | kubectl apply -f -
done

# GCS service account key secret (apps namespace only)
# The Terraform output already decodes the key via base64decode(),
# so --from-literal stores the raw JSON without double-encoding.
kubectl create secret generic sobel-secrets \
  --namespace=apps \
  --from-literal=GCS_SERVICE_ACCOUNT_KEY="${GCS_SA_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Verify:
```bash
kubectl get secrets -n infra
kubectl get secrets -n apps
```

### 9.3 Apply the ConfigMap

**WHAT**: Create the ConfigMap that holds non-sensitive configuration.
**WHY**: The application code reads env vars from this ConfigMap
(`RABBITMQ_HOST`, `REDIS_HOST`, `FRAGMENT_GRID_SIZE`, etc.).

```bash
kubectl apply -f kubernetes/configmaps-secrets.yaml
```

> The ConfigMap currently has placeholder values for
> `GCS_UPLOAD_BUCKET` and `GCS_RESULT_BUCKET`. We will fix these
> in step 10.

### 9.4 Deploy RabbitMQ

**WHAT**: Create a RabbitMQ StatefulSet with persistent storage and a
LoadBalancer service (for external worker VM access).
**WHY**: RabbitMQ is the message broker that connects all services.
Pods in both namespaces and worker VMs outside the cluster need to
publish and consume messages.

```bash
kubectl apply -f kubernetes/rabbitmq-deployment.yaml
```

Wait for it:
```bash
kubectl wait --for=condition=ready pod -l app=rabbitmq -n infra --timeout=300s
```

### 9.5 Deploy Redis

**WHAT**: Create a Redis StatefulSet with AOF persistence and password
authentication.
**WHY**: Redis tracks image metadata and fragment completion state.
The Split and Joiner services use it for fragment tracking.

```bash
kubectl apply -f kubernetes/redis-deployment.yaml
```

Wait for it:
```bash
kubectl wait --for=condition=ready pod -l app=redis -n infra --timeout=300s
```

### 9.6 Verify infrastructure is running

```bash
kubectl get pods -n infra
# Expected: rabbitmq-0 Running, redis-0 Running
kubectl get svc -n infra
# Expected: rabbitmq, rabbitmq-lb, rabbitmq-headless, redis
```

### 9.7 Get the RabbitMQ LoadBalancer external IP

**WHAT**: Retrieve the external IP of the RabbitMQ LoadBalancer service.
**WHY**: Worker VMs (running outside the cluster) need this IP to
connect to RabbitMQ. We will configure it in the MIG instance template
later.

```bash
export RABBIT_LB_IP=$(kubectl get svc rabbitmq-lb -n infra \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "RabbitMQ LB IP: ${RABBIT_LB_IP}"
```

If the IP is empty (still provisioning):
```bash
while [ -z "$RABBIT_LB_IP" ]; do
  echo "Waiting for RabbitMQ LB IP..."
  sleep 10
  RABBIT_LB_IP=$(kubectl get svc rabbitmq-lb -n infra \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
done
echo "RabbitMQ LB IP: ${RABBIT_LB_IP}"
```

---

## 10. Configure the Application ConfigMap

**WHAT**: Replace the placeholder bucket names in the ConfigMap with
the actual GCS bucket names created by Terraform.
**WHY**: The application code reads `GCS_UPLOAD_BUCKET` and
`GCS_RESULT_BUCKET` from the ConfigMap. If these contain the literal
string `<GCS_UPLOAD_BUCKET>`, all GCS operations will fail.

```bash
kubectl patch configmap sobel-config -n apps --type merge \
  -p "{\"data\":{\"GCS_UPLOAD_BUCKET\":\"${UPLOAD_BUCKET}\",\"GCS_RESULT_BUCKET\":\"${RESULT_BUCKET}\"}}"
```

Verify:
```bash
kubectl get configmap sobel-config -n apps -o yaml | grep -E "GCS_UPLOAD|GCS_RESULT"
# Expected: the actual bucket names, not placeholders
```

---

## 11. Deploy Application Services (Backend, Split, Joiner, Frontend, DLQ Monitor)

**WHAT**: Use Kustomize to substitute `PROJECT_ID` in image references
and environment variables, then apply all application Deployments.
**WHY**: Kustomize handles the placeholder substitution without modifying
the original YAML files. The repo stays clean and reusable across
different GCP projects. The only file changed during deployment is
`kubernetes/kustomization.yaml`, and only temporarily.

### 11.1 Set your project ID in kustomization.yaml

The Kubernetes manifests use `gcr.io/PROJECT_ID/` as an image placeholder.
The `kubernetes/kustomization.yaml` file contains a `REPLACE_PROJECT_ID`
string in its `images:` and `configMapGenerator:` sections. Replace it
with your actual `$PROJECT_ID`:

```bash
sed -i "s|REPLACE_PROJECT_ID|${PROJECT_ID}|g" kubernetes/kustomization.yaml

# Verify the substitution
grep -c "REPLACE_PROJECT_ID" kubernetes/kustomization.yaml
# Expected output: 0
```

> **How Kustomize handles the substitution**:
> - The `images:` field replaces `gcr.io/PROJECT_ID/<service>` with
>   `gcr.io/<YOUR_PROJECT_ID>/<service>` in all Deployment image fields.
> - The `configMapGenerator` creates a `deployment-config` ConfigMap with
>   your `PROJECT_ID`.
> - The `replacements:` field patches `MIG_PROJECT` in the
>   worker-autoscaler Deployment with the value from that ConfigMap.
> - Application Deployment YAMLs remain untouched — no `sed -i` on
>   source-controlled files.

### 11.2 Apply all application Deployments with Kustomize

```bash
kubectl apply -k kubernetes/
```

This single command processes `kustomization.yaml` and applies:
- `namespaces.yaml` (idempotent — already exists)
- `configmaps-secrets.yaml` (idempotent)
- RabbitMQ and Redis StatefulSets (idempotent)
- All 6 application Deployments (Backend, Split, Joiner, Frontend,
  DLQ Monitor, Worker Autoscaler)

### 11.3 Wait for all deployments to be ready

```bash
for svc in backend split joiner frontend dlq-monitor worker-autoscaler; do
  kubectl rollout status deployment/"${svc}" -n apps --timeout=120s
  echo "=== ${svc} is ready ==="
done
```

### 11.4 Revert kustomization.yaml (optional but recommended)

Restore the placeholder so the file is clean for the next deployment:

```bash
git checkout kubernetes/kustomization.yaml
# -- or --
sed -i "s|${PROJECT_ID}|REPLACE_PROJECT_ID|g" kubernetes/kustomization.yaml
```

### 11.5 Get the frontend external IP

**WHAT**: The frontend is exposed via a LoadBalancer service on port 80.
**WHY**: Users connect to this IP to upload images and download results.

```bash
export FRONTEND_IP=$(kubectl get svc frontend -n apps \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "Frontend URL: http://${FRONTEND_IP}"

# Wait if still provisioning
while [ -z "$FRONTEND_IP" ]; do
  echo "Waiting for Frontend LB IP..."
  sleep 10
  FRONTEND_IP=$(kubectl get svc frontend -n apps \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
done
echo "Frontend URL: http://${FRONTEND_IP}"
```

---

## 12. Deploy Worker VMs

**WHAT**: Set up the Compute Engine worker VMs that will run the Sobel
edge-detection filter.
**WHY**: Workers are the only component that runs outside Kubernetes
(per the design requirement). They connect to internal services via
the RabbitMQ LoadBalancer.

### 12.1 How the worker MIG works

The Managed Instance Group was created by Terraform in step 6 with
`target_size = 0`. It has an instance template that embeds the startup
script and some metadata. However, the `rabbitmq_host` metadata field
is currently **empty** — it must be populated with the RabbitMQ LB IP
before any worker VM can start.

### 12.2 Create a new instance template with the RabbitMQ host

**WHAT**: Create a replacement instance template with `rabbitmq_host`
set to the RabbitMQ LB IP. The startup script reads this metadata
at boot to connect to RabbitMQ.
**WHY**: Instance templates are immutable — you cannot edit an existing
one. A new template must be created with the correct metadata. Also,
`gcloud compute instance-templates create` does NOT have a
`--source-instance-template` flag; you must recreate the template
using the same parameters Terraform used plus the new metadata value.

First, create the worker startup script (extracted from
`terraform/mig.tf`):

```bash
cat > /tmp/worker-startup.sh << 'SCRIPT'
#!/bin/bash
set -e

RABBITMQ_HOST=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/rabbitmq_host)

if [ -z "$RABBITMQ_HOST" ]; then
  echo "ERROR: rabbitmq_host metadata not set."
  exit 1
fi

RABBITMQ_USER=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/rabbitmq_user)
RABBITMQ_PASSWORD=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/rabbitmq_password)
RABBITMQ_PORT=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/rabbitmq_port)
GCS_UPLOAD=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/gcs_upload_bucket)
GCS_RESULT=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/gcs_result_bucket)
WORKER_ID="worker-$(hostname -s)"

docker run -d --restart=unless-stopped --name sobel-worker \
  -e RABBITMQ_URL="amqp://${RABBITMQ_USER}:${RABBITMQ_PASSWORD}@${RABBITMQ_HOST}:${RABBITMQ_PORT}/" \
  -e GCS_UPLOAD_BUCKET="${GCS_UPLOAD}" \
  -e GCS_RESULT_BUCKET="${GCS_RESULT}" \
  -e WORKER_ID="${WORKER_ID}" \
  gcr.io/${PROJECT_ID}/worker:latest
SCRIPT
```

Then create the new template with all parameters matching
`terraform/mig.tf` plus `rabbitmq_host`:

```bash
NEW_TEMPLATE="sobel-worker-template-$(date +%s)"

gcloud compute instance-templates create "${NEW_TEMPLATE}" \
  --machine-type=e2-standard-2 \
  --image-project=cos-cloud \
  --image-family=cos-stable \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-standard \
  --network=sobel-vpc \
  --subnet=sobel-subnet \
  --tags=worker \
  --service-account="sobel-gcs-access@${PROJECT_ID}.iam.gserviceaccount.com" \
  --scopes=cloud-platform \
  --metadata="rabbitmq_host=${RABBIT_LB_IP},rabbitmq_user=guest,rabbitmq_password=${TF_RABBIT_PW},rabbitmq_port=5672,gcs_upload_bucket=${UPLOAD_BUCKET},gcs_result_bucket=${RESULT_BUCKET}" \
  --metadata-from-file=startup-script=/tmp/worker-startup.sh

echo "Created template: ${NEW_TEMPLATE}"
```

### 12.3 Roll the MIG to use the new template

**WHAT**: Tell the MIG to use the new template with the correct
RabbitMQ host. Since `target_size` is 0, no VMs are created yet.
**WHY**: The MIG must know the RabbitMQ host before any worker VM
can start. This step prepares the template for when the autoscaler
scales up.

```bash
gcloud compute instance-groups managed rolling-action start-update \
  sobel-worker-mig \
  --region=us-central1 \
  --version=template="${NEW_TEMPLATE}" \
  --type=proactive \
  --max-surge=2 \
  --max-unavailable=1
```

### 12.4 Verify the MIG status

```bash
gcloud compute instance-groups managed list
# Expected: sobel-worker-mig, target_size=0, current_size=0
```

---

## 13. Deploy the Worker Autoscaler

**WHAT**: The Worker Autoscaler is a Kubernetes Deployment that
continuously monitors the RabbitMQ queue depth and resizes the MIG
accordingly.
**WHY**: Worker VMs cost money when running. The autoscaler keeps them
at 0 when idle and scales up when fragments appear in the queue.

### 13.1 How the autoscaler is deployed

The `kubernetes/worker-autoscaler-deployment.yaml` file is tracked in the
repository. It was already applied in step 11.2 as part of
`kubectl apply -k kubernetes/`. Kustomize handles:

- Image substitution (`gcr.io/PROJECT_ID/worker_autoscaler` → your project)
- `MIG_PROJECT` env var substitution via the `deployment-config`
  ConfigMap (the old `${PROJECT_ID}` shell-style placeholder is now
  `GCP_PROJECT_ID`, replaced by Kustomize's `replacements:` field)
- Memory limits: `requests=128Mi / limits=256Mi` (increased from the
  original 64Mi/128Mi to prevent OOMKilled crashes)

### 13.2 Verify the autoscaler is running

Verify it is polling:
```bash
kubectl logs -n apps deployment/worker-autoscaler --tail=20
# Expected: "Worker Autoscaler starting — min=0, max=10, ..."
# Followed by polling messages every 30 seconds.
```

---

## 14. Validate the Deployment

### 14.1 Check all health endpoints

```bash
# Frontend health
curl -s http://${FRONTEND_IP}/health
# Expected: {"status":"healthy"}
curl -s http://${FRONTEND_IP}/ready
# Expected: {"status":"ready"}

# Backend health (via port-forward since backend is ClusterIP-only)
kubectl port-forward svc/backend -n apps 8001:8000 &
sleep 2
curl -s http://localhost:8001/health
# Expected: {"status":"healthy"}
curl -s http://localhost:8001/ready
# Expected: {"status":"ready"}
kill %1 2>/dev/null
```

### 14.2 Verify RabbitMQ queue topology

```bash
kubectl port-forward svc/rabbitmq -n infra 15672:15672 &
sleep 2
echo "RabbitMQ Management: http://localhost:15672 (user: guest)"
kill %1 2>/dev/null
```

The following queues should appear (empty until an image is uploaded):
- `images.new`
- `fragments.pending`
- `fragments.dead`
- `results.joiner` (transient, appears when joiner connects)
- `results.dashboard` (transient, appears when frontend connects)
- `images.completed`

---

## 15. End-to-End Test

### 15.1 Upload an image through the frontend

Open `http://${FRONTEND_IP}` in a browser. You should see:

- A file picker labeled "Upload a PNG image for distributed edge
  detection"
- An "Upload" button

Select a PNG image (recommended: 512x512 pixels or larger — dimensions
must be evenly divisible by 4). Click **Upload**.

You should observe:
1. "Uploading..." status text
2. A progress bar that fills as fragments complete
3. A "Download Result" button when all 16 fragments are processed

### 15.2 Upload via the API (alternative)

```bash
# Create a small test PNG
python3 -c "
from PIL import Image
img = Image.new('L', (128, 128), 100)
img.save('/tmp/test-sobel.png')
"

# Upload
curl -s -X POST "http://${FRONTEND_IP}/api/images" \
  -F "file=@/tmp/test-sobel.png" | python3 -m json.tool
```

### 15.3 Check processing status

Replace IMAGE_ID from the upload response:

```bash
curl -s "http://${FRONTEND_IP}/api/images/IMAGE_ID/status" | python3 -m json.tool
```

### 15.4 Download the result

```bash
curl -s "http://${FRONTEND_IP}/api/images/IMAGE_ID/result" | python3 -m json.tool
```

---

## 16. Cleanup / Teardown

**WHAT**: Destroy all resources to avoid ongoing charges.
**WHY**: GKE clusters and Compute Engine VMs cost money even when idle.

### 16.1 Stop the autoscaler

```bash
kubectl delete -f kubernetes/worker-autoscaler-deployment.yaml --ignore-not-found
```

### 16.2 Delete worker VMs

```bash
gcloud compute instance-groups managed resize sobel-worker-mig \
  --region=us-central1 --size=0
gcloud compute instance-groups managed delete sobel-worker-mig \
  --region=us-central1 --quiet
```

Delete instance templates:
```bash
TEMPLATES=$(gcloud compute instance-templates list \
  --filter="name~sobel-worker" --format="value(name)")
for t in $TEMPLATES; do
  gcloud compute instance-templates delete "$t" --quiet
done
```

### 16.3 Delete Kubernetes namespaces

```bash
kubectl delete namespace apps
kubectl delete namespace infra
```

### 16.4 Destroy Terraform infrastructure

```bash
cd terraform
terraform destroy -auto-approve
cd ..
```

This deletes: GKE cluster, node pools, VPC, subnets, firewall rules,
GCS buckets, service accounts and keys.

### 16.5 If terraform destroy fails on GCS buckets

Buckets with objects cannot be deleted by Terraform. Empty them first:

```bash
gsutil rm -r "gs://${UPLOAD_BUCKET}"
gsutil rm -r "gs://${RESULT_BUCKET}"
cd terraform && terraform destroy -auto-approve
```

### 16.6 (Optional) Delete the GCP project

```bash
gcloud projects delete "${PROJECT_ID}"
```

---

## 17. Troubleshooting

### Deployment Issues

| Symptom | Cause | Fix |
|---|---|---|
| `terraform apply` fails with `403 Quota exceeded` | vCPU quota too low in region | Request increase at console.cloud.google.com/iam-admin/quotas |
| `terraform apply` fails with `403 API not enabled` | APIs not enabled | Run the `gcloud services enable` commands from step 3.3 |
| `terraform apply` fails with `PERMISSION_DENIED` | SA key not set or lacks roles | Verify `GOOGLE_APPLICATION_CREDENTIALS` is set and step 4.2 roles applied |
| `gcloud container clusters get-credentials` fails | `compute/region` not set | Run `gcloud config set compute/region us-central1` |
| `kubectl get nodes` returns nothing | Credentials not configured | Run step 8 again |

### Pod Issues

| Symptom | Cause | Fix |
|---|---|---|
| Pods stuck in `Pending` with `Insufficient cpu` | Node pool full | Wait 2-3 min for cluster autoscaler |
| Pods stuck in `ImagePullBackOff` | Image not found | Verify images were pushed (step 7). Check `PROJECT_ID` replacement (step 11.1) |
| Pods in `CrashLoopBackOff` with `KeyError: 'GCS_SERVICE_ACCOUNT_KEY'` | `sobel-secrets` missing | Run the secret creation command from step 9.2 |
| Pods in `CrashLoopBackOff` with `KeyError: 'GCS_UPLOAD_BUCKET'` | ConfigMap placeholders not replaced | Run the ConfigMap patch from step 10 |
| Pods in `CrashLoopBackOff` — other errors | App fails to start | Check `kubectl logs <pod-name> -n apps` |

### RabbitMQ Issues

| Symptom | Cause | Fix |
|---|---|---|
| RabbitMQ connection refused | Pod not ready or wrong credentials | `kubectl get pods -n infra`, verify secrets exist |
| Worker VMs cannot connect | `rabbitmq_host` metadata not set | Update instance template with LB IP (step 12.2) |

### GCS Issues

| Symptom | Cause | Fix |
|---|---|---|
| GCS upload fails with `403` | SA key missing or invalid | Recreate `sobel-secrets` from `terraform output gcs_service_account_key` |
| GCS operations fail with bucket not found | ConfigMap has placeholders | Run step 10 |

### Worker Issues

| Symptom | Cause | Fix |
|---|---|---|
| MIG stays at 0 even with queue depth >0 | Autoscaler cannot reach RabbitMQ | Check `kubectl logs deployment/worker-autoscaler -n apps` |
| Worker VMs created but never process | `rabbitmq_host` missing or wrong | Check startup script via serial console |
| Fragment timeout | Worker too slow | Increase worker machine type in `terraform.tfvars` |
