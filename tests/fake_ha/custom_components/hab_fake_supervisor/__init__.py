"""Supervisor simulé pour le banc de test de ha-bootstrap.

Reproduit les points d'API que le script utilise (via la commande WebSocket
`supervisor/api` du vrai Home Assistant OS) et l'ingress du File editor.
L'état est gardé dans <config>/hab_fake_supervisor.json (survit aux redémarrages).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.request

import voluptuous as vol
from aiohttp import web

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

DOMAIN = "hab_fake_supervisor"
PAYLOAD = os.environ.get("HAB_TEST_PAYLOAD", "")
CLOUD = os.environ.get("HAB_TEST_CLOUD", "")
TMP = os.environ.get("HAB_TEST_TMP", "/tmp/hab_fake_tmp")

REPOS = {
    "core": {"source": "core", "addons": {"configurator": "File editor"}},
    "https://github.com/hacs/addons": {"addons": {"get": "Get HACS"}},
    "https://github.com/homeassistant-apps/repository": {"addons": {"cloudflared": "Cloudflared"}},
    "https://github.com/brenner-tobias/ha-addons": {"addons": {"cloudflared": "Cloudflared"}},
}
DEFAULT_OPTIONS = {"cloudflared": {"external_hostname": "", "additional_hosts": []},
                   "configurator": {"dirsfirst": False, "enforce_basepath": True},
                   "get": {}}
CLOUDFLARED_KEYS = {"external_hostname", "additional_hosts", "tunnel_name", "catch_all_service",
                    "nginx_proxy_manager", "tunnel_token", "post_quantum", "run_parameters", "log_level"}


def repo_slug(url: str) -> str:
    return "core" if url == "core" else hashlib.sha1(url.lower().encode()).hexdigest()[:8]


class State:
    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.path = hass.config.path("hab_fake_supervisor.json")
        self.data = {"repos": ["core"], "addons": {}, "sessions": [], "calls": []}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.data = json.load(f)

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=1)


def addon_base(slug: str) -> str:
    return slug.split("_", 1)[1] if "_" in slug else slug


def catalog(st: State):
    out = {}
    for url in st.data["repos"]:
        rs = repo_slug(url)
        for base, name in REPOS[url]["addons"].items():
            slug = f"{rs}_{base}"
            out[slug] = {"slug": slug, "name": name, "repository": rs, "base": base}
    return out


class ApiError(Exception):
    pass


async def handle(hass: HomeAssistant, st: State, method: str, endpoint: str, data: dict):
    st.data["calls"].append([method, endpoint])
    cat = catalog(st)
    ad = st.data["addons"]
    def flag(name):
        return os.path.exists(hass.config.path(f"hab_update_{name}"))

    async def done_and_restart(name, restart):
        await hass.async_add_executor_job(os.remove, hass.config.path(f"hab_update_{name}"))
        if restart:
            async def later():
                await asyncio.sleep(1)
                await hass.services.async_call("homeassistant", "restart")
            hass.async_create_task(later())
        return {}
    if endpoint == "/supervisor/info":
        up = flag("supervisor")
        return {"version": "2026.11.1" if up else "2026.11.2", "version_latest": "2026.11.2", "update_available": up}
    if endpoint == "/supervisor/ping":
        return {}
    if endpoint in ("/core/info", "/os/info"):
        n = endpoint.split("/")[1]
        return {"version": "16.2" if flag(n) else "16.3", "version_latest": "16.3", "update_available": flag(n)}
    if endpoint == "/supervisor/update":
        return await done_and_restart("supervisor", False)
    if endpoint in ("/core/update", "/os/update"):
        return await done_and_restart(endpoint.split("/")[1], True)
    if endpoint == "/store" and method == "GET":
        return {"addons": [{"slug": s, "name": a["name"], "repository": a["repository"], "installed": s in ad}
                           for s, a in cat.items()],
                "repositories": [{"slug": repo_slug(u), "source": u, "url": u} for u in st.data["repos"]]}
    if endpoint == "/store/repositories" and method == "POST":
        url = data["repository"]
        if url in st.data["repos"]:
            raise ApiError(f"Can't add {url}, already exists")
        if url not in REPOS:
            raise ApiError(f"{url} is not a valid app repository")
        st.data["repos"].append(url)
        return {}
    m = re.match(r"^/(?:store/)?addons/([^/]+)/(\w+)$", endpoint)
    if not m:
        raise ApiError(f"endpoint simulé inconnu : {method} {endpoint}")
    slug, action = m.groups()
    if slug not in cat:
        raise ApiError(f"App {slug} does not exist in the store")
    base = cat[slug]["base"]
    if action == "install":
        if slug in ad:
            raise ApiError("already installed")
        ad[slug] = {"version": "1.0.0", "state": "stopped", "options": dict(DEFAULT_OPTIONS.get(base, {})),
                    "boot": "manual", "watchdog": False}
        return {"job_id": "job1"} if data.get("background") else {}
    if action == "info":
        if slug not in ad:
            return {"slug": slug, "name": cat[slug]["name"], "version": None, "state": "unknown", "options": {}}
        info = dict(ad[slug], slug=slug, name=cat[slug]["name"])
        if base == "configurator":
            info["ingress_entry"] = f"/api/hassio_ingress/tok_{slug}"
        return info
    if slug not in ad:
        raise ApiError(f"App {slug} is not installed")
    a = ad[slug]
    if action == "options":
        if "options" in data:
            if base == "cloudflared" and set(data["options"]) - CLOUDFLARED_KEYS:
                raise ApiError(f"options invalides : {set(data['options']) - CLOUDFLARED_KEYS}")
            a["options"] = data["options"]
        if "boot" in data:
            a["boot"] = data["boot"]
        if "watchdog" in data:
            a["watchdog"] = data["watchdog"]
        return {}
    if action in ("start", "restart"):
        a["state"] = "started"
        if base == "get":
            dest = hass.config.path("custom_components", "hacs")
            await hass.async_add_executor_job(
                lambda: (shutil.rmtree(dest, ignore_errors=True), shutil.copytree(os.path.join(PAYLOAD, "hacs"), dest)))

            async def stop_later():
                await asyncio.sleep(2)
                a["state"] = "stopped"
                st.save()
            hass.async_create_task(stop_later())
        if base == "cloudflared" and a["options"].get("tunnel_token"):
            body = json.dumps({"token": a["options"]["tunnel_token"]}).encode()

            def notify():
                req = urllib.request.Request(CLOUD + "/test/tunnel_up", data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10).read()
            await hass.async_add_executor_job(notify)
        return {}
    if action == "stop":
        a["state"] = "stopped"
        return {}
    raise ApiError(f"action simulée inconnue : {action}")


async def async_setup(hass: HomeAssistant, config) -> bool:
    st = State(hass)
    os.makedirs(TMP, exist_ok=True)

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "supervisor/api", vol.Required("endpoint"): str,
                                      vol.Required("method"): str, vol.Optional("data"): dict,
                                      vol.Optional("params"): dict, vol.Optional("timeout"): vol.Any(int, float, None)})
    @websocket_api.async_response
    async def ws_api(hass, connection, msg):
        try:
            if msg["endpoint"] == "/ingress/session":
                sess = secrets.token_hex(16)
                st.data["sessions"].append(sess)
                res = {"session": sess}
            else:
                res = await handle(hass, st, msg["method"].upper(), msg["endpoint"], msg.get("data") or {})
            st.save()
            connection.send_result(msg["id"], res)
        except ApiError as e:
            st.save()
            connection.send_error(msg["id"], "unknown_error", str(e))

    websocket_api.async_register_command(hass, ws_api)

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): "hab_test/zeroconf", vol.Required("host"): str,
                                      vol.Required("port"): int, vol.Required("uuid"): str, vol.Required("name"): str})
    @websocket_api.async_response
    async def ws_zeroconf(hass, connection, msg):
        """Simule l'annonce mDNS d'une autre instance (le banc n'a pas de multicast)."""
        from ipaddress import ip_address
        from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
        info = ZeroconfServiceInfo(ip_address=ip_address(msg["host"]), ip_addresses=[ip_address(msg["host"])],
                                   port=msg["port"], hostname="grange.local.", type="_home-assistant._tcp.local.",
                                   name=f"{msg['name']}._home-assistant._tcp.local.",
                                   properties={"uuid": msg["uuid"], "location_name": msg["name"],
                                               "internal_url": f"http://{msg['host']}:{msg['port']}",
                                               "base_url": f"http://{msg['host']}:{msg['port']}"})
        r = await hass.config_entries.flow.async_init("remote_homeassistant", context={"source": "zeroconf"}, data=info)
        connection.send_result(msg["id"], {"type": r.get("type"), "flow_id": r.get("flow_id")})

    websocket_api.async_register_command(hass, ws_zeroconf)
    hass.http.register_view(FakeIngress(hass, st))
    return True


class FakeIngress(HomeAssistantView):
    """Ingress du File editor : /api/save et /api/exec_command, système de fichiers « traduit »."""

    url = "/api/hassio_ingress/{token}/{path:.*}"
    name = "api:hab_fake:ingress"
    requires_auth = False

    def __init__(self, hass, st):
        self.hass = hass
        self.st = st

    def _map(self, text: str) -> str:
        return text.replace("/tmp/", TMP + "/").replace("/homeassistant", self.hass.config.config_dir)

    async def post(self, request: web.Request, token: str, path: str):
        sess = request.cookies.get("ingress_session")
        if sess not in self.st.data["sessions"]:
            return web.Response(status=401, text="401: Unauthorized")
        slug = token.removeprefix("tok_")
        if self.st.data["addons"].get(slug, {}).get("state") != "started":
            return web.Response(status=502, text="502: app arrêtée")
        form = await request.post()
        if path == "api/save":
            fn = self._map(form["filename"])
            text = form["text"]
            if fn.endswith("hab_helper.py"):
                text = self._map(text)
            await self.hass.async_add_executor_job(lambda: open(fn, "w").write(text))
            return web.json_response({"error": False, "message": "File saved successfully", "file": fn})
        if path == "api/exec_command":
            cmd = self._map(form["command"]).split()

            def run():
                return subprocess.run(cmd, capture_output=True, text=True, timeout=int(form.get("timeout", 15)))
            r = await self.hass.async_add_executor_job(run)
            return web.json_response({"error": False, "returncode": r.returncode, "stdout": r.stdout,
                                      "stderr": r.stderr})
        return web.Response(status=404)
