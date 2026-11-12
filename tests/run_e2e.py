"""Banc de bout en bout de ha-bootstrap.

Vrai Home Assistant Core (venv HAB_HASS) + Supervisor/HACS/HA-MCP simulés + faux Cloudflare/GitHub
+ vraie intégration Accès distant (dépôt cloné) + faux `qm`.
Usage : python3 tests/run_e2e.py <hass> <dépôt acces-distant>
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = "/tmp/hab_e2e"
CFG = os.path.join(WORK, "ha")
CONF = os.path.join(WORK, "conf")
CLOUD_PORT = 18080
CLOUD = f"http://127.0.0.1:{CLOUD_PORT}"
IP = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()[0]
HA_URL = f"http://{IP}:8123"
PWD = "MotDePasse-Test-2026"

sys.path.insert(0, ROOT)
import ha_bootstrap as hb  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✔ " if cond else "  ✖ ") + name + (f" — {detail}" if detail and not cond else ""), flush=True)


def direct(method, url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def cloud(path="/test/state", body=None, method=None):
    return direct(method or ("POST" if body is not None else "GET"), CLOUD + path, body)


ENV_BASE = dict(os.environ, NO_PROXY=f"127.0.0.1,localhost,{IP}", no_proxy=f"127.0.0.1,localhost,{IP}")


class HARunner:
    def __init__(self, hass_bin):
        self.hass_bin = hass_bin
        self.proc = None
        self.stop = False
        self.t = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        env = dict(ENV_BASE, HAB_TEST_PAYLOAD=os.path.join(HERE, "fake_ha", "payload"), HAB_TEST_CLOUD=CLOUD,
                   HAB_TEST_TMP=os.path.join(WORK, "addon_tmp"), HAB_TEST_RATE_LIMIT="1")
        log = open(os.path.join(WORK, "hass.out"), "a")
        while not self.stop:
            self.proc = subprocess.Popen([self.hass_bin, "-c", CFG, "--ignore-os-check"], env=env,
                                         stdout=log, stderr=subprocess.STDOUT)
            rc = self.proc.wait()
            log.write(f"\n### hass exit {rc}\n")
            log.flush()
            if rc != 100:
                break

    def start(self):
        self.t.start()

    def shutdown(self):
        self.stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(60)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def run_script(name, answers, extra_env=None, expect_rc=0):
    path = os.path.join(WORK, f"answers_{name}.json")
    json.dump(answers, open(path, "w"))
    env = dict(ENV_BASE, HAB_ANSWERS=path, HAB_CONF_DIR=CONF, HAB_CF_API=CLOUD + "/client/v4",
               HAB_GITHUB_API=CLOUD + "/github",
               HAB_PUBLIC_CHECK_URL=CLOUD + "/public/{host}/manifest.json",
               HAB_TEST_VM_IP=IP, HAB_TEST_QM_LOG=os.path.join(WORK, "qm.log"),
               PATH=os.path.join(HERE, "bin") + ":" + os.environ["PATH"])
    env.update(extra_env or {})
    t0 = time.time()
    r = subprocess.run([os.environ.get("HAB_PY", sys.executable), "-I", os.path.join(ROOT, "ha_bootstrap.py")], env=env,
                       capture_output=True, text=True, timeout=1800)
    out = r.stdout + r.stderr
    open(os.path.join(WORK, f"out_{name}.txt"), "w").write(out)
    print(f"\n── scénario {name} : code {r.returncode} en {int(time.time() - t0)} s", flush=True)
    check(f"[{name}] code de sortie {expect_rc}", r.returncode == expect_rc, out[-2500:])
    left = {k: v for k, v in json.load(open(path)).items() if isinstance(v, list) and v}
    return out


def ha_session():
    ha = hb.HA(HA_URL, hb.UI.__new__(hb.UI))
    ha.ui.scripted = True
    ha.ui.answers = {}
    ha.ui.asked = []
    assert ha.login("richard", PWD) is None
    return ha


def main():
    hass_bin, ad_repo = sys.argv[1], sys.argv[2]
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(os.path.join(CFG, "custom_components"))
    os.makedirs(os.path.join(WORK, "addon_tmp"))
    for comp in ("hab_fake_supervisor", "marketplace"):
        shutil.copytree(os.path.join(HERE, "fake_ha", "custom_components", comp),
                        os.path.join(CFG, "custom_components", comp))
    open(os.path.join(CFG, "configuration.yaml"), "w").write(
        "api:\nconfig:\nonboarding:\nperson:\nwebhook:\nwebsocket_api:\nhab_fake_supervisor:\n"
        "logger:\n  default: warning\n")
    cloud_p = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_cloud.py"), str(CLOUD_PORT), ad_repo],
                               env=ENV_BASE, stdout=open(os.path.join(WORK, "cloud.out"), "w"), stderr=subprocess.STDOUT)
    runner = HARunner(hass_bin)
    runner.start()
    try:
        for _ in range(240):
            try:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(HA_URL + "/api/onboarding", timeout=3)
                break
            except Exception:
                time.sleep(1)
        scenario_fresh()
        scenario_rerun()
        scenario_failures()
    finally:
        runner.shutdown()
        cloud_p.terminate()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n════ {len(RESULTS) - len(bad)}/{len(RESULTS)} vérifications OK ════")
    for n, _, d in bad:
        print(f"  ✖ {n}\n     {d[:600]}")
    sys.exit(1 if bad else 0)


# ───────────────────────── scénario 1 : instance neuve, profil Centrale, tout ─────────────────────────

def scenario_fresh():
    answers = {
        "target": "105", "snapshot_before": True,
        "owner_intro": "ok", "owner_name": "Richard", "owner_username": "richard",
        "owner_password": PWD, "owner_password2": PWD,
        "profile": "centrale", "instance_name": "Centrale",
        "steps": ["general", "updates", "users", "hacs", "ha_mcp", "acces_distant", "remote_ha", "cloudflare"],
        "coords": "47.6012, 1.3335",
        "people.add": [True, False], "people.name": "Julie", "people.username": "julie",
        "people.admin": False, "people.remote": True,
        "store_warning": True, "github_code": "ok",
        "github_token": ["mauvais-jeton", "gh-good-token"], "remember.github_token": True,
        "cf_token": "cf-good-token", "remember.cloudflare_token": True,
        "host": "ha-centrale.degest.test",
        "waf_confirm": True,
        "summary": "ok", "secrets": "ok", "snapshot_after": True,
    }
    out = run_script("neuve", answers)
    check("[neuve] jeton GitHub faux refusé puis bon accepté", "GitHub refuse ce jeton" in out)
    check("[neuve] limite GitHub anonyme → Marketplace reliée à GitHub par code", "limite les téléchargements anonymes" in out
          and "Code GitHub : WXYZ-9876" in out and "GitHub relié à la Marketplace" in out)
    mk = json.load(open(os.path.join(CFG, "marketplace_fake.json")))
    check("[neuve] ha-mcp pris dans le catalogue par défaut (pas de dépôt ajouté)",
          [r["full_name"] for r in mk["repos"] if r["installed"]] == ["homeassistant-ai/ha-mcp", "custom-components/remote_homeassistant"]
          and len(mk["repos"]) == 3, json.dumps(mk["repos"]))
    ha = ha_session()
    users = {u["username"]: u for u in ha.call("config/auth/list") if u.get("username")}
    check("[neuve] comptes richard/julie/claude/ha-mcp", set(users) >= {"richard", "julie", "claude", "ha-mcp"}, str(list(users)))
    check("[neuve] richard propriétaire admin", users["richard"]["is_owner"] and "system-admin" in users["richard"]["group_ids"])
    check("[neuve] julie non admin", users["julie"]["group_ids"] == ["system-users"])
    check("[neuve] claude admin, accès distant permis", "system-admin" in users["claude"]["group_ids"] and not users["claude"]["local_only"])
    check("[neuve] ha-mcp admin local seulement", "system-admin" in users["ha-mcp"]["group_ids"] and users["ha-mcp"]["local_only"])
    persons = ha.call("person/list")
    names = sorted(p["name"] for p in persons["storage"])
    check("[neuve] personnes Richard et Julie seulement", names == ["Julie", "Richard"], str(names))
    cfg = ha.config()
    check("[neuve] réglages généraux", cfg["location_name"] == "Centrale" and cfg["country"] == "FR"
          and cfg["currency"] == "EUR" and cfg["time_zone"] == "Europe/Paris" and cfg["unit_system"]["temperature"] == "°C"
          and abs(cfg["latitude"] - 47.6012) < 1e-4, json.dumps({k: cfg.get(k) for k in ("location_name", "country", "currency", "time_zone", "latitude")}))
    check("[neuve] adresses interne/externe", cfg.get("external_url") == "https://ha-centrale.degest.test"
          and cfg.get("internal_url") == HA_URL, f"{cfg.get('external_url')} {cfg.get('internal_url')}")
    onb = hb.http("GET", HA_URL + "/api/onboarding", local=True)[0]
    check("[neuve] écran d'accueil terminé", onb == 404, str(onb))
    httpc = ha.call("http/config")
    st = httpc["stable"]
    check("[neuve] proxy Cloudflared autorisé et confirmé (stable, rien en attente)",
          st.get("use_x_forwarded_for") and "172.30.33.0/24" in st.get("trusted_proxies", []) and not httpc.get("pending"),
          json.dumps(httpc)[:400])
    for dom in ("marketplace", "ha_mcp_tools", "acces_distant"):
        e = ha.entries(dom)
        check(f"[neuve] intégration {dom} configurée et chargée", e and e[0]["state"] == "loaded", json.dumps(e)[:300])
    mcp = ha.entries("ha_mcp_tools")
    check("[neuve] HA-MCP : serveur + outils fichiers/YAML", sorted(e["title"] for e in mcp) == ["HA-MCP File & YAML Tools", "HA-MCP Server"],
          json.dumps(mcp)[:300])
    ad = ha.entries("acces_distant")
    check("[neuve] Accès distant en mode réglages", ad and ad[0]["title"] == "Réglages (sans certificats)")
    mf = os.path.join(CFG, "custom_components", "acces_distant", "manifest.json")
    check("[neuve] Accès distant 1.7.0 (vraie intégration) posée",
          os.path.exists(mf) and json.load(open(mf))["version"] == "1.7.0")
    check("[neuve] Remote Home Assistant posé", os.path.isdir(os.path.join(CFG, "custom_components", "remote_homeassistant")))
    check("[neuve] fichiers temporaires du File editor effacés",
          not any(f.startswith("hab_") for f in os.listdir(os.path.join(WORK, "addon_tmp"))), str(os.listdir(os.path.join(WORK, "addon_tmp"))))
    check("[neuve] aucun dossier de travail .hab_ laissé", not [d for d in os.listdir(CFG) if d.startswith(".hab_")])
    sup = json.load(open(os.path.join(CFG, "hab_fake_supervisor.json")))
    cfd = next((v for k, v in sup["addons"].items() if k.endswith("_cloudflared")), None)
    s = cloud()
    tun = list(s["tunnels"].values())
    check("[neuve] un tunnel, sain, nommé d'après l'hôte", len(tun) == 1 and tun[0]["status"] == "healthy"
          and tun[0]["public"]["name"] == "ha-centrale.degest.test", json.dumps(tun)[:300])
    check("[neuve] tunnel → http://homeassistant:8123 puis 404", tun and tun[0]["config"]["ingress"][0] == {
        "hostname": "ha-centrale.degest.test", "service": "http://homeassistant:8123", "originRequest": {}}
          and tun[0]["config"]["ingress"][-1] == {"service": "http_status:404"})
    check("[neuve] app Cloudflared : jeton du tunnel, démarrage auto, surveillance",
          cfd and cfd["options"].get("tunnel_token") == tun[0]["token"] and cfd["state"] == "started"
          and cfd["boot"] == "auto" and cfd["watchdog"] is True and cfd["options"].get("additional_hosts") == [],
          json.dumps(cfd)[:300])
    check("[neuve] DNS CNAME proxifié vers le tunnel", len(s["dns"]) == 1 and s["dns"][0]["type"] == "CNAME"
          and s["dns"][0]["content"] == f"{tun[0]['public']['id']}.cfargotunnel.com" and s["dns"][0]["proxied"] is True)
    check("[neuve] certificat client demandé sur les deux hôtes", s["mtls"] == ["ha.degest.test", "ha-centrale.degest.test"], str(s["mtls"]))
    rules = s["ruleset"]["rules"]
    order = [r["description"] for r in rules]
    check("[neuve] ordre des règles : modèle, nouvelle mTLS, nouvelle Claude, puis les anciennes",
          order == ["Ha- externe", "Ha- externe ha-centrale.degest.test — ha-bootstrap",
                    "HA-MCP - Claude ha-centrale.degest.test — ha-bootstrap", "HA-MCP - Claude", "sentinel", "Geo Block"], str(order))
    from fake_cloud import fresh_state  # noqa
    orig = {r["id"]: r for r in fresh_state()["ruleset"]["rules"]}
    check("[neuve] règles d'origine inchangées", all(r == orig[r["id"]] for r in rules if r["id"] in orig))
    nb, ns = rules[1], rules[2]
    conf = json.load(open(os.path.join(CONF, "config.json")))
    wpath = conf.get("webhook.centrale")
    check("[neuve] chemin webhook HA-MCP retrouvé", wpath and wpath.startswith("/api/webhook/") and len(wpath) > 30, str(wpath))
    check("[neuve] règle mTLS : hôte, SKI, /auth/token, webhook de la centrale, pas celui de la Grange",
          'http.host eq "ha-centrale.degest.test"' in nb["expression"] and "9D9721B71DD6781A5DBBF7421E38392D9FB9402F" in nb["expression"]
          and '"/auth/token"' in nb["expression"] and f'eq "{wpath}"' in nb["expression"]
          and "grangeWEBHOOK" not in nb["expression"] and "ha.degest.test" not in nb["expression"].replace("ha-centrale.degest.test", ""),
          nb["expression"])
    check("[neuve] règle Claude : copie de l'action skip (Super Bot Fight) et du journal",
          ns["action"] == "skip" and ns["action_parameters"] == {"ruleset": "current", "phases": ["http_request_sbfm"]}
          and ns.get("logging") == {"enabled": True} and 'http.host eq "ha-centrale.degest.test"' in ns["expression"])
    check("[neuve] contrôle public : refusé sans certificat", "refusé sans certificat" in out)
    backups = os.listdir(os.path.join(CONF, "backups"))
    check("[neuve] sauvegarde des règles WAF avant modification", len(backups) == 1 and
          len(json.load(open(os.path.join(CONF, "backups", backups[0])))["rules"]) == 4)
    sec_path = os.path.join(CONF, "secrets.json")
    sec = json.load(open(sec_path))
    check("[neuve] jetons gardés (fichier 600)", sec.get("cloudflare_token") == "cf-good-token" and sec.get("github_token") == "gh-good-token"
          and stat.S_IMODE(os.stat(sec_path).st_mode) == 0o600)
    leaks = []
    for d in ("logs", "rapports"):
        for f in os.listdir(os.path.join(CONF, d)):
            txt = open(os.path.join(CONF, d, f)).read()
            for secret in ("cf-good-token", "gh-good-token", PWD, tun[0]["token"]):
                if secret in txt:
                    leaks.append(f"{d}/{f}: {secret[:6]}")
    check("[neuve] aucun secret dans journaux et rapports", not leaks, str(leaks))
    check("[neuve] mots de passe de julie et claude affichés à la fin (pas celui du compte de service)",
          out.count("(identifiant julie)") >= 1 and "(identifiant claude)" in out and "(identifiant ha-mcp)" not in out)
    qm = open(os.path.join(WORK, "qm.log")).read()
    check("[neuve] instantanés Proxmox avant et après", "snapshot 105 hab_avant_" in qm and "snapshot 105 hab_socle_" in qm, qm)
    check("[neuve] URL du connecteur Claude donnée", f"https://ha-centrale.degest.test{wpath}" in out)
    toks = [t for t in ha.call("auth/refresh_tokens") if t.get("client_id") == HA_URL + "/"]
    check("[neuve] session du script supprimée à la fin (reste celle du banc)", len(toks) == 1, json.dumps(toks)[:300])
    ha.ws_close()


# ───────────────────────── scénario 2 : relance, rien ne doit bouger ─────────────────────────

def scenario_rerun():
    before = cloud()
    sup_before = json.load(open(os.path.join(CFG, "hab_fake_supervisor.json")))["addons"]
    answers = {
        "target": "105", "snapshot_before": False,
        "login_password": PWD,
        "profile": "centrale", "people.add": False,
        "summary": "ok", "snapshot_after": False,
    }
    out = run_script("relance", answers)
    after = cloud()
    check("[relance] aucune nouvelle règle, tunnel ou DNS", len(after["ruleset"]["rules"]) == 6 and len(after["tunnels"]) == 1
          and len(after["dns"]) == 1 and after["ruleset"] == before["ruleset"])
    check("[relance] « règles WAF déjà en place »", "règles WAF déjà en place" in out)
    check("[relance] pas de redémarrage inutile", "aucun redémarrage nécessaire" in out)
    check("[relance] comptes déjà là", "créés : aucun" in out)
    check("[relance] Accès distant déjà à jour", "déjà à jour" in out)
    check("[relance] pas de nouveau code GitHub", "Code GitHub" not in out)
    check("[relance] aucune question de jeton (gardés)", "Colle le jeton" not in out)
    check("[relance] aucun mot de passe affiché", "affichés une seule fois" not in out)
    sup_after = json.load(open(os.path.join(CFG, "hab_fake_supervisor.json")))["addons"]
    check("[relance] apps inchangées", sup_after == sup_before)
    ha = ha_session()
    check("[relance] pas de doublon de personne", len(ha.call("person/list")["storage"]) == 2)
    ha.ws_close()


# ───────────────────────── scénario 3 : cas d'échec ─────────────────────────

def scenario_failures():
    # a) mot de passe faux puis bon ; jeton Cloudflare sans permission WAF → arrêt propre, rien touché
    cloud("/test/set", {"missing": ["waf"]})
    before = cloud()
    answers = {"target": "105", "snapshot_before": False, "login_password": ["faux-mot-de-passe", PWD],
               "login_username": ["richard", "richard"],
               "profile": "centrale", "steps": ["cloudflare"], "error.cloudflare": "skip",
               "summary": "ok", "snapshot_after": False}
    out = run_script("permission", answers)
    check("[échec] mot de passe faux signalé puis connexion", "Connexion refusée" in out and "Connecté en tant que Richard" in out)
    check("[échec] permission WAF manquante nommée", "Zone · WAF de zone" in out, out[-1500:])
    after = cloud()
    check("[échec] rien modifié chez Cloudflare", after["ruleset"] == before["ruleset"] and after["mtls"] == before["mtls"]
          and len(after["tunnels"]) == 1)
    cloud("/test/set", {"missing": []})

    # b) sous-domaine déjà pris par un autre enregistrement → on ne l'écrase pas
    cloud("/test/set", {"dns": before["dns"] + [{"id": "dns-x", "name": "ha-autre.degest.test", "type": "A", "content": "1.2.3.4", "proxied": False}]})
    answers = {"target": "105", "snapshot_before": False, "login_password": PWD, "profile": "perso",
               "instance_name": "Autre", "steps": ["cloudflare"], "host": "ha-autre.degest.test",
               "waf_confirm": True, "dns_replace": False, "summary": "ok", "snapshot_after": False}
    out = run_script("dns", answers)
    s = cloud()
    rec = next(r for r in s["dns"] if r["name"] == "ha-autre.degest.test")
    check("[échec] DNS existant conservé", rec["type"] == "A" and rec["content"] == "1.2.3.4")
    check("[échec] étape passée avec explication", "déjà utilisé" in out)
    check("[échec] pas de tunnel créé pour l'hôte refusé", len(s["tunnels"]) == 1)

    # c) limite de règles de l'offre atteinte → message clair, rien de cassé
    cloud("/test/set", {"rule_limit": len(s["ruleset"]["rules"])})
    answers = {"target": "105", "snapshot_before": False, "login_password": PWD, "profile": "perso",
               "instance_name": "Troisieme", "steps": ["cloudflare"], "host": "ha-troisieme.degest.test",
               "waf_confirm": True, "error.cloudflare": "skip", "summary": "ok", "snapshot_after": False}
    out = run_script("limite", answers)
    check("[échec] limite de règles expliquée", "limite de ton offre" in out, out[-1200:])
    check("[échec] pas de tunnel ouvert sans règle", len(cloud()["tunnels"]) == 1)
    cloud("/test/set", {"rule_limit": 10})

    # d) chez un ami : zone sans aucune règle, offre sans Super Bot Fight, certificats créés par le script
    cloud("/test/set", {"ruleset": None, "mtls": [], "no_sbfm": True})
    answers = {"target": "105", "snapshot_before": False, "login_password": PWD, "profile": "principal",
               "instance_name": "Chez Paul", "steps": ["ha_mcp", "cloudflare"], "host": "ha-paul.degest.test",
               "waf_confirm": True, "cert.add": [True, True, False], "cert.name": ["telephone-paul", "pc-paul"],
               "summary": "ok", "secrets": "ok", "snapshot_after": False}
    out = run_script("ami", answers)
    s = cloud()
    rules = (s["ruleset"] or {}).get("rules") or []
    conf = json.load(open(os.path.join(CONF, "config.json")))
    wpath = conf.get("webhook.chez-paul")
    check("[ami] deux règles créées de zéro (certificat obligatoire, puis Claude)",
          [r["description"] for r in rules] == ["Certificat obligatoire ha-paul.degest.test — ha-bootstrap",
                                                "Claude ha-paul.degest.test — ha-bootstrap"], json.dumps(rules)[:400])
    b = rules[0]["expression"] if rules else ""
    check("[ami] règle générée : certificat vérifié et non révoqué, /auth/token, webhook de l'instance",
          'http.host eq "ha-paul.degest.test"' in b and "cf.tls_client_auth.cert_verified and not cf.tls_client_auth.cert_revoked" in b
          and '"/auth/token"' in b and wpath and f'eq "{wpath}"' in b, b)
    check("[ami] offre sans Super Bot Fight : saut des seules règles personnalisées",
          len(rules) == 2 and rules[1]["action_parameters"] == {"ruleset": "current"})
    check("[ami] certificat client demandé sur l'hôte", s["mtls"] == ["ha-paul.degest.test"])
    check("[ami] contrôle : refusé sans certificat", "refusé sans certificat" in out)
    d = os.path.join(CONF, "certificats", "ha-paul.degest.test")
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    check("[ami] deux fichiers .p12, aucune clé laissée en clair", files == ["pc-paul.crt", "pc-paul.p12", "telephone-paul.crt",
                                                                             "telephone-paul.p12"], str(files))
    import re as _re
    ok_p12 = True
    for n in ("telephone-paul", "pc-paul"):
        m = _re.search(rf"Certificat {n} \([^)]*\) :\n\s+(\S+)", out)
        f = os.path.join(d, f"{n}.p12")
        r = subprocess.run(["openssl", "pkcs12", "-in", f, "-passin", f"pass:{m.group(1) if m else 'x'}", "-nokeys"],
                           capture_output=True, text=True) if os.path.exists(f) else None
        ok_p12 &= bool(m and r and r.returncode == 0 and "BEGIN CERTIFICATE" in r.stdout
                       and stat.S_IMODE(os.stat(f).st_mode) == 0o600)
    check("[ami] .p12 lisibles avec le mot de passe affiché (600)", ok_p12)
    check("[ami] certificats signés par l'autorité Cloudflare", len(s["client_certs"]) == 2)
    leaks = [f for f in os.listdir(os.path.join(CONF, "logs"))
             if _re.search(r"Certificat telephone-paul \([^)]*\) :\n\s+[A-Za-z0-9]{12}\b", open(os.path.join(CONF, "logs", f)).read())]
    check("[ami] mot de passe des certificats absent des journaux", not leaks, str(leaks))

    # d2) accès par mot de passe seulement : pas de règle, pas de certificat, joignable
    before = cloud()
    answers = {"target": "105", "snapshot_before": False, "login_password": PWD, "profile": "perso",
               "instance_name": "Libre", "steps": ["cloudflare"], "host": "ha-libre.degest.test", "cf_mode": "login",
               "summary": "ok", "snapshot_after": False}
    out = run_script("libre", answers)
    after = cloud()
    check("[libre] aucune règle ni certificat ajouté", after["ruleset"] == before["ruleset"] and after["mtls"] == before["mtls"])
    check("[libre] joignable avec le mot de passe Home Assistant", "joignable (mot de passe Home Assistant)" in out, out[-600:])
    check("[libre] aucune question de certificat", "Certificats des appareils" not in out)
    cloud("/test/set", {"no_sbfm": False})

    # h) comptes : reprise des personnes d'une instance précédente, puis locataire
    cp = os.path.join(CONF, "config.json")
    c = json.load(open(cp))
    c["people"].append({"name": "Paul", "username": "paul", "admin": True, "local_only": False})
    json.dump(c, open(cp, "w"))
    answers = {"target": "105", "snapshot_before": False, "login_password": PWD, "profile": "principal",
               "instance_name": "Centrale", "steps": ["users"], "people.reuse": True, "people.add": False,
               "summary": "ok", "secrets": "ok", "snapshot_after": False}
    out = run_script("reprise", answers)
    ha = ha_session()
    users = {u["username"]: u for u in ha.call("config/auth/list") if u.get("username")}
    check("[reprise] Paul repris de l'instance précédente, administrateur", "paul" in users
          and "system-admin" in users["paul"]["group_ids"] and "créés : paul" in out, out[-600:])
    answers = {"target": "105", "snapshot_before": False, "login_password": PWD, "profile": "location",
               "instance_name": "Centrale", "steps": ["users"], "people.add": [True, False], "people.name": "Martin",
               "people.username": ["locataire", "martin-appart"], "people.remote": True,
               "summary": "ok", "secrets": "ok", "snapshot_after": False}
    out = run_script("locataire", answers)
    users = {u["username"]: u for u in ha.call("config/auth/list") if u.get("username")}
    check("[locataire] identifiant générique refusé", "Trop générique" in out)
    check("[locataire] compte créé, non administrateur, avec sa personne", "martin-appart" in users
          and users["martin-appart"]["group_ids"] == ["system-users"]
          and any(p["name"] == "Martin" for p in ha.call("person/list")["storage"]))
    check("[locataire] pas mémorisé pour les instances suivantes",
          not any(p["username"] == "martin-appart" for p in json.load(open(cp))["people"]))
    ha.ws_close()

    # f) mises à jour disponibles (Supervisor, Core, OS) : appliquées dans l'ordre, attente des redémarrages
    for n in ("supervisor", "core", "os"):
        open(os.path.join(CFG, f"hab_update_{n}"), "w").write("1")
    exits_before = open(os.path.join(WORK, "hass.out")).read().count("### hass exit 100")
    answers = {"target": "105", "snapshot_before": False, "login_password": PWD, "profile": "perso",
               "instance_name": "Centrale", "steps": ["updates"], "summary": "ok", "snapshot_after": False}
    out = run_script("majs", answers)
    exits = open(os.path.join(WORK, "hass.out")).read().count("### hass exit 100") - exits_before
    check("[majs] Supervisor, Core et OS mis à jour", "Mises à jour : Supervisor 2026.11.2, Core 16.3, OS 16.3" in out, out[-800:])
    check("[majs] deux redémarrages attendus et suivis", exits == 2, str(exits))
    check("[majs] plus rien en attente", not any(os.path.exists(os.path.join(CFG, f"hab_update_{n}")) for n in ("supervisor", "core", "os")))

    # g) double authentification (TOTP) activée sur le compte : le code est demandé et accepté
    import base64, hashlib, hmac, struct as st

    def totp(secret):
        key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
        h = hmac.new(key, st.pack(">Q", int(time.time()) // 30), hashlib.sha1).digest()
        o = h[-1] & 0x0F
        return f"{(st.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"
    ha = ha_session()
    r = ha.call("auth/setup_mfa", mfa_module_id="totp")
    secret = r["description_placeholders"]["code"]
    r2 = ha.call("auth/setup_mfa", flow_id=r["flow_id"], user_input={"code": totp(secret)})
    check("[mfa] TOTP activé pour le banc", r2.get("type") == "create_entry", json.dumps(r2)[:200])
    ha.ws_close()
    answers = {"target": "105", "snapshot_before": False, "login_password": PWD, "login_mfa": totp(secret),
               "profile": "perso", "instance_name": "Centrale", "steps": ["general"], "coords": "",
               "summary": "ok", "snapshot_after": False}
    out = run_script("mfa", answers)
    check("[mfa] connexion avec code à 6 chiffres", "Connecté en tant que Richard" in out and "Connexion refusée" not in out, out[-800:])

    # e) instantané Proxmox impossible → on demande, refus = arrêt propre (code 2)
    answers = {"target": "105", "snapshot_before": True, "snapshot_before.continue": False}
    out = run_script("snapfail", answers, extra_env={"HAB_TEST_SNAP_FAIL": "1"}, expect_rc=2)
    check("[échec] instantané impossible → arrêt demandé", "pas d'instantané" in out)


if __name__ == "__main__":
    main()
