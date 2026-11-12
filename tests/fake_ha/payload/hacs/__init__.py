"""HACS simulé : catalogue, ajout de dépôt, téléchargement (copie des intégrations du banc)."""
import json, os, shutil, time
import voluptuous as vol
from homeassistant.components import websocket_api

DOMAIN = "hacs"
PAYLOAD = os.path.dirname(os.path.dirname(__file__)) if False else os.environ.get("HAB_TEST_PAYLOAD", "")
KNOWN = {"homeassistant-ai/ha-mcp-integration": "ha_mcp_tools",
         "custom-components/remote_homeassistant": "remote_homeassistant"}


def _state(hass):
    p = hass.config.path("hacs_fake.json")
    if os.path.exists(p):
        return p, json.load(open(p))
    data = {"repos": [{"id": "1001", "full_name": "custom-components/remote_homeassistant", "category": "integration", "installed": False},
                      {"id": "1002", "full_name": "thomasloven/lovelace-card-mod", "category": "plugin", "installed": False}],
            "next": 2000}
    json.dump(data, open(p, "w"))
    return p, data


async def async_setup(hass, config):
    return True


async def async_setup_entry(hass, entry):
    started = time.time()
    if hass.data.get(DOMAIN):
        return True
    hass.data[DOMAIN] = True

    @websocket_api.websocket_command({vol.Required("type"): "hacs/info"})
    @websocket_api.callback
    def info(hass, conn, msg):
        conn.send_result(msg["id"], {"stage": "running" if time.time() - started > 3 else "setup"})

    @websocket_api.websocket_command({vol.Required("type"): "hacs/repositories/list", vol.Optional("categories"): list})
    @websocket_api.callback
    def lst(hass, conn, msg):
        _, d = _state(hass)
        conn.send_result(msg["id"], [dict(r, installed_version="1.0" if r["installed"] else None) for r in d["repos"]])

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "hacs/repositories/add", vol.Required("repository"): str,
                                      vol.Required("category"): str})
    @websocket_api.callback
    def add(hass, conn, msg):
        p, d = _state(hass)
        name = msg["repository"].replace("https://github.com/", "").strip("/")
        if name in KNOWN and not any(r["full_name"] == name for r in d["repos"]):
            d["next"] += 1
            d["repos"].append({"id": str(d["next"]), "full_name": name, "category": msg["category"], "installed": False})
            json.dump(d, open(p, "w"))
        conn.send_result(msg["id"], {})

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "hacs/repository/download", vol.Required("repository"): str,
                                      vol.Optional("version"): str})
    @websocket_api.async_response
    async def dl(hass, conn, msg):
        p, d = _state(hass)
        r = next((x for x in d["repos"] if x["id"] == msg["repository"]), None)
        if not r or r["full_name"] not in KNOWN:
            conn.send_error(msg["id"], "error", "dépôt inconnu du banc")
            return
        comp = KNOWN[r["full_name"]]
        dest = hass.config.path("custom_components", comp)
        await hass.async_add_executor_job(lambda: (shutil.rmtree(dest, ignore_errors=True),
                                                   shutil.copytree(os.path.join(PAYLOAD, comp), dest)))
        r["installed"] = True
        json.dump(d, open(p, "w"))
        conn.send_result(msg["id"], {})

    for c in (info, lst, add, dl):
        websocket_api.async_register_command(hass, c)
    return True
