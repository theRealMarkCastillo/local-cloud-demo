"""Connected accounts: per-user links to upstream services, stored by Keycloak as link-only identity providers."""
ALIASES = ("upstream-demo",)


def link_params(alias: str) -> dict:
    """Extra authorization parameters that make Keycloak run its account-linking action for this provider."""
    if alias not in ALIASES:
        raise KeyError(alias)
    return {"kc_action": "idp_link:" + alias}


def statuses(linked_accounts: list[dict]) -> list[dict]:
    """Reduce Keycloak's account API answer to the services this app offers."""
    connected = {item.get("providerAlias") for item in linked_accounts if item.get("connected")}
    return [{"alias": alias, "connected": alias in connected} for alias in ALIASES]
