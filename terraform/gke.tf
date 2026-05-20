resource "google_container_cluster" "sobel_cluster" {
  name     = var.cluster_name
  location = var.region

  remove_default_node_pool = true
  initial_node_count       = 1

  node_config {
      disk_type    = "pd-standard"
      disk_size_gb = 50
  }

  network    = google_compute_network.sobel_vpc.id
  subnetwork = google_compute_subnetwork.sobel_subnet.id

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  network_policy {
    enabled  = true
    provider = "CALICO"
  }

  logging_service    = "logging.googleapis.com/kubernetes"
  monitoring_service = "monitoring.googleapis.com/kubernetes"

  release_channel {
    channel = "REGULAR"
  }

  # Private cluster disabled — using public master endpoint with subnet-scoped
  # firewall rules for simplicity (student project).
  private_cluster_config {
    enable_private_nodes = false
  }

  depends_on = [
    google_compute_subnetwork.sobel_subnet
  ]
}

resource "google_container_node_pool" "infra_pool" {
  name     = var.infra_pool_name
  location = var.region
  cluster  = google_container_cluster.sobel_cluster.name

  node_count = var.infra_node_count

  node_config {
    machine_type = var.infra_machine_type
    disk_size_gb = 50
    disk_type    = "pd-standard"

    labels = {
      pool = "infra"
    }

    taint {
      key    = "dedicated"
      value  = "infra"
      effect = "NO_SCHEDULE"
    }

    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}

resource "google_container_node_pool" "app_pool" {
  name     = var.app_pool_name
  location = var.region
  cluster  = google_container_cluster.sobel_cluster.name

  initial_node_count = var.app_node_count

  autoscaling {
    min_node_count = var.app_node_count
    max_node_count = var.app_node_max_count
  }

  node_config {
    machine_type = var.app_machine_type
    disk_size_gb = 50
    disk_type    = "pd-standard"

    labels = {
      pool = "apps"
    }

    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}
