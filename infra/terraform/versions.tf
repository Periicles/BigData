terraform {
  required_version = ">= 1.10"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "5.4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.9.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "0.14.1"
    }
  }
}

provider "azurerm" {
  subscription_id = var.subscription_id

  features {
    key_vault {
      # Démonstration jetable : un `destroy` doit vraiment libérer le nom.
      purge_soft_delete_on_destroy = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
}
