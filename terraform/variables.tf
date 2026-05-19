variable "project_id" {
  type        = string
  description = "GCP Project ID"
}

variable "region" {
  type        = string
  description = "GCP region for all resources"
  default     = "us-central1"
}

variable "zones" {
  type        = list(string)
  description = "GCP zones within the region"
  default     = ["us-central1-a", "us-central1-b", "us-central1-c"]
}

variable "cluster_name" {
  type        = string
  description = "GKE cluster name"
  default     = "sobel-cluster"
}

variable "infra_pool_name" {
  type        = string
  default     = "infra-pool"
}

variable "infra_machine_type" {
  type        = string
  default     = "e2-standard-2"
}

variable "infra_node_count" {
  type        = number
  default     = 1
}

variable "app_pool_name" {
  type        = string
  default     = "app-pool"
}

variable "app_machine_type" {
  type        = string
  default     = "e2-standard-2"
}

variable "app_node_count" {
  type        = number
  default     = 2
}

variable "app_node_max_count" {
  type        = number
  default     = 4
}

variable "network_name" {
  type        = string
  default     = "sobel-vpc"
}

variable "subnet_name" {
  type        = string
  default     = "sobel-subnet"
}

variable "subnet_cidr" {
  type        = string
  default     = "10.0.0.0/16"
}

variable "pod_cidr" {
  type        = string
  default     = "10.1.0.0/16"
}

variable "svc_cidr" {
  type        = string
  default     = "10.2.0.0/20"
}

variable "upload_bucket_name" {
  type        = string
}

variable "results_bucket_name" {
  type        = string
}

variable "worker_mig_name" {
  type        = string
  default     = "sobel-worker-mig"
}

variable "worker_min_replicas" {
  type        = number
  default     = 0
}

variable "worker_max_replicas" {
  type        = number
  default     = 10
}

variable "worker_target_size" {
  type        = number
  default     = 0
}

variable "worker_machine_type" {
  type        = string
  default     = "e2-standard-2"
}

variable "worker_container_image" {
  type        = string
}

variable "rabbitmq_host" {
  type        = string
  default     = ""
}

variable "rabbitmq_password" {
  type        = string
  sensitive   = true
}

variable "redis_password" {
  type        = string
  sensitive   = true
}

variable "worker_allow_ssh" {
  type        = bool
  default     = false
}
