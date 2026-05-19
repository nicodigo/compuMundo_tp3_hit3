resource "google_compute_network" "sobel_vpc" {
  name                    = var.network_name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "sobel_subnet" {
  name          = var.subnet_name
  ip_cidr_range = var.subnet_cidr
  region        = var.region
  network       = google_compute_network.sobel_vpc.id

  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pod_cidr
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.svc_cidr
  }
}

resource "google_compute_firewall" "allow_gke_master" {
  name    = "allow-gke-master"
  network = google_compute_network.sobel_vpc.name
  direction = "INGRESS"
  allow {
    protocol = "tcp"
    ports    = ["443", "10250"]
  }
  source_ranges = [var.subnet_cidr]
}

resource "google_compute_firewall" "allow_node_to_node" {
  name    = "allow-node-to-node"
  network = google_compute_network.sobel_vpc.name
  direction = "INGRESS"
  allow { protocol = "tcp" }
  allow { protocol = "udp" }
  source_ranges = [var.subnet_cidr]
}

resource "google_compute_firewall" "allow_rabbitmq" {
  name    = "allow-rabbitmq"
  network = google_compute_network.sobel_vpc.name
  direction = "INGRESS"
  allow {
    protocol = "tcp"
    ports    = ["5672", "15672"]
  }
  source_ranges = [var.subnet_cidr]
}

resource "google_compute_firewall" "allow_redis" {
  name    = "allow-redis"
  network = google_compute_network.sobel_vpc.name
  direction = "INGRESS"
  allow {
    protocol = "tcp"
    ports    = ["6379"]
  }
  source_ranges = [var.subnet_cidr]
}

resource "google_compute_firewall" "allow_frontend_lb" {
  name    = "allow-frontend-lb"
  network = google_compute_network.sobel_vpc.name
  direction = "INGRESS"
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
  source_ranges = ["0.0.0.0/0"]
}

resource "google_compute_firewall" "allow_ssh_workers" {
  count   = var.worker_allow_ssh ? 1 : 0
  name    = "allow-ssh-workers"
  network = google_compute_network.sobel_vpc.name
  direction = "INGRESS"
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  source_ranges = ["0.0.0.0/0"]
}
