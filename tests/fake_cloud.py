"""Faux Cloudflare + faux GitHub + contrôle « public » pour le banc ha-bootstrap.

Cloudflare : /client/v4/...   GitHub : /github/...   Public : /public/<hôte>/...
Pilotage du banc : /test/...   État dans STATE (JSON via GET /test/state).
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ZONE = "degest.test"
SKI = "9D9721B71DD6781A5DBBF7421E38392D9FB9402F"
GRANGE_WEBHOOK = "/api/webhook/grangeWEBHOOKid0123456789"
OAUTH = ('starts_with(http.request.uri.path, "/.well-known/")'
         ' or starts_with(http.request.uri.path, "/api/ha_mcp_tools/oauth/")')


def accès_distant_expr(host, webhook):
    """Même construction que manager.waf_expression() d'Accès distant 1.7 (options token + Claude)."""
    return (f'(http.host eq "{host}")\n'
            "and not (cf.tls_client_auth.cert_verified\n"
            "         and not cf.tls_client_auth.cert_revoked\n"
            f'         and cf.tls_client_auth.cert_issuer_ski eq "{SKI}")'
            '\nand not (http.request.uri.path eq "/auth/token")'
            f"\nand not (ip.src in {{160.79.104.0/21}}\n"
            f'         and (http.request.uri.path eq "{webhook}"\n'
            f"              or {OAUTH}))")


def fresh_state():
    return {
        "token": "cf-good-token", "missing": [], "rule_limit": 10, "gh_token": "gh-good-token", "no_sbfm": False,
        "client_certs": [],
        "zone": {"id": "zone0123456789abcdef", "name": ZONE, "account": {"id": "acc0123456789"}},
        "mtls": [f"ha.{ZONE}"],
        "tunnels": {}, "dns": [],
        "ruleset": {"id": "rs_custom_1", "phase": "http_request_firewall_custom", "rules": [
            {"id": "r1", "description": "Ha- externe", "action": "block", "enabled": True,
             "expression": accès_distant_expr(f"ha.{ZONE}", GRANGE_WEBHOOK)},
            {"id": "r2", "description": "HA-MCP - Claude", "action": "skip", "enabled": True,
             "expression": f'(http.host eq "ha.{ZONE}" and ip.src in {{160.79.104.0/21}})',
             "action_parameters": {"ruleset": "current", "phases": ["http_request_sbfm"]},
             "logging": {"enabled": True}},
            {"id": "r3", "description": "sentinel", "action": "block", "enabled": True,
             "expression": '(http.request.uri.path contains "/.env")'},
            {"id": "r4", "description": "Geo Block", "action": "block", "enabled": True,
             "expression": '(ip.src.country ne "FR" and not ip.src in {160.79.104.0/21})'},
        ]},
        "log": [],
    }


STATE = fresh_state()
LOCK = threading.Lock()
TARBALL = b""


def make_tarball(src_dir):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(src_dir, arcname="Basilik41-hass-acces-distant-66f92ac",
                filter=lambda ti: None if "/.git" in ti.name or "node_modules" in ti.name else ti)
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def ok(self, result):
        self._send(200, {"success": True, "errors": [], "messages": [], "result": result})

    def err(self, status, code, msg):
        self._send(status, {"success": False, "errors": [{"code": code, "message": msg}], "result": None})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        with LOCK:
            STATE["log"].append(f"{method} {u.path}")
            try:
                if u.path.startswith("/client/v4"):
                    return self.cf(method, u.path[len("/client/v4"):], q)
                if u.path.startswith("/github"):
                    return self.gh(method, u.path[len("/github"):])
                if u.path.startswith("/public/"):
                    return self.public(u.path[len("/public/"):])
                if u.path == "/test/state":
                    return self._send(200, STATE)
                if u.path == "/test/reset":
                    STATE.clear()
                    STATE.update(fresh_state())
                    STATE.update(self._body())
                    return self._send(200, {})
                if u.path == "/test/set":
                    STATE.update(self._body())
                    return self._send(200, {})
                if u.path == "/test/tunnel_up":
                    tok = self._body().get("token")
                    for t in STATE["tunnels"].values():
                        if t["token"] == tok:
                            t["status"] = "healthy"
                    return self._send(200, {})
                self._send(404, {"error": "inconnu"})
            except Exception as e:  # noqa
                self._send(500, {"error": repr(e)})

    # ── Cloudflare ──
    def perm(self, name):
        if self.headers.get("Authorization") != f"Bearer {STATE['token']}":
            self.err(401, 10000, "Authentication error")
            return False
        if name in STATE["missing"]:
            self.err(403, 10000, "Authentication error")
            return False
        return True

    def cf(self, method, path, q):
        z = STATE["zone"]
        acc = z["account"]["id"]
        if path == "/zones" and method == "GET":
            if not self.perm("zone"):
                return
            name = q.get("name")
            allz = [z] + STATE.get("extra_zones", [])
            return self.ok([x for x in allz if name is None or name == x["name"]])
        m = re.match(rf"^/accounts/{acc}/cfd_tunnel(?:/([^/]+))?(?:/(configurations|token))?$", path)
        if m:
            if not self.perm("tunnel"):
                return
            tid, sub = m.groups()
            T = STATE["tunnels"]
            if not tid and method == "GET":
                return self.ok([t["public"] for t in T.values() if t["public"]["name"] == q.get("name", t["public"]["name"])])
            if not tid and method == "POST":
                b = self._body()
                if any(t["public"]["name"] == b["name"] for t in T.values()):
                    return self.err(409, 1013, "tunnel name already exists")
                tid = "tun-" + secrets.token_hex(6)
                T[tid] = {"public": {"id": tid, "name": b["name"], "status": "inactive"}, "token": "eyJ" + secrets.token_hex(24),
                          "config": None, "status": "inactive"}
                return self.ok(T[tid]["public"])
            if tid not in T:
                return self.err(404, 1003, "tunnel not found")
            if sub == "configurations" and method == "PUT":
                T[tid]["config"] = self._body()["config"]
                return self.ok({"tunnel_id": tid})
            if sub == "token":
                return self.ok(T[tid]["token"])
            pub = dict(T[tid]["public"], status=T[tid]["status"])
            return self.ok(pub)
        zp = f"/zones/{z['id']}"
        if path == zp + "/dns_records":
            if not self.perm("dns"):
                return
            if method == "GET":
                return self.ok([r for r in STATE["dns"] if r["name"] == q.get("name", r["name"])])
            b = self._body()
            if any(r["name"] == b["name"] for r in STATE["dns"]):
                return self.err(400, 81053, "An A, AAAA, or CNAME record with that host already exists.")
            rec = dict(b, id="dns-" + secrets.token_hex(4))
            STATE["dns"].append(rec)
            return self.ok(rec)
        m = re.match(rf"^{zp}/dns_records/([^/]+)$", path)
        if m and method == "PATCH":
            if not self.perm("dns"):
                return
            rec = next(r for r in STATE["dns"] if r["id"] == m.group(1))
            rec.update(self._body())
            return self.ok(rec)
        if path == zp + "/certificate_authorities/hostname_associations":
            if not self.perm("ssl"):
                return
            if method == "PUT":
                STATE["mtls"] = list(self._body()["hostnames"])
            return self.ok({"hostnames": STATE["mtls"]})
        if path == zp + "/client_certificates" and method == "POST":
            if not self.perm("ssl"):
                return
            csr = self._body()["csr"]
            import subprocess, tempfile
            with tempfile.TemporaryDirectory() as t:
                open(f"{t}/r.csr", "w").write(csr)
                r = subprocess.run(["openssl", "x509", "-req", "-in", f"{t}/r.csr", "-CA", CA_CRT, "-CAkey", CA_KEY,
                                    "-CAcreateserial", "-CAserial", f"{t}/s", "-days", "3650", "-out", f"{t}/c.crt"],
                                   capture_output=True, text=True)
                if r.returncode:
                    return self.err(400, 1400, "CSR invalide : " + r.stderr[-200:])
                cert = open(f"{t}/c.crt").read()
            rec = {"id": "cc-" + secrets.token_hex(4), "certificate": cert, "status": "active"}
            STATE["client_certs"].append(rec)
            return self.ok(rec)
        if path == zp + "/rulesets":
            if not self.perm("waf"):
                return
            rs = STATE["ruleset"]
            return self.ok([{"id": rs["id"], "phase": rs["phase"]}] if rs else [])
        if path == zp + "/rulesets/phases/http_request_firewall_custom/entrypoint":
            if not self.perm("waf"):
                return
            if method == "PUT":
                rules = self._body()["rules"]
                bad = self._bad_skip(rules)
                if bad:
                    return bad
                STATE["ruleset"] = {"id": "rs_new_" + secrets.token_hex(3), "phase": "http_request_firewall_custom",
                                    "rules": [dict(r, id="r" + secrets.token_hex(4)) for r in rules]}
                return self.ok(STATE["ruleset"])
            if STATE["ruleset"] is None:
                return self.err(404, 10003, "could not find entrypoint ruleset")
            return self.ok(STATE["ruleset"])
        rs = STATE["ruleset"]
        m = re.match(rf"^{zp}/rulesets/{rs['id'] if rs else 'x'}/rules(?:/([^/]+))?$", path)
        if m:
            if not self.perm("waf"):
                return
            b = self._body()
            rules = rs["rules"]
            if method == "POST":
                if len(rules) >= STATE["rule_limit"]:
                    return self.err(400, 20217, "exceeded maximum number of rules in the phase http_request_firewall_custom: 5 out of 5")
                bad = self._bad_skip([b])
                if bad:
                    return bad
                pos = b.pop("position", None)
                rule = dict(b, id="r" + secrets.token_hex(4))
                if pos and "after" in pos:
                    i = next(i for i, r in enumerate(rules) if r["id"] == pos["after"])
                    rules.insert(i + 1, rule)
                elif pos and "index" in pos:
                    rules.insert(pos["index"] - 1, rule)
                else:
                    rules.append(rule)
                return self.ok(rs)
            if method == "PATCH":
                r = next(r for r in rules if r["id"] == m.group(1))
                r.update(b)
                return self.ok(rs)
            if method == "DELETE":
                rs["rules"] = [r for r in rules if r["id"] != m.group(1)]
                return self.ok(rs)
        self.err(404, 7003, f"route simulée inconnue : {method} {path}")

    def _bad_skip(self, rules):
        if STATE["no_sbfm"] and any("http_request_sbfm" in (r.get("action_parameters") or {}).get("phases", []) for r in rules):
            self.err(400, 20120, "phase http_request_sbfm is not available for this zone plan")
            return True
        return False

    # ── GitHub ──
    def gh(self, method, path):
        if self.headers.get("Authorization") != f"Bearer {STATE['gh_token']}":
            return self._send(401, {"message": "Bad credentials"})
        if path == "/repos/Basilik41/hass-acces-distant/commits/main":
            return self._send(200, {"sha": "66f92acdeadbeef"})
        if path == "/repos/Basilik41/hass-acces-distant/tarball/main":
            return self._send(200, TARBALL, "application/gzip")
        self._send(404, {"message": "Not Found"})

    # ── accès public simulé : refuse sans certificat si l'hôte est protégé ──
    def public(self, rest):
        host = rest.split("/", 1)[0]
        def has_host(e):
            if f'http.host eq "{host}"' in e:
                return True
            m = re.search(r'http\.host in \{([^}]*)\}', e)
            return bool(m and f'"{host}"' in m.group(1))
        protected = host in STATE["mtls"] and any(
            r["action"] == "block" and has_host(r["expression"])
            and "cf.tls_client_auth.cert_verified" in r["expression"] for r in (STATE["ruleset"] or {"rules": []})["rules"])
        exists = any(r["name"] == host for r in STATE["dns"])
        if not exists:
            return self._send(530, b"origin DNS error", "text/plain")
        geo = any(r["action"] == "block" and f'http.host eq "{host}"' in r["expression"] and "cf.tls_client_auth" not in r["expression"]
                  for r in (STATE["ruleset"] or {"rules": []})["rules"])
        self._send(403 if protected or geo else 200, b"{}")


CA_KEY = CA_CRT = ""


def main():
    global TARBALL, CA_KEY, CA_CRT
    import subprocess, tempfile
    t = tempfile.mkdtemp()
    CA_KEY, CA_CRT = f"{t}/ca.key", f"{t}/ca.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", CA_KEY, "-out", CA_CRT,
                    "-days", "3650", "-subj", "/CN=Cloudflare Managed CA (banc)"], check=True, capture_output=True)
    port = int(sys.argv[1])
    TARBALL = make_tarball(sys.argv[2])
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"fake cloud on {port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
