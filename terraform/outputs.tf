output "cluster_name" {
  value       = google_container_cluster.sobel_cluster.name
  description = "GKE cluster name"
}

output "cluster_endpoint" {
  value       = google_container_cluster.sobel_cluster.endpoint
  description = "GKE cluster API endpoint"
  sensitive   = true
}

output "cluster_ca_certificate" {
  value       = google_container_cluster.sobel_cluster.master_auth[0].cluster_ca_certificate
  description = "GKE cluster CA certificate (base64-encoded)"
  sensitive   = true
}

output "get_credentials_command" {
  value       = "gcloud container clusters get-credentials ${var.cluster_name} --region ${var.region} --project ${var.project_id}"
  description = "Command to configure kubectl for the cluster"
}

output "vpc_name" {
  value       = google_compute_network.sobel_vpc.name
  description = "VPC network name"
}

output "subnet_name" {
  value       = google_compute_subnetwork.sobel_subnet.name
  description = "Subnet name"
}

output "uploads_bucket_name" {
  value       = google_storage_bucket.uploads.name
  description = "GCS bucket for original image uploads"
}

output "results_bucket_name" {
  value       = google_storage_bucket.results.name
  description = "GCS bucket for processed results"
}

output "gcs_service_account_email" {
  value       = google_service_account.gcs_sa.email
  description = "GCS service account email (attach to worker VMs & create K8s secret from key)"
}

output "gcs_service_account_key" {
  value       = google_service_account_key.gcs_sa_key.private_key
  description = "GCS service account private key (base64-encoded, for K8s Secret)"
  sensitive   = true
}

output "worker_mig_name" {
  value       = google_compute_region_instance_group_manager.worker_mig.name
  description = "Managed Instance Group name for worker VMs"
}

output "worker_mig_self_link" {
  value       = google_compute_region_instance_group_manager.worker_mig.self_link
  description = "MIG self-link for Compute Engine API resize calls"
}

output "rabbitmq_password" {
  value       = var.rabbitmq_password
  description = "RabbitMQ password (for K8s Secret creation)"
  sensitive   = true
}

output "redis_password" {
  value       = var.redis_password
  description = "Redis password (for K8s Secret creation)"
  sensitive   = true
}
