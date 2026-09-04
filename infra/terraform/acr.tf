resource "azurerm_container_registry" "eds" {
  name                = "acr${var.prefixe}${random_string.suffixe.result}"
  resource_group_name = azurerm_resource_group.eds.name
  location            = azurerm_resource_group.eds.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.etiquettes
}

resource "azurerm_role_assignment" "kubelet_acr" {
  scope                = azurerm_container_registry.eds.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.eds.kubelet_identity[0].object_id
}
