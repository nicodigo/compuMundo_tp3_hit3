resource "google_storage_bucket" "uploads" {
  name          = var.upload_bucket_name
  location      = var.region
  force_destroy = false
  storage_class = "STANDARD"

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket" "results" {
  name          = var.results_bucket_name
  location      = var.region
  force_destroy = false
  storage_class = "STANDARD"

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }
}

# Service account for GCS access from both GKE pods (via K8s Secret key)
# and worker VMs (attached to MIG instance template)
resource "google_service_account" "gcs_sa" {
  account_id   = "sobel-gcs-access"
  display_name = "Sobel GCS Access Service Account"
}

resource "google_project_iam_member" "gcs_sa_storage" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.gcs_sa.email}"
}

# Service account key for GKE pods (mounted as a Kubernetes Secret).
# Worker VMs get GCS access natively via the SA attached to the instance template.
resource "google_service_account_key" "gcs_sa_key" {
  service_account_id = google_service_account.gcs_sa.name
}

# IAM bindings to allow GCS SA to read/write both buckets
# (objectAdmin already covers both, but explicit bindings per bucket
#  would be preferred in production for least-privilege).
# For simplicity, the project-level role is sufficient.
