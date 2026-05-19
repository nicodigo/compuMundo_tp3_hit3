# Instance template for worker VMs.
# Worker VMs use Container-Optimized OS with a Docker container running
# the Sobel worker process.
resource "google_compute_instance_template" "worker_template" {
  name_prefix  = "sobel-worker-template-"
  machine_type = var.worker_machine_type
  tags         = ["worker"]

  disk {
    source_image = "cos-cloud/cos-stable"
    disk_size_gb = 30
    disk_type    = "pd-standard"
    boot         = true
  }

  network_interface {
    network    = google_compute_network.sobel_vpc.id
    subnetwork = google_compute_subnetwork.sobel_subnet.id

    access_config {
      # Ephemeral external IP for Docker image pull
    }
  }

  service_account {
    email  = google_service_account.gcs_sa.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  # Metadata values are populated by the worker-autoscaler service
  # after RabbitMQ LoadBalancer hostname is known.
  # The startup script pulls these from the metadata server.
  metadata = {
    rabbitmq_user       = "guest"
    rabbitmq_password   = var.rabbitmq_password
    rabbitmq_port       = "5672"
    gcs_upload_bucket   = var.upload_bucket_name
    gcs_result_bucket   = var.results_bucket_name
  }

  metadata_startup_script = <<-EOF
    #!/bin/bash
    set -e

    RABBITMQ_HOST=$(curl -s -H "Metadata-Flavor: Google" \
      http://metadata.google.internal/computeMetadata/v1/instance/attributes/rabbitmq_host)

    # If RabbitMQ host is empty, the worker-autoscaler hasn't updated
    # the template yet. Wait (with backoff) for it to be set.
    if [ -z "$RABBITMQ_HOST" ]; then
      echo "ERROR: rabbitmq_host metadata not set."
      echo "Set it via: gcloud compute instance-templates update <template> --metadata rabbitmq_host=<LB_IP>"
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
      ${var.worker_container_image}
  EOF

  lifecycle {
    create_before_destroy = true
  }
}

# Regional Managed Instance Group for worker VMs.
# Scaled by the worker-autoscaler service (not by GCP's built-in autoscaler).
resource "google_compute_region_instance_group_manager" "worker_mig" {
  name     = var.worker_mig_name
  region   = var.region
  base_instance_name = "sobel-worker"

  distribution_policy_zones = var.zones

  target_size = var.worker_target_size

  version {
    instance_template = google_compute_instance_template.worker_template.id
  }

  update_policy {
    type                  = "PROACTIVE"
    minimal_action        = "REPLACE"
    max_surge_fixed       = 2
    max_unavailable_fixed = 1
  }

  auto_healing_policies {
    health_check      = google_compute_health_check.worker_health.id
    initial_delay_sec = 300
  }
}

# TCP health check on SSH port — if SSH is down, the VM is unrecoverable.
resource "google_compute_health_check" "worker_health" {
  name = "sobel-worker-health"

  tcp_health_check {
    port = 22
  }
}
