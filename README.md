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

| Tool | Minimum Version | Purpose |
|---|---|---|
| [Terraform](https://developer.hashicorp.com/terraform/downloads) | >= 1.7 | Infrastructure as Code |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | >= 1.28 | Kubernetes management |
| [Docker](https://docs.docker.com/engine/install/) | >= 24 | Container builds |
| [gcloud CLI](https://cloud.google.com/sdk/docs/install) | >= 460 | GCP authentication |
| [Python](https://www.python.org/downloads/) | >= 3.11 | Application development |
| [GCP Account](https://cloud.google.com/free) | With billing enabled | Cloud infrastructure |

## GCP Project Setup

1. **Create a new GCP project:**
   ```bash
   gcloud projects create sobel-processing-<YOUR_INITIALS> --name="Sobel Processing"
   gcloud config set project sobel-processing-<YOUR_INITIALS>
   PROJECT_ID=$(gcloud config get-value project)
   ```

2. **Enable required APIs:**
   ```bash
   gcloud services enable container.googleapis.com
   gcloud services enable compute.googleapis.com
   gcloud services enable storage-component.googleapis.com
   gcloud services enable monitoring.googleapis.com
   gcloud services enable iamcredentials.googleapis.com
   ```

3. **Create a service account for Terraform:**
   ```bash
   gcloud iam service-accounts create terraform-sa \
     --display-name="Terraform Service Account"

   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:terraform-sa@$PROJECT_ID.iam.gserviceaccount.com" \
     --role="roles/container.admin"

   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:terraform-sa@$PROJECT_ID.iam.gserviceaccount.com" \
     --role="roles/compute.admin"

   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:terraform-sa@$PROJECT_ID.iam.gserviceaccount.com" \
     --role="roles/storage.admin"

   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:terraform-sa@$PROJECT_ID.iam.gserviceaccount.com" \
     --role="roles/iam.serviceAccountUser"
   ```

4. **Download the service account key:**
   ```bash
   mkdir -p ~/.gcp
   gcloud iam service-accounts keys create ~/.gcp/sobel-terraform-key.json \
     --iam-account="terraform-sa@$PROJECT_ID.iam.gserviceaccount.com"
   export GOOGLE_APPLICATION_CREDENTIALS=~/.gcp/sobel-terraform-key.json
   ```

## Quick Deploy

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd sobel-distributed-system
   ```

2. **Deploy infrastructure with Terraform:**
   ```bash
   cd terraform
   cp terraform.tfvars.example terraform.tfvars
   # Edit terraform.tfvars with your project_id, bucket names, passwords, and worker_container_image
   terraform init
   terraform plan -out=tfplan
   terraform apply tfplan
   cd ..
   ```

   This creates the GKE cluster, VPC, GCS buckets, and the worker Managed Instance Group (initially scaled to zero).

3. **Get cluster credentials:**
   ```bash
   gcloud container clusters get-credentials sobel-cluster --region=us-central1
   ```

4. **Deploy infrastructure services:**
   ```bash
   kubectl apply -f kubernetes/namespaces.yaml

   # Create secrets using the same passwords from terraform.tfvars
   kubectl create secret generic rabbitmq-secret \
     --from-literal=rabbitmq-password=$(terraform -chdir=terraform output -raw rabbitmq_password) \
     -n infra
   kubectl create secret generic redis-secret \
     --from-literal=redis-password=$(terraform -chdir=terraform output -raw redis_password) \
     -n infra

   kubectl apply -f kubernetes/rabbitmq-deployment.yaml
   kubectl apply -f kubernetes/redis-deployment.yaml
   ```

5. **Deploy application services:**
   ```bash
   # Edit kubernetes/configmaps-secrets.yaml first:
   #   Replace <GCS_UPLOAD_BUCKET> and <GCS_RESULT_BUCKET> with your bucket names
   kubectl apply -f kubernetes/configmaps-secrets.yaml
   kubectl apply -f kubernetes/backend-deployment.yaml
   kubectl apply -f kubernetes/split-deployment.yaml
   kubectl apply -f kubernetes/joiner-deployment.yaml
   kubectl apply -f kubernetes/frontend-deployment.yaml
   kubectl apply -f kubernetes/dlq-monitor-deployment.yaml
   ```

6. **Wait for pods to be ready:**
   ```bash
   kubectl get pods -n infra -w
   kubectl get pods -n apps -w
   ```

7. **Get the frontend URL:**
   ```bash
   kubectl get svc frontend -n apps
   # Access the EXTERNAL-IP in your browser
   ```

   > **Note:** Worker VMs are deployed as part of step 2 via the Managed Instance Group. They scale from zero automatically when the worker-autoscaler detects queued fragments.

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

```bash
# Empty GCS buckets first (required before terraform can delete them)
gsutil rm -r gs://<UPLOAD_BUCKET_NAME>
gsutil rm -r gs://<RESULTS_BUCKET_NAME>

# Destroy all infrastructure (cluster, VPC, buckets, worker MIG)
cd terraform
terraform destroy -auto-approve
cd ..
```

Optionally, delete the entire GCP project:
```bash
gcloud projects delete <YOUR_PROJECT_ID>
```
