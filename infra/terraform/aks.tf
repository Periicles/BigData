resource "azurerm_kubernetes_cluster" "eds" {
  name                      = "aks-${var.prefixe}"
  resource_group_name       = azurerm_resource_group.eds.name
  location                  = azurerm_resource_group.eds.location
  dns_prefix                = "aks-${var.prefixe}"
  kubernetes_version        = var.version_aks
  automatic_upgrade_channel = "patch"
  sku_tier                  = "Free"
  tags                      = local.etiquettes

  default_node_pool {
    name            = "system"
    node_count      = 1
    vm_size         = var.taille_noeud
    os_disk_size_gb = 64
    os_sku          = "AzureLinux"
  }

  identity {
    type = "SystemAssigned"
  }

  # Provisionnement manuel : le seul nœud est celui du pool `system`
  # ci-dessus, rien n'est ajouté automatiquement (quota étudiant).
  node_provisioning_profile {
    mode = "Manual"
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
  }

  # Projette les secrets Key Vault dans les pods (SecretProviderClass).
  key_vault_secrets_provider {
    secret_rotation_enabled = true
  }

  # Monte les conteneurs Blob dans les pods (blobfuse).
  storage_profile {
    blob_driver_enabled = true
  }
}
