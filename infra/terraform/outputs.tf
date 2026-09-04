output "resource_group_name" {
  value = azurerm_resource_group.eds.name
}

output "aks_name" {
  value = azurerm_kubernetes_cluster.eds.name
}

output "acr_name" {
  value = azurerm_container_registry.eds.name
}

output "acr_login_server" {
  value = azurerm_container_registry.eds.login_server
}

output "key_vault_name" {
  value = azurerm_key_vault.eds.name
}

output "storage_account_name" {
  value = azurerm_storage_account.eds.name
}

output "tenant_id" {
  value = data.azurerm_client_config.actuel.tenant_id
}

output "csi_client_id" {
  description = "Identité de l'addon Key Vault CSI, à donner à la SecretProviderClass."
  value       = azurerm_kubernetes_cluster.eds.key_vault_secrets_provider[0].secret_identity[0].client_id
}

output "kubelet_client_id" {
  description = "Identité du kubelet, avec laquelle blobfuse s'authentifie."
  value       = azurerm_kubernetes_cluster.eds.kubelet_identity[0].client_id
}

output "ip_autorisee" {
  value = var.ip_autorisee
}
