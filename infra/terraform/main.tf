data "azurerm_client_config" "actuel" {}

# Suffixe aléatoire : Storage Account, Key Vault et ACR ont des noms
# mondialement uniques.
resource "random_string" "suffixe" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

locals {
  etiquettes = {
    projet = "eds-chu"
    usage  = "demonstration"
  }
}

resource "azurerm_resource_group" "eds" {
  name     = "rg-${var.prefixe}-cloud"
  location = var.region
  tags     = local.etiquettes
}
