"""Marketplace simulée (le catalogue réel data-v2.hacs.xyz n'est pas joignable depuis le banc).

Reproduit les commandes et contrôles de la Marketplace de HA 2026.11 utilisés par ha-bootstrap :
info, warning/accept (par compte), github/connect (assistant de reconfiguration avec code),
repositories/list|add (ajout = GitHub obligatoire), repository/install (avertissement obligatoire,
limite anonyme simulée par HAB_TEST_RATE_LIMIT).
"""
import json, os, shutil, time
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import SOURCE_SYSTEM, SOURCE_RECONFIGURE, ConfigEntryState
from homeassistant.helpers import discovery_flow

DOMAIN = "marketplace"
PAYLOAD = os.environ.get("HAB_TEST_PAYLOAD", "")
FILES = {"homeassistant-ai/ha-mcp": "ha_mcp_tools", "homeassistant-ai/ha-mcp-integration": "ha_mcp_tools",
         "custom-components/remote_homeassistant": "remote_homeassistant"}


def _state(hass):
    p = hass.config.path("marketplace_fake.json")
    if os.path.exists(p):
        return p, json.load(open(p))
    d = {"accepted": [], "rate_limited_once": bool(os.environ.get("HAB_TEST_RATE_LIMIT")),
         "repos": [{"id": "9001", "full_name": "homeassistant-ai/ha-mcp", "category": "integration", "installed": False},
                   {"id": "1001", "full_name": "custom-components/remote_homeassistant", "category": "integration", "installed": False},
                   {"id": "1002", "full_name": "thomasloven/lovelace-card-mod", "category": "plugin", "installed": False}],
         "next": 9100}
    json.dump(d, open(p, "w"))
    return p, d


def _err(conn, mid, code, msg):
    conn.send_error(mid, code, msg)


async def async_setup(hass, config):
    started = time.time()

    def loaded():
        return [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "marketplace/info"})
    @websocket_api.callback
    def info(hass, conn, msg):
        if not loaded():
            return _err(conn, msg["id"], "not_loaded", "not loaded")
        _, d = _state(hass)
        e = loaded()[0]
        conn.send_result(msg["id"], {"stage": "running" if time.time() - started > 4 else "startup",
                                     "disabled_reason": None, "github_connected": bool(e.data.get("token")),
                                     "warning_accepted": conn.user.id in d["accepted"], "categories": ["integration", "plugin"]})

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "marketplace/warning/accept"})
    @websocket_api.callback
    def accept(hass, conn, msg):
        p, d = _state(hass)
        if conn.user.id not in d["accepted"]:
            d["accepted"].append(conn.user.id)
        json.dump(d, open(p, "w"))
        conn.send_result(msg["id"])

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "marketplace/github/connect"})
    @websocket_api.async_response
    async def connect(hass, conn, msg):
        e = loaded()[0]
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": e.entry_id})
        conn.send_result(msg["id"], {"flow_id": r["flow_id"]})

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "marketplace/repositories/list", vol.Optional("categories"): list})
    @websocket_api.callback
    def lst(hass, conn, msg):
        _, d = _state(hass)
        if time.time() - started < 4:
            return conn.send_result(msg["id"], [])
        conn.send_result(msg["id"], [dict(r, installed_version="9.2.0" if r["installed"] else None) for r in d["repos"]])

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "marketplace/repositories/add", vol.Required("repository"): str,
                                      vol.Required("category"): str})
    @websocket_api.callback
    def add(hass, conn, msg):
        p, d = _state(hass)
        if conn.user.id not in d["accepted"]:
            return _err(conn, msg["id"], "warning_not_accepted", "warning")
        if not loaded()[0].data.get("token"):
            return _err(conn, msg["id"], "github_not_connected", "GitHub")
        name = msg["repository"].replace("https://github.com/", "").strip("/")
        if any(r["full_name"] == name for r in d["repos"]):
            return _err(conn, msg["id"], "repository_exists", "exists")
        d["next"] += 1
        d["repos"].append({"id": str(d["next"]), "full_name": name, "category": msg["category"], "installed": False})
        json.dump(d, open(p, "w"))
        conn.send_result(msg["id"])

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "marketplace/repository/install", vol.Required("repository"): str,
                                      vol.Optional("version"): str, vol.Optional("confirm_replace_built_in"): bool})
    @websocket_api.async_response
    async def install(hass, conn, msg):
        p, d = _state(hass)
        if conn.user.id not in d["accepted"]:
            return _err(conn, msg["id"], "warning_not_accepted", "warning")
        if d["rate_limited_once"] and not loaded()[0].data.get("token"):
            return _err(conn, msg["id"], "github_rate_limited", "rate limited")
        r = next((x for x in d["repos"] if x["id"] == msg["repository"]), None)
        if not r or r["full_name"] not in FILES:
            return _err(conn, msg["id"], "repository_not_found", "not found")
        comp = FILES[r["full_name"]]
        dest = hass.config.path("custom_components", comp)
        await hass.async_add_executor_job(lambda: (shutil.rmtree(dest, ignore_errors=True),
                                                   shutil.copytree(os.path.join(PAYLOAD, comp), dest)))
        r["installed"] = True
        json.dump(d, open(p, "w"))
        conn.send_result(msg["id"])

    for c in (info, accept, connect, lst, add, install):
        websocket_api.async_register_command(hass, c)
    if not hass.config_entries.async_entries(DOMAIN):
        discovery_flow.async_create_flow(hass, DOMAIN, context={"source": SOURCE_SYSTEM}, data={})
    return True


async def async_setup_entry(hass, entry):
    return True


async def async_unload_entry(hass, entry):
    return True
