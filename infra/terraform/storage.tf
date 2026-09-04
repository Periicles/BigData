# Un compte, deux conteneurs : `source` reçoit le dépôt du CHU (identités
# en clair — données synthétiques ici, mais le principe HDS vaut), `lake` la
# copie pseudonymisée et projetée que ClickHouse lit.
resource "azurerm_storage_account" "eds" {
  name                            = "sa${var.prefixe}${random_string.suffixe.result}"
  resource_group_name             = azurerm_resource_group.eds.name
  location                        = azurerm_resource_group.eds.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  # La clé partagée sert à la named collection ClickHouse ; blobfuse et
  # l'upload passent, eux, par l'identité (RBAC).
  shared_access_key_enabled = true
  tags                      = local.etiquettes
}

resource "azurerm_storage_container" "source" {
  name                  = "source"
  storage_account_id    = azurerm_storage_account.eds.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "lake" {
  name                  = "lake"
  storage_account_id    = azurerm_storage_account.eds.id
  container_access_type = "private"
}

# L'opérateur envoie le dépôt source avec `az storage blob upload-batch
# --auth-mode login` : il lui faut le rôle données, pas seulement Owner.
resource "azurerm_role_assignment" "operateur_blob" {
  scope                = azurerm_storage_account.eds.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.actuel.object_id
}

# blobfuse monte `source` et `lake` dans le pod du pipeline avec l'identité
# du kubelet : aucune clé dans le cluster pour ce chemin-là.
resource "azurerm_role_assignment" "kubelet_blob" {
  scope                = azurerm_storage_account.eds.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_kubernetes_cluster.eds.kubelet_identity[0].object_id
}
