resource "azurerm_key_vault" "eds" {
  name                       = "kv-${var.prefixe}-${random_string.suffixe.result}"
  resource_group_name        = azurerm_resource_group.eds.name
  location                   = azurerm_resource_group.eds.location
  tenant_id                  = data.azurerm_client_config.actuel.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  # Jetable : sans protection contre la purge, sinon le nom reste bloqué
  # 90 jours après `destroy`.
  purge_protection_enabled   = false
  soft_delete_retention_days = 7
  tags                       = local.etiquettes
}

# Terraform écrit les secrets avec l'identité de l'opérateur.
resource "azurerm_role_assignment" "operateur_secrets" {
  scope                = azurerm_key_vault.eds.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.actuel.object_id
}

# L'addon CSI de l'AKS lit les secrets pour les projeter dans les pods.
resource "azurerm_role_assignment" "csi_secrets" {
  scope                = azurerm_key_vault.eds.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_kubernetes_cluster.eds.key_vault_secrets_provider[0].secret_identity[0].object_id
}

# Un rôle RBAC met jusqu'à une minute à devenir effectif sur le plan de
# données : écrire un secret avant serait refusé (403) de façon aléatoire.
resource "time_sleep" "propagation_rbac" {
  depends_on      = [azurerm_role_assignment.operateur_secrets]
  create_duration = "90s"
}

# ── Secrets générés ─────────────────────────────────────────────────────
# Mots de passe ClickHouse et sel : alphanumériques, pour rester valides
# dans une variable d'environnement, un fichier XML et un GRANT SQL.
resource "random_password" "clickhouse" {
  for_each = toset(["ch-admin-password", "ch-pilotage-password", "ch-recherche-password", "ch-exploitation-password"])
  length   = 32
  special  = false
}

resource "random_password" "sel" {
  length  = 64
  special = false
}

# Metabase exige (complexité « normal ») au moins un chiffre, une minuscule,
# une majuscule et un caractère spécial ; on restreint les spéciaux à ceux
# qui ne posent aucun problème dans un JSON ou un shell.
resource "random_password" "metabase" {
  for_each         = toset(["mb-admin-password", "mb-pilotage-password", "mb-recherche-password"])
  length           = 24
  special          = true
  override_special = "!@#%^*_-+=."
  min_lower        = 1
  min_upper        = 1
  min_numeric      = 1
  min_special      = 1
}

resource "azurerm_key_vault_secret" "clickhouse" {
  for_each     = random_password.clickhouse
  name         = each.key
  value        = each.value.result
  key_vault_id = azurerm_key_vault.eds.id
  depends_on   = [time_sleep.propagation_rbac]
}

resource "azurerm_key_vault_secret" "sel" {
  name         = "eds-pseudo-salt"
  value        = random_password.sel.result
  key_vault_id = azurerm_key_vault.eds.id
  depends_on   = [time_sleep.propagation_rbac]
}

resource "azurerm_key_vault_secret" "metabase" {
  for_each     = random_password.metabase
  name         = each.key
  value        = each.value.result
  key_vault_id = azurerm_key_vault.eds.id
  depends_on   = [time_sleep.propagation_rbac]
}

# La named collection entière est un secret : elle contient la clé du compte.
resource "azurerm_key_vault_secret" "lake_xml" {
  name = "clickhouse-lake-xml"
  value = templatefile("${path.module}/lake.xml.tftpl", {
    compte    = azurerm_storage_account.eds.name
    cle       = azurerm_storage_account.eds.primary_access_key
    conteneur = azurerm_storage_container.lake.name
  })
  key_vault_id = azurerm_key_vault.eds.id
  depends_on   = [time_sleep.propagation_rbac]
}
