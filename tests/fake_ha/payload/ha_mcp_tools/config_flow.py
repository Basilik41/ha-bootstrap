"""Assistant HA-MCP simulé, fidèle aux champs et contrôles de ha-mcp 9.2 (server_credentials)."""
import secrets
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.auth.providers import homeassistant as hap


class Flow(config_entries.ConfigFlow, domain="ha_mcp_tools"):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(entry):
        return Options()

    async def async_step_user(self, user_input=None):
        return self.async_show_menu(step_id="user", menu_options=["server", "tools"])

    async def async_step_tools(self, user_input=None):
        await self.async_set_unique_id("ha_mcp_tools")
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="HA-MCP File & YAML Tools", data={"entry_type": "tools"})
        return self.async_show_form(step_id="tools")

    async def async_step_server(self, user_input=None):
        await self.async_set_unique_id("ha_mcp_tools-server", raise_on_progress=False)
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            username = (user_input.get("admin_username") or "").strip()
            provider = next(p for p in self.hass.auth.auth_providers if p.type == "homeassistant")
            await provider.async_initialize()
            user = None
            for u in await self.hass.auth.async_get_users():
                for c in u.credentials:
                    if c.auth_provider_type == "homeassistant" and c.data.get("username") == username:
                        user = u
            ok = user is not None and user.is_active and user.is_admin and user.local_only
            if ok:
                try:
                    await provider.async_validate_login(username, user_input.get("admin_password") or "")
                except hap.InvalidAuth:
                    ok = False
            if not ok:
                errors["admin_username"] = "invalid_login"
            elif user_input.get("remote_access") not in ("disabled", "ha_auth", "legacy", "none"):
                errors["remote_access"] = "invalid"
            else:
                return self.async_create_entry(title="HA-MCP Server",
                                               data={"entry_type": "server", "webhook_id": secrets.token_hex(16)},
                                               options={"enable_webhook": True, "webhook_auth": user_input["remote_access"],
                                                        "bind_host": user_input.get("bind_host")})
        return self.async_show_form(step_id="server", errors=errors, data_schema=vol.Schema({
            vol.Optional("admin_token"): str, vol.Optional("admin_username"): str, vol.Optional("admin_password"): str,
            vol.Required("remote_access", default="disabled"): str, vol.Required("bind_host", default="127.0.0.1"): str}))


class Options(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        wid = self.config_entry.data["webhook_id"]
        return self.async_show_form(step_id="init", data_schema=vol.Schema({}),
                                    description_placeholders={"connect_url": f"http://10.0.0.5:8123/api/webhook/{wid}",
                                                              "llm_api_docs_url": "https://example.invalid"})
