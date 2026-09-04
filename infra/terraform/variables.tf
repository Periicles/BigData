variable "subscription_id" {
  description = "Abonnement Azure cible (Azure for Students)."
  type        = string
}

variable "prefixe" {
  description = "Préfixe des noms de ressources."
  type        = string
  default     = "eds"
}

variable "region" {
  description = "Région Azure. France Central est certifiée HDS."
  type        = string
  default     = "francecentral"
}

variable "ip_autorisee" {
  description = "Seule plage autorisée à joindre Metabase (CIDR, ex. 203.0.113.4/32)."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.ip_autorisee))
    error_message = "ip_autorisee doit être un CIDR IPv4, par exemple 203.0.113.4/32."
  }
}

variable "taille_noeud" {
  description = "Taille du nœud AKS. B2ms : 2 vCPU, 8 Go, dans le quota étudiant."
  type        = string
  default     = "Standard_B2ms"
}

variable "version_aks" {
  description = "Version mineure de Kubernetes ; le patch suit le canal `patch`."
  type        = string
  default     = "1.35"
}
