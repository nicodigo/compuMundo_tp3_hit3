# Deployment Guide -- Sobel Distributed Image Processing System

## 1. Prerequisites

### Required Tools

| Tool | Minimum Version | Verify Installation |
|---|---|---|
| Terraform | >= 1.7 | `terraform version` |
| kubectl | >= 1.28 | `kubectl version --client` |
| Docker | >= 24 | `docker --version` |
| gcloud CLI | >= 460 | `gcloud --version` |
| Python | >= 3.11 | `python3 --version` |

### GCP Resources Required

- A GCP project with billing enabled
- The $300 free tier credits are sufficient
- Quota for: Compute Engine (vCPUs), GKE nodes, Cloud Storage

---

## 2. GCP Project Configuration

### 2.1 Create a GCP Project

```bash
gcloud projects create sobel-processing-<YOUR_INITIALS> \
  --name="Sobel Processing"

gcloud config set project sobel-processing-<YOUR_INITIALS>
```

Replace `<YOUR_INITIALS>` with your initials to ensure uniqueness.

### 2.2 Enable Required APIs

```bash
gcloud services enable container.googleapis.com
gcloud services enable compute.googleapis.com
gcloud services enable storage-component.googleapis.com
gcloud services enable monitoring.googleapis.com
gcloud services enable iamcredentials.googleapis.com
```

### 2.3 Create a Service Account for Terraform

```bash
gcloud iam service-accounts create terraform-sa \
  --display-name="Terraform Service Account"

PROJECT_ID=$(gcloud config get-value project)

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

### 2.4 Download Service Account Key

```bash
mkdir -p ~/.gcp
gcloud iam service-accounts keys create ~/.gcp/sobel-terraform-key.json \
  --iam-account="terraform-sa@$PROJECT_ID.iam.gserviceaccount.com"

export GOOGLE_APPLICATION_CREDENTIALS=~/.gcp/sobel-terraform-key.json
```

---

## 3. Clone and Configure

```bash
git clone <repository-url>
cd sobel-distributed-system

cd terraform
```

---

## 4. Step-by-Step Terraform Deployment

### 4.1 Configure Terraform Variables

```bash
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with your values:

```hcl
project_id          = "sobel-processing-<YOUR_INITIALS>"
region              = "us-central1"
zone                = "us-central1-a"
infra_node_count    = 1
app_node_count      = 2
app_max_node_count  = 4
worker_max_count    = 10
upload_bucket       = "sobel-uploads-<YOUR_INITIALS>"
results_bucket      = "sobel-results-<YOUR_INITIALS>"
```

### 4.2 Deploy GKE Cluster, Networking, and GCS Buckets

```bash
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

Expected duration: **10-15 minutes** for GKE cluster creation.

### 4.3 Verify Cluster Access

```bash
gcloud container clusters get-credentials sobel-cluster \
  --region=$(gcloud config get-value compute/region)

kubectl get nodes
# Expected: 1 infra node + 2 app nodes (all STATUS=Ready)
```

---

## 5. Deploy Kubernetes Services

### 5.1 Create Namespaces

```bash
kubectl apply -f kubernetes/namespaces.yaml
```

Creates `infra` (RabbitMQ, Redis) and `apps` (application services) namespaces.

### 5.2 Create Secrets

```bash
kubectl create secret generic rabbitmq-secret \
  --from-literal=rabbitmq-password=$(openssl rand -base64 12) -n infra

kubectl create secret generic redis-secret \
  --from-literal=redis-password=$(openssl rand -base64 12) -n infra
```

### 5.3 Deploy Infrastructure Services

```bash
kubectl apply -f kubernetes/rabbitmq-deployment.yaml
kubectl apply -f kubernetes/redis-deployment.yaml

kubectl wait --for=condition=ready pod -l app=rabbitmq -n infra --timeout=300s
kubectl wait --for=condition=ready pod -l app=redis -n infra --timeout=300s
```

### 5.4 Verify Infrastructure

```bash
kubectl get pods -n infra

kubectl port-forward svc/rabbitmq 15672:15672 -n infra
# Access: http://localhost:15672 (user: guest)
```

### 5.5 Deploy Application Services

```bash
kubectl apply -f kubernetes/backend-deployment.yaml
kubectl apply -f kubernetes/split-deployment.yaml
kubectl apply -f kubernetes/joiner-deployment.yaml
kubectl apply -f kubernetes/frontend-deployment.yaml
kubectl apply -f kubernetes/dlq-monitor-deployment.yaml
kubectl apply -f kubernetes/worker-autoscaler-deployment.yaml

kubectl wait --for=condition=ready pod -l app \
  -n apps --timeout=300s
```

### 5.6 Verify Application Services

```bash
kubectl get pods -n apps
kubectl get svc -n apps

kubectl get svc frontend -n apps \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

---

## 6. Deploy Worker VMs

### 6.1 Configure Worker Terraform

```bash
cd terraform/workers

cat > terraform.tfvars <<EOF
project_id      = "$(gcloud config get-value project)"
region          = "$(gcloud config get-value compute/region)"
zone            = "$(gcloud config get-value compute/zone)"
mig_name        = "sobel-worker-mig"
min_replicas    = 0
max_replicas    = 10
target_size     = 0
EOF
```

### 6.2 Apply Worker Infrastructure

```bash
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

### 6.3 Verify Workers

```bash
gcloud compute instance-groups managed list
gcloud compute instances list --filter="name~sobel-worker"
# Expected: 0 instances (scale from zero)
```

---

## 7. Build and Push Docker Images

```bash
for service in backend split joiner frontend dlq-monitor; do
  docker build -t gcr.io/$PROJECT_ID/$service:latest ./applications/$service
  docker push gcr.io/$PROJECT_ID/$service:latest
done

docker build -t gcr.io/$PROJECT_ID/worker:latest ./applications/worker
docker push gcr.io/$PROJECT_ID/worker:latest
```

---

## 8. Validation

### 8.1 Health Check Endpoints

```bash
curl http://<FRONTEND_IP>/health

kubectl port-forward svc/backend 8000:8000 -n apps &
curl http://localhost:8000/health
# Expected: {"status": "healthy", "rabbitmq": "connected", "redis": "connected"}
```

### 8.2 End-to-End Processing Test

1. Open `http://<FRONTEND_IP>` in a browser
2. Upload a PNG image (recommended: 512x512 or larger)
3. Observe the progress bar updating as fragments complete
4. Click "Download Result" to obtain the edge-map

### 8.3 RabbitMQ Queue Verification

```bash
kubectl port-forward svc/rabbitmq 15672:15672 -n infra &
curl -u guest:$(kubectl get secret rabbitmq-secret -n infra \
  -o jsonpath='{.data.rabbitmq-password}' | base64 -d) \
  http://localhost:15672/api/queues
```

Expected queues after an upload:
- `images.new` -- empty (split consumes immediately)
- `fragments.pending` -- may have items if workers are scaling
- `fragments.dead` -- empty in normal operation
- `results.joiner` -- transient queue
- `results.dashboard` -- transient queue

---

## 9. Rollback and Cleanup

### 9.1 Roll Back a Deployment

```bash
kubectl rollout undo deployment/backend -n apps
kubectl rollout undo deployment/backend -n apps --to-revision=2
```

### 9.2 Full Cleanup

```bash
# 1. Delete worker VMs
cd terraform/workers
terraform destroy -auto-approve
cd ../..

# 2. Delete application deployments
kubectl delete namespace apps
kubectl delete namespace infra

# 3. Delete cluster and infrastructure
cd terraform
terraform destroy -auto-approve
cd ..

# 4. Delete GCS buckets (must be emptied first)
gsutil rm -r gs://sobel-uploads-<YOUR_INITIALS>
gsutil rm -r gs://sobel-results-<YOUR_INITIALS>

# 5. (Optional) Delete the GCP project entirely
gcloud projects delete $(gcloud config get-value project)
```

---

## 10. Common Deployment Issues

| Issue | Cause | Resolution |
|---|---|---|
| `terraform apply` fails with `403 Quota exceeded` | vCPU quota exceeded in region | Request quota increase or select different region |
| Pods stuck in `Pending` with `Insufficient cpu` | Node pool is full | Wait for cluster autoscaler (2-3 min) |
| Pods in `CrashLoopBackOff` | App fails to start | Check `kubectl logs <pod-name> -n <namespace>` |
| RabbitMQ connection refused | Pod not ready or wrong credentials | Check `kubectl get pods -n infra`, verify secrets |
| MIG shows 0 instances even with queue depth >0 | Autoscaler not connecting | Check `kubectl logs -n apps deployment/worker-autoscaler` |
| GCS upload fails with `403` | Service account permissions | Verify GCS IAM roles are attached |
