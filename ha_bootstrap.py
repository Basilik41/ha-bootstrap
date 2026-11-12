#!/usr/bin/env python3
"""ha-bootstrap — met en place le socle d'une instance Home Assistant OS, en une fois.

Lancé depuis le shell Proxmox (ou tout Linux du réseau de l'instance) par install.sh.
Python standard uniquement : rien à installer.

Chaque étape vérifie l'état réel avant d'agir : on peut relancer le script autant
de fois que nécessaire, il ne refait que ce qui manque.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import string
import struct
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

VERSION = "1.0.0"

CONF_DIR = os.environ.get("HAB_CONF_DIR") or os.path.join(os.path.expanduser("~"), ".config", "ha-bootstrap")
CF_API = os.environ.get("HAB_CF_API", "https://api.cloudflare.com/client/v4")
GITHUB_API = os.environ.get("HAB_GITHUB_API", "https://api.github.com")
PUBLIC_CHECK = os.environ.get("HAB_PUBLIC_CHECK_URL", "https://{host}/manifest.json")

ANTHROPIC_RANGE = "160.79.104.0/21"
ACCES_DISTANT_REPO = "Basilik41/hass-acces-distant"
HA_MCP_REPOS = ["homeassistant-ai/ha-mcp", "homeassistant-ai/ha-mcp-integration"]
REMOTE_HA_REPO = "custom-components/remote_homeassistant"
HACS_ADDONS_REPO = "https://github.com/hacs/addons"
CLOUDFLARED_REPOS = ["https://github.com/homeassistant-apps/repository",
                     "https://github.com/brenner-tobias/ha-addons"]
DOCKER_NET = "172.30.33.0/24"  # réseau des apps HAOS (Cloudflared)

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

PROFILES = {
    "centrale": {
        "label": "Centrale (Richard et Julie, lit les autres instances)",
        "name": "Centrale", "slug": "centrale",
        "steps": ["general", "updates", "users", "hacs", "ha_mcp", "acces_distant", "remote_ha", "cloudflare"],
        "people": [{"name": "Julie", "username": "julie", "admin": False}],
    },
    "bien": {
        "label": "Bien loué (locataire, données remontées vers la centrale)",
        "name": "Bien loué", "slug": "bien",
        "steps": ["general", "updates", "users", "hacs", "ha_mcp", "acces_distant", "cloudflare"],
        "people": [{"name": "Locataire", "username": "", "admin": False, "tenant": True}],
    },
    "principal": {
        "label": "Maison ou bâtiment de la famille",
        "name": "Maison", "slug": "maison",
        "steps": ["general", "updates", "users", "hacs", "ha_mcp", "acces_distant", "remote_ha", "cloudflare"],
        "people": [{"name": "Julie", "username": "julie", "admin": False}],
    },
    "perso": {
        "label": "Personnalisé (je choisis tout)",
        "name": "Home Assistant", "slug": "ha",
        "steps": ["general", "updates", "users", "hacs", "ha_mcp"],
        "people": [],
    },
}

STEP_LABELS = {
    "general": "Réglages généraux (nom, France, €, fuseau, unités)",
    "updates": "Mises à jour (Supervisor, Core, OS)",
    "users": "Comptes (famille, Claude, compte de service HA-MCP)",
    "hacs": "Boutique : Marketplace intégrée (ou HACS si HA < 2026.11)",
    "ha_mcp": "HA-MCP (accès de Claude)",
    "acces_distant": "Accès distant (intégration Basilik41, mode réglages)",
    "remote_ha": "Remote Home Assistant (liaisons entre instances)",
    "cloudflare": "Cloudflare (tunnel, DNS, certificat mTLS, règles WAF)",
}
STEP_ORDER = ["general", "updates", "users", "hacs", "ha_mcp", "acces_distant", "remote_ha", "cloudflare"]


# ───────────────────────────── erreurs ─────────────────────────────

class Abort(Exception):
    """Arrêt demandé (ou réponse manquante en mode test)."""


class StepSkipped(Exception):
    """Étape volontairement passée (raison dans le message)."""


class NetError(Exception):
    pass


class HAError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CFError(Exception):
    def __init__(self, status: int, errors: list, path: str):
        msgs = "; ".join(f"[{e.get('code')}] {e.get('message')}" for e in errors if isinstance(e, dict)) or f"HTTP {status}"
        super().__init__(f"Cloudflare {path} : {msgs}")
        self.status = status
        self.codes = {e.get("code") for e in errors if isinstance(e, dict)}
        self.text = msgs.lower()


# ───────────────────────────── journal ─────────────────────────────

class Log:
    def __init__(self):
        self.secrets: set[str] = set()
        self.path = None
        try:
            d = os.path.join(CONF_DIR, "logs")
            os.makedirs(d, mode=0o700, exist_ok=True)
            self.path = os.path.join(d, datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
            with open(self.path, "w") as f:
                f.write(f"ha-bootstrap {VERSION}\n")
            os.chmod(self.path, 0o600)
        except OSError:
            self.path = None

    def hide(self, value: str | None):
        if value and len(value) >= 6:
            self.secrets.add(value)

    def clean(self, text: str) -> str:
        for s in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(s, "•••")
        return text

    def write(self, text: str):
        if not self.path:
            return
        try:
            with open(self.path, "a") as f:
                f.write(datetime.now().strftime("%H:%M:%S ") + self.clean(text) + "\n")
        except OSError:
            pass


LOG = Log()


# ───────────────────────────── interface ─────────────────────────────

class UI:
    """Fenêtres whiptail (comme les scripts community), repli en mode texte.

    En test, HAB_ANSWERS pointe vers un JSON {clé: réponse | [réponses…]}.
    """

    def __init__(self):
        self.answers: dict = {}
        self.scripted = False
        path = os.environ.get("HAB_ANSWERS")
        if path:
            with open(path) as f:
                self.answers = json.load(f)
            self.scripted = True
        self.whip = (not self.scripted and shutil.which("whiptail") is not None
                     and sys.stdin.isatty() and sys.stdout.isatty())
        self.asked: list[str] = []

    # -- mode test --
    def _scripted(self, key: str, default):
        self.asked.append(key)
        if key in self.answers:
            v = self.answers[key]
            if isinstance(v, list):
                if not v:
                    raise Abort(f"[test] plus de réponse pour {key}")
                return v.pop(0)
            return v
        if default is not None:
            return default
        raise Abort(f"[test] réponse manquante : {key}")

    # -- whiptail --
    def _size(self, text: str, extra: int = 0) -> tuple[str, str]:
        cols, rows = shutil.get_terminal_size((100, 30))
        w = max(50, min(cols - 4, 100))
        lines = 0
        for line in text.split("\n"):
            lines += max(1, (len(line) + w - 7) // (w - 6))
        h = max(8, min(rows - 2, lines + 7 + extra))
        return str(h), str(w)

    def _whip(self, args: list[str]) -> tuple[int, str]:
        r = subprocess.run(["whiptail", "--backtitle", f"ha-bootstrap {VERSION}"] + args,
                           stderr=subprocess.PIPE)
        return r.returncode, r.stderr.decode("utf-8", "replace").strip()

    def _cancelled(self):
        if self.yesno("_stop", "Arrêter ?", "Tu as annulé. Arrêter le script ?\n\n"
                      "Rien n'est perdu : il reprendra là où il s'est arrêté.", default=False):
            raise Abort("Arrêt demandé")

    # -- API --
    def section(self, title: str):
        LOG.write(f"=== {title}")
        print(f"\n\033[1;36m▶ {title}\033[0m", flush=True)

    def say(self, text: str):
        LOG.write(text)
        print(f"  {text}", flush=True)

    def ok(self, text: str):
        LOG.write("OK " + text)
        print(f"  \033[32m✔\033[0m {text}", flush=True)

    def warn(self, text: str):
        LOG.write("ATTENTION " + text)
        print(f"  \033[33m⚠\033[0m {text}", flush=True)

    def info(self, key: str, title: str, text: str):
        LOG.write(f"[info {key}] {title}: {text}")
        if self.scripted:
            self._scripted(key, "ok")
            print(f"\n== {title} ==\n{text}\n", flush=True)
            return
        if self.whip:
            h, w = self._size(text)
            self._whip(["--title", title, "--scrolltext", "--msgbox", text, h, w])
        else:
            print(f"\n== {title} ==\n{text}\n")
            input("[Entrée pour continuer] ")

    def yesno(self, key: str, title: str, text: str, default: bool = True) -> bool:
        if self.scripted:
            v = self._scripted(key, default)
            return bool(v) if not isinstance(v, str) else v.lower() in ("1", "o", "oui", "y", "yes", "true")
        if self.whip:
            h, w = self._size(text)
            args = ["--title", title, "--yes-button", "Oui", "--no-button", "Non"]
            if not default:
                args.append("--defaultno")
            rc, _ = self._whip(args + ["--yesno", text, h, w])
            return rc == 0
        d = "O/n" if default else "o/N"
        r = input(f"\n{title}\n{text}\n[{d}] ").strip().lower()
        return default if not r else r in ("o", "oui", "y", "yes")

    def ask(self, key: str, title: str, text: str, default: str = "", secret: bool = False,
            validate=None, allow_empty: bool = False) -> str:
        refused = 0
        while True:
            if self.scripted and refused >= 3:
                raise Abort(f"[test] {key} refusé trois fois")
            if self.scripted:
                v = str(self._scripted(key, default if (default or allow_empty) else None))
            elif self.whip:
                h, w = self._size(text, 2)
                kind = "--passwordbox" if secret else "--inputbox"
                args = ["--title", title, "--ok-button", "Valider", "--cancel-button", "Annuler", kind, text, h, w]
                if not secret:
                    args.append(default)
                rc, v = self._whip(args)
                if rc != 0:
                    self._cancelled()
                    continue
            else:
                prompt = f"\n{title}\n{text}\n" + (f"[{default}] " if default and not secret else "> ")
                v = getpass.getpass(prompt) if secret else input(prompt)
            v = v.strip()
            if not v and default and not secret:
                v = default
            if not v and not allow_empty:
                refused += 1
                self.error_box(key, "Valeur vide", "Il faut une valeur.")
                continue
            if validate and v:
                err = validate(v)
                if err:
                    refused += 1
                    self.error_box(key, "Valeur refusée", err)
                    continue
            if secret:
                LOG.hide(v)
            return v

    def error_box(self, key: str, title: str, text: str):
        LOG.write(f"[refus {key}] {text}")
        if self.scripted:
            self.asked.append(f"refus:{key}")
            print(f"  ✖ {text}", flush=True)
            return
        if self.whip:
            h, w = self._size(text)
            self._whip(["--title", title, "--msgbox", text, h, w])
        else:
            print(f"  ✖ {text}")

    def menu(self, key: str, title: str, text: str, items: list[tuple[str, str]], default: str | None = None) -> str:
        tags = [t for t, _ in items]
        while True:
            if self.scripted:
                v = self._scripted(key, default)
                if v not in tags:
                    raise Abort(f"[test] choix inconnu pour {key} : {v}")
                return v
            if self.whip:
                h, w = self._size(text, len(items) + 2)
                args = ["--title", title, "--ok-button", "Choisir", "--cancel-button", "Annuler"]
                if default in tags:
                    args += ["--default-item", default]
                flat = []
                for t, label in items:
                    flat += [t, label]
                rc, v = self._whip(args + ["--menu", text, h, w, str(min(len(items), 12))] + flat)
                if rc != 0:
                    self._cancelled()
                    continue
                return v
            print(f"\n{title}\n{text}")
            for i, (t, label) in enumerate(items, 1):
                mark = "*" if t == default else " "
                print(f" {mark}{i}) {label}")
            r = input("Numéro : ").strip()
            if not r and default in tags:
                return default
            if r.isdigit() and 1 <= int(r) <= len(items):
                return items[int(r) - 1][0]

    def checklist(self, key: str, title: str, text: str, items: list[tuple[str, str, bool]]) -> list[str]:
        if self.scripted:
            self.asked.append(key)
            v = self.answers.get(key, [t for t, _, on in items if on])
            if v and isinstance(v[0], list):  # plusieurs passages : liste de listes
                v = v.pop(0)
            unknown = set(v) - {t for t, _, _ in items}
            if unknown:
                raise Abort(f"[test] choix inconnus pour {key} : {unknown}")
            return list(v)
        if self.whip:
            while True:
                h, w = self._size(text, len(items) + 2)
                flat = []
                for t, label, on in items:
                    flat += [t, label, "ON" if on else "OFF"]
                rc, v = self._whip(["--title", title, "--separate-output", "--ok-button", "Valider",
                                    "--cancel-button", "Annuler", "--checklist", text, h, w,
                                    str(min(len(items), 12))] + flat)
                if rc != 0:
                    self._cancelled()
                    continue
                return [x for x in v.split("\n") if x]
        print(f"\n{title}\n{text}")
        chosen = []
        for t, label, on in items:
            if self.yesno(f"{key}.{t}", title, label, default=on):
                chosen.append(t)
        return chosen


# ───────────────────────────── HTTP ─────────────────────────────

def _ssl_context():
    ctx = ssl.create_default_context()
    ca = os.environ.get("HAB_CA_BUNDLE")
    if ca:
        ctx.load_verify_locations(ca)
    return ctx


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                      urllib.request.HTTPSHandler(context=_ssl_context()))
_INTERNET = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()))


def http(method: str, url: str, *, json_body=None, form=None, headers=None, timeout: float = 30,
         local: bool = False, raw: bool = False):
    """Requête HTTP. local=True : sans proxy (Home Assistant sur le réseau local)."""
    data = None
    h = {"Accept": "application/json", "User-Agent": f"ha-bootstrap/{VERSION}"}
    h.update(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    opener = _DIRECT if local else _INTERNET
    try:
        with opener.open(req, timeout=timeout) as r:
            body, status, rh = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        body, status, rh = e.read(), e.code, dict(e.headers or {})
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        raise NetError(f"{method} {url.split('?')[0]} : {getattr(e, 'reason', e)}") from e
    text = body.decode("utf-8", "replace")
    LOG.write(f"{method} {url.split('?')[0]} -> {status}")
    if raw:
        return status, text, rh
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        payload = text
    return status, payload


# ───────────────────────────── WebSocket (RFC 6455, client minimal) ─────────────────────────────

class WSClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 15):
        u = urllib.parse.urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            sock = _ssl_context().wrap_socket(sock, server_hostname=u.hostname)
        self.sock = sock
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        host = u.hostname + (f":{u.port}" if u.port else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"User-Agent: ha-bootstrap/{VERSION}\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSClosed("connexion fermée pendant la poignée de main")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise WSClosed(f"refus WebSocket : {lines[0]}")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        hdrs = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        if hdrs.get("sec-websocket-accept") != accept:
            raise WSClosed("clé WebSocket invalide")
        self.closed = False

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise
            except OSError as e:
                self.closed = True
                raise WSClosed(str(e)) from e
            if not chunk:
                self.closed = True
                raise WSClosed("connexion fermée")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes):
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(head + mask + masked)
        except OSError as e:
            self.closed = True
            raise WSClosed(str(e)) from e

    def send(self, text: str):
        self._send_frame(0x1, text.encode())

    def recv(self, timeout: float) -> str:
        self.sock.settimeout(timeout)
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if b2 & 0x80:
                mask = self._read(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(n)))
            else:
                data = self._read(n)
            if opcode == 0x8:
                self.closed = True
                raise WSClosed("fermé par Home Assistant")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.closed = True


# ───────────────────────────── Home Assistant ─────────────────────────────

class HA:
    def __init__(self, base: str, ui: UI):
        self.base = base.rstrip("/")
        self.ui = ui
        self.client_id = self.base + "/"
        self.redirect = self.base + "/?auth_callback=1"
        self.access: str | None = None
        self.refresh: str | None = None
        self.expires = 0.0
        self.ws: WebSocket | None = None
        self.msg_id = 0

    # -- authentification --
    def _store_tokens(self, payload):
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise HAError("auth", f"jeton non obtenu : {payload}")
        self.access = payload["access_token"]
        self.refresh = payload.get("refresh_token") or self.refresh
        self.expires = time.time() + float(payload.get("expires_in", 1800))
        LOG.hide(self.access)
        LOG.hide(self.refresh)

    def _exchange(self, code: str):
        s, p = http("POST", self.base + "/auth/token", local=True,
                    form={"grant_type": "authorization_code", "code": code, "client_id": self.client_id})
        if s != 200:
            raise HAError("auth", f"échange du code refusé (HTTP {s})")
        self._store_tokens(p)

    def token(self) -> str:
        if self.access and time.time() < self.expires - 60:
            return self.access
        if not self.refresh:
            raise HAError("auth", "pas de session")
        s, p = http("POST", self.base + "/auth/token", local=True,
                    form={"grant_type": "refresh_token", "refresh_token": self.refresh, "client_id": self.client_id})
        if s != 200:
            raise HAError("auth", f"session refusée (HTTP {s}) : relance le script")
        self._store_tokens(p)
        return self.access

    def onboarding(self) -> dict[str, bool] | None:
        """{étape: faite} ; None si l'accueil est terminé (vue retirée)."""
        s, p = http("GET", self.base + "/api/onboarding", local=True, timeout=10)
        if s == 404:
            return None
        if s != 200 or not isinstance(p, list):
            raise HAError("onboarding", f"réponse inattendue (HTTP {s})")
        return {x["step"]: bool(x["done"]) for x in p}

    def create_owner(self, name: str, username: str, password: str):
        s, p = http("POST", self.base + "/api/onboarding/users", local=True,
                    json_body={"name": name, "username": username, "password": password,
                               "client_id": self.client_id, "language": "fr"})
        if s != 200:
            raise HAError("onboarding", f"création du compte refusée (HTTP {s}) : {p}")
        self._exchange(p["auth_code"])

    def login(self, username: str, password: str, mfa_code=None) -> str | None:
        """Ouvre une session. Renvoie None si OK, sinon le motif ('invalid_auth', …)."""
        s, p = http("POST", self.base + "/auth/login_flow", local=True,
                    json_body={"client_id": self.client_id, "handler": ["homeassistant", None],
                               "redirect_uri": self.redirect})
        if s != 200:
            raise HAError("auth", f"connexion impossible (HTTP {s})")
        flow = p["flow_id"]
        s, p = http("POST", f"{self.base}/auth/login_flow/{flow}", local=True,
                    json_body={"client_id": self.client_id, "username": username, "password": password})
        for _ in range(3):
            if s != 200:
                return f"HTTP {s}"
            if p.get("type") == "create_entry":
                self._exchange(p["result"])
                return None
            if p.get("type") == "form" and p.get("step_id") == "mfa":
                if mfa_code is None:
                    return "mfa_required"
                s, p = http("POST", f"{self.base}/auth/login_flow/{flow}", local=True,
                            json_body={"client_id": self.client_id, "code": mfa_code()})
                continue
            errors = p.get("errors") or {}
            return errors.get("base") or p.get("type") or "refus"
        return "refus"

    # -- REST --
    def req(self, method: str, path: str, **kw):
        h = kw.pop("headers", {})
        h["Authorization"] = "Bearer " + self.token()
        return http(method, self.base + path, local=True, headers=h, **kw)

    def api_up(self) -> bool:
        try:
            s, _ = self.req("GET", "/api/", timeout=8)
            return s == 200
        except (NetError, HAError):
            return False

    # -- WebSocket --
    def ws_connect(self):
        if self.ws and not self.ws.closed:
            return
        url = self.base.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        ws = WebSocket(url)
        first = json.loads(ws.recv(15))
        if first.get("type") != "auth_required":
            raise HAError("ws", f"accueil inattendu : {first}")
        ws.send(json.dumps({"type": "auth", "access_token": self.token()}))
        res = json.loads(ws.recv(15))
        if res.get("type") != "auth_ok":
            raise HAError("ws", f"authentification refusée : {res.get('message')}")
        self.ws = ws

    def ws_close(self):
        if self.ws:
            self.ws.close()
            self.ws = None

    def send_only(self, payload: dict) -> int:
        self.ws_connect()
        self.msg_id += 1
        payload = dict(payload, id=self.msg_id)
        self.ws.send(json.dumps(payload))
        return self.msg_id

    def call(self, type_: str, wait: float = 30, **payload):
        # Renvoi seulement si l'envoi lui-même a échoué (connexion périmée) : une commande déjà
        # partie n'est jamais rejouée (mise à jour, redémarrage…).
        for attempt in (1, 2):
            try:
                mid = self.send_only(dict(payload, type=type_))
                break
            except (WSClosed, OSError) as e:
                self.ws = None
                if attempt == 2:
                    raise HAError("ws", f"connexion impossible ({e})")
        deadline = time.time() + wait
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise HAError("timeout", f"{type_} : pas de réponse en {int(wait)} s")
            try:
                raw = self.ws.recv(min(left, 30))
            except socket.timeout:
                continue
            except (WSClosed, OSError) as e:
                self.ws = None
                raise HAError("ws", f"connexion perdue ({e})")
            msgs = json.loads(raw)
            for m in (msgs if isinstance(msgs, list) else [msgs]):
                if m.get("id") == mid and m.get("type") == "result":
                    if m.get("success"):
                        return m.get("result")
                    err = m.get("error") or {}
                    raise HAError(err.get("code", "error"), err.get("message", str(err)))

    def supervisor(self, method: str, endpoint: str, data: dict | None = None, timeout: float = 60):
        kw = {"endpoint": endpoint, "method": method, "timeout": timeout}
        if data is not None:
            kw["data"] = data
        return self.call("supervisor/api", wait=timeout + 20, **kw)

    def config(self) -> dict:
        return self.call("get_config")

    # -- redémarrage --
    def wait_up(self, timeout: float = 900, what: str = "Home Assistant"):
        self.ws_close()
        deadline = time.time() + timeout
        shown = 0.0
        while time.time() < deadline:
            try:
                if self.api_up():
                    self.ws_connect()
                    if self.config().get("state") == "RUNNING":
                        return
            except (HAError, WSClosed, NetError, OSError, ValueError):
                self.ws = None
            if time.time() - shown > 30:
                self.ui.say(f"… attente : {what} ({int(deadline - time.time())} s max)")
                shown = time.time()
            time.sleep(4)
        raise HAError("timeout", f"{what} ne répond pas après {int(timeout)} s")

    def wait_ws_drop(self, timeout: float = 180) -> bool:
        """Attend que Home Assistant coupe la connexion (arrêt effectif). True si c'est arrivé."""
        if not self.ws:
            return True
        deadline = time.time() + timeout
        dropped = False
        while time.time() < deadline:
            try:
                self.ws.recv(5)
            except socket.timeout:
                continue
            except (WSClosed, OSError, ValueError):
                dropped = True
                break
        self.ws = None
        return dropped

    def restart(self, why: str):
        self.ui.say(f"Redémarrage de Home Assistant ({why})…")
        self.wait_up(what="la fin du démarrage")  # HA refuse de redémarrer pendant un démarrage
        mid = self.send_only({"type": "call_service", "domain": "homeassistant", "service": "restart"})
        deadline = time.time() + 180
        dropped = False
        while time.time() < deadline:
            try:
                raw = self.ws.recv(5)
            except socket.timeout:
                continue
            except (WSClosed, OSError, ValueError):
                dropped = True
                break
            for m in (lambda x: x if isinstance(x, list) else [x])(json.loads(raw)):
                if m.get("id") == mid and m.get("type") == "result" and not m.get("success"):
                    err = m.get("error") or {}
                    raise HAError(err.get("code", "restart"), f"redémarrage refusé : {err.get('message')}")
        self.ws = None
        if not dropped:
            raise HAError("restart", "Home Assistant n'a pas redémarré (3 min)")
        self.wait_up(what="le redémarrage")
        self.ui.ok("Home Assistant a redémarré")

    # -- assistants de configuration --
    def flow_start(self, handler: str):
        s, p = self.req("POST", "/api/config/config_entries/flow",
                        json_body={"handler": handler, "show_advanced_options": False}, timeout=60)
        if s != 200:
            raise HAError("flow", f"{handler} : {p}")
        return p

    def flow_step(self, flow_id: str, data: dict | None = None):
        if data is None:
            s, p = self.req("GET", f"/api/config/config_entries/flow/{flow_id}", timeout=60)
        else:
            s, p = self.req("POST", f"/api/config/config_entries/flow/{flow_id}", json_body=data, timeout=120)
        if s == 404:
            return None
        if s != 200:
            raise HAError("flow", f"étape refusée : {p}")
        return p

    def flow_abort(self, flow_id: str, options: bool = False):
        kind = "options/flow" if options else "flow"
        try:
            self.req("DELETE", f"/api/config/config_entries/{kind}/{flow_id}", timeout=15)
        except (NetError, HAError):
            pass

    def entries(self, domain: str) -> list[dict]:
        s, p = self.req("GET", f"/api/config/config_entries/entry?domain={urllib.parse.quote(domain)}")
        if s != 200 or not isinstance(p, list):
            raise HAError("entries", f"lecture des intégrations impossible (HTTP {s})")
        return [e for e in p if e.get("domain") == domain]

    def has_integration(self, domain: str) -> bool:
        try:
            self.call("manifest/get", integration=domain)
            return True
        except HAError:
            return False


# ───────────────────────────── Cloudflare ─────────────────────────────

class Cloudflare:
    def __init__(self, token: str):
        self.token = token
        LOG.hide(token)

    def req(self, method: str, path: str, body=None, params=None):
        url = CF_API + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        s, p = http(method, url, json_body=body, headers={"Authorization": f"Bearer {self.token}"}, timeout=45)
        if isinstance(p, dict) and s < 400 and p.get("success", True):
            return p.get("result")
        errors = (p.get("errors") if isinstance(p, dict) else None) or []
        raise CFError(s, errors, path)

    def find_zone(self, host: str) -> dict:
        labels = host.split(".")
        for i in range(len(labels) - 1):
            name = ".".join(labels[i:])
            res = self.req("GET", "/zones", params={"name": name})
            if res:
                return res[0]
        raise CFError(404, [{"code": 0, "message": f"aucune zone Cloudflare ne contient {host} (ou le jeton n'y a pas accès)"}], "/zones")


# ───────────────────────────── Proxmox ─────────────────────────────

class Proxmox:
    def __init__(self):
        self.available = bool(shutil.which("qm")) and not os.environ.get("HAB_NO_PVE")

    def run(self, *args, timeout=60) -> str:
        r = subprocess.run(["qm", *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() or f"qm {' '.join(args)} a échoué")
        return r.stdout

    def vms(self) -> list[dict]:
        out = []
        for line in self.run("list").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3 and parts[0].isdigit():
                out.append({"vmid": parts[0], "name": parts[1], "status": parts[2]})
        return out

    def ips(self, vmid: str) -> list[str]:
        raw = self.run("guest", "cmd", vmid, "network-get-interfaces", timeout=30)
        data = json.loads(raw)
        found = []
        for itf in data if isinstance(data, list) else []:
            name = itf.get("name", "")
            if name == "lo" or name.startswith(("docker", "hassio", "veth")):
                continue
            for a in itf.get("ip-addresses", []):
                ip = a.get("ip-address", "")
                if a.get("ip-address-type") == "ipv4" and not ip.startswith(("127.", "172.30.", "172.17.")):
                    found.append(ip)
        return found

    def snapshot(self, vmid: str, name: str, desc: str):
        self.run("snapshot", vmid, name, "--description", desc, timeout=300)


# ───────────────────────────── outils ─────────────────────────────

def gen_password(n: int = 20) -> str:
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0OolI1")
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(n))
        if any(c.isdigit() for c in p) and any(c.islower() for c in p) and any(c.isupper() for c in p):
            LOG.hide(p)
            return p


def slugify(text: str) -> str:
    t = text.lower()
    for a, b in (("àâä", "a"), ("éèêë", "e"), ("îï", "i"), ("ôö", "o"), ("ùûü", "u"), ("ç", "c")):
        for ch in a:
            t = t.replace(ch, b)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t or "ha"


def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path: str, data, private: bool = False):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# Script exécuté DANS l'app File editor (Python présent dans l'image) pour poser
# une intégration d'un dépôt GitHub privé, sans que le jeton apparaisse dans une commande.
REMOTE_INSTALLER = r'''
import io, json, os, shutil, sys, tarfile, tempfile, urllib.request
P = "/homeassistant/.hab_params.json" if os.path.isdir("/homeassistant") else "/config/.hab_params.json"
try:
    with open(P) as f:
        prm = json.load(f)
finally:
    try:
        os.remove(P)
    except OSError:
        pass
root = "/homeassistant" if os.path.isdir("/homeassistant") else "/config"
comp = prm["component"]
dest = os.path.join(root, "custom_components", comp)
def out(**kw):
    print(json.dumps(kw)); sys.exit(0)
req = urllib.request.Request(prm["api"] + "/repos/" + prm["repo"] + "/tarball/" + prm["ref"],
    headers={"Authorization": "Bearer " + prm["token"], "Accept": "application/vnd.github+json",
             "User-Agent": "ha-bootstrap"})
try:
    data = urllib.request.urlopen(req, timeout=90).read()
except Exception as e:
    out(ok=False, error="téléchargement : %s" % e)
os.makedirs(os.path.join(root, "custom_components"), exist_ok=True)
work = tempfile.mkdtemp(prefix=".hab_", dir=root)
try:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = []
        for m in tar.getmembers():
            name = m.name
            if name.startswith("/") or ".." in name.split("/") or m.issym() or m.islnk() or m.isdev():
                continue
            members.append(m)
        tar.extractall(work, members=members)
    src = None
    for base, dirs, files in os.walk(work):
        if os.path.basename(base) == comp and "manifest.json" in files and os.path.basename(os.path.dirname(base)) == "custom_components":
            src = base
            break
    if not src:
        out(ok=False, error="intégration %s absente de l'archive" % comp)
    with open(os.path.join(src, "manifest.json")) as f:
        version = json.load(f).get("version")
    previous = None
    if os.path.isdir(dest):
        try:
            with open(os.path.join(dest, "manifest.json")) as f:
                previous = json.load(f).get("version")
        except Exception:
            previous = "?"
        if previous == version and not prm.get("force"):
            out(ok=True, version=version, previous=previous, unchanged=True)
        backup = os.path.join(root, ".%s_precedent" % comp)
        shutil.rmtree(backup, ignore_errors=True)
        os.rename(dest, backup)
    shutil.move(src, dest)
    out(ok=True, version=version, previous=previous, unchanged=False)
finally:
    shutil.rmtree(work, ignore_errors=True)
'''


# ───────────────────────────── le script ─────────────────────────────

class Bootstrap:
    def __init__(self):
        self.ui = UI()
        self.pve = Proxmox()
        self.conf = load_json(os.path.join(CONF_DIR, "config.json"), {})
        self.vault = load_json(os.path.join(CONF_DIR, "secrets.json"), {})
        for v in self.vault.values():
            if isinstance(v, str):
                LOG.hide(v)
        self.ha: HA | None = None
        self.ha_ip = ""
        self.vmid: str | None = None
        self.profile = "perso"
        self.instance_name = ""
        self.instance_slug = ""
        self.steps: list[str] = []
        self.report: list[tuple[str, str, str]] = []
        self.secrets_out: list[tuple[str, str]] = []
        self.need_restart = False
        self.webhook_path: str | None = None
        self.public_host: str | None = None
        self.next_steps: list[str] = []

    # -- sauvegardes locales --
    def save_conf(self):
        save_json(os.path.join(CONF_DIR, "config.json"), self.conf)

    def remember_secret(self, key: str, value: str, what: str) -> None:
        if self.vault.get(key) == value:
            return
        if self.ui.yesno(f"remember.{key}", "Garder le jeton ?",
                         f"Garder {what} sur ce serveur pour les prochaines instances ?\n\n"
                         f"Fichier {os.path.join(CONF_DIR, 'secrets.json')} lisible par root seulement.\n"
                         "Sinon il te sera redemandé à chaque lancement.", default=True):
            self.vault[key] = value
            save_json(os.path.join(CONF_DIR, "secrets.json"), self.vault, private=True)

    # -- exécution d'une étape avec reprise --
    def run_step(self, sid: str, title: str, fn) -> bool:
        while True:
            self.ui.section(title)
            try:
                res = fn()
                self.report.append((title, "ok", res or "fait"))
                return True
            except StepSkipped as e:
                self.ui.warn(f"passé : {e}")
                self.report.append((title, "passé", str(e)))
                return False
            except Abort:
                raise
            except Exception as e:  # noqa: BLE001 — toute erreur d'étape est proposée à la reprise
                LOG.write(traceback.format_exc())
                msg = LOG.clean(str(e))
                self.ui.warn(msg)
                choice = self.ui.menu(f"error.{sid}", "Problème", f"{title}\n\n{msg}\n\nQue faire ?",
                                      [("retry", "Réessayer"), ("skip", "Passer cette étape"),
                                       ("stop", "Arrêter (relançable plus tard)")],
                                      None if self.ui.scripted else "retry")
                if choice == "skip":
                    self.report.append((title, "erreur", msg))
                    return False
                if choice == "stop":
                    raise Abort(f"Arrêt sur : {title}")

    # ═════════════ 1. cible ═════════════
    def choose_target(self):
        base = os.environ.get("HAB_URL")
        if not base and self.pve.available:
            vms = self.pve.vms()
            items = [(v["vmid"], f"{v['name']} ({v['status']})") for v in vms]
            items += [("new", "Créer une nouvelle VM Home Assistant OS"), ("url", "Saisir l'adresse à la main")]
            guess = next((v["vmid"] for v in reversed(vms) if "haos" in v["name"].lower() or "home" in v["name"].lower()), None)
            choice = self.ui.menu("target", "Quelle instance ?", "Choisis la VM Home Assistant à préparer :", items, guess or "url")
            if choice == "new":
                choice = self.create_vm()
            if choice != "url":
                self.vmid = choice
                vm = next((v for v in self.pve.vms() if v["vmid"] == choice), None)
                if vm and vm["status"] != "running":
                    if self.ui.yesno("start_vm", "VM arrêtée", f"La VM {choice} est arrêtée. La démarrer ?"):
                        self.pve.run("start", choice)
                        time.sleep(5)
                ip = self.vm_ip(choice)
                if ip:
                    base = f"http://{ip}:8123"
        if not base:
            def check(v):
                return None if re.match(r"^https?://[^/\s]+$", v) else "Forme attendue : http://192.168.1.50:8123"
            base = self.ui.ask("url", "Adresse de Home Assistant", "Adresse locale de l'instance :",
                               default="http://", validate=check)
        self.ha_ip = urllib.parse.urlparse(base).hostname or ""
        self.ha = HA(base, self.ui)
        self.ui.ok(f"Instance : {base}" + (f" (VM {self.vmid})" if self.vmid else ""))

    def vm_ip(self, vmid: str) -> str | None:
        self.ui.say("Lecture de l'adresse IP par l'agent QEMU…")
        for _ in range(40):
            try:
                ips = self.pve.ips(vmid)
                if ips:
                    if len(ips) == 1:
                        return ips[0]
                    return self.ui.menu("vm_ip", "Plusieurs adresses", "Quelle adresse utiliser ?",
                                        [(ip, ip) for ip in ips], ips[0])
            except (RuntimeError, ValueError, subprocess.TimeoutExpired):
                pass
            time.sleep(6)
        self.ui.warn("Agent QEMU muet : adresse à saisir.")
        return None

    def create_vm(self) -> str:
        before = {v["vmid"] for v in self.pve.vms()}
        self.ui.info("new_vm", "Nouvelle VM",
                     "Le script officiel community-scripts va s'ouvrir.\n\n"
                     "Conseils : mode Advanced, 2 vCPU, 4 Go de RAM, 64 Go de disque, "
                     "bridge et VLAN du réseau serveurs, « Start VM when completed » coché.\n\n"
                     "Quand il a fini, ha-bootstrap reprend tout seul.")
        url = "https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/vm/haos-vm.sh"
        rc = subprocess.call(["bash", "-c", f'bash -c "$(curl -fsSL {url})"'])
        after = [v for v in self.pve.vms() if v["vmid"] not in before]
        if rc != 0 or not after:
            raise Abort("La VM n'a pas été créée.")
        return after[-1]["vmid"]

    def snapshot(self, label: str, desc: str, key: str, default=True):
        if not (self.vmid and self.pve.available):
            return
        if not self.ui.yesno(key, "Instantané Proxmox", f"Prendre un instantané de la VM {self.vmid} ({desc}) ?\n\n"
                             "C'est le point de retour si quelque chose tourne mal.", default=default):
            return
        name = f"hab_{label}_{datetime.now().strftime('%Y%m%d_%H%M')}"
        try:
            self.pve.snapshot(self.vmid, name, f"ha-bootstrap : {desc}")
            self.ui.ok(f"Instantané {name}")
            self.report.append((f"Instantané « {desc} »", "ok", name))
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            self.ui.warn(f"instantané impossible : {e}")
            if not self.ui.yesno(f"{key}.continue", "Instantané impossible",
                                 f"{e}\n\nContinuer sans instantané ?", default=False):
                raise Abort("Arrêt : pas d'instantané")

    # ═════════════ 2. connexion ═════════════
    def wait_reachable(self):
        deadline = time.time() + 1800
        shown = 0.0
        while True:
            try:
                return self.ha.onboarding()
            except (NetError, HAError):
                if time.time() > deadline:
                    raise Abort("Home Assistant injoignable depuis 30 min : vérifie la VM et le réseau.")
                if time.time() - shown > 30:
                    self.ui.say("… Home Assistant démarre (le premier démarrage peut prendre 20 min)")
                    shown = time.time()
                time.sleep(10)

    def connect(self):
        self.ui.section("Connexion")
        status = self.wait_reachable()
        if status is not None and not status.get("user"):
            self.ui.info("owner_intro", "Instance neuve",
                         "Aucun compte n'existe encore : on crée ton compte administrateur.\n\n"
                         "Mets le MÊME identifiant de connexion que sur la Grange : le bouton « Aller à… » "
                         "te reconnaîtra alors tout seul.")
            name = self.ui.ask("owner_name", "Ton compte", "Nom affiché :", default="Richard")
            username = self.ui.ask("owner_username", "Ton compte", "Identifiant de connexion :",
                                   default=self.conf.get("owner_username", ""), validate=self.check_username)
            pwd = self.new_password("owner")
            self.ha.create_owner(name, username, pwd)
            self.conf["owner_username"] = username
            self.save_conf()
            self.ui.ok(f"Compte {username} créé")
        else:
            default_user = self.conf.get("owner_username", "")
            for attempt in range(1, 4):
                username = self.ui.ask("login_username", "Connexion", "Ton identifiant Home Assistant :",
                                       default=default_user)
                pwd = self.ui.ask("login_password", "Connexion", f"Mot de passe de {username} :", secret=True)
                res = self.ha.login(username, pwd, mfa_code=lambda: self.ui.ask(
                    "login_mfa", "Double authentification", "Code à 6 chiffres :"))
                if res is None:
                    break
                self.ui.error_box("login", "Connexion refusée",
                                  f"Connexion refusée ({res}). Essai {attempt}/3.")
            else:
                raise Abort("Connexion impossible")
            self.conf["owner_username"] = username
            self.save_conf()
        if self.ha.config().get("state") != "RUNNING":
            self.ha.wait_up(what="la fin du démarrage")
        me = self.ha.call("auth/current_user")
        if not me.get("is_admin"):
            raise Abort(f"Le compte {me.get('name')} n'est pas administrateur.")
        self.me = me
        self.finish_onboarding(status)
        cfg = self.ha.config()
        self.ui.ok(f"Connecté en tant que {me.get('name')} — Home Assistant {cfg.get('version')}")

    def new_password(self, key: str) -> str:
        while True:
            p1 = self.ui.ask(f"{key}_password", "Mot de passe", "Mot de passe (12 caractères minimum) :", secret=True,
                             validate=lambda v: None if len(v) >= 12 else "12 caractères minimum.")
            p2 = self.ui.ask(f"{key}_password2", "Mot de passe", "Encore une fois :", secret=True)
            if p1 == p2:
                return p1
            self.ui.error_box(key, "Différents", "Les deux saisies ne correspondent pas.")

    @staticmethod
    def check_username(v: str):
        if not USERNAME_RE.match(v):
            return "Minuscules, chiffres, point, tiret ou souligné ; 2 à 32 caractères."
        return None

    def finish_onboarding(self, status):
        if status is None:
            return
        if not status.get("core_config"):
            self.ha.req("POST", "/api/onboarding/core_config", json_body={})
        if not status.get("analytics"):
            self.ha.req("POST", "/api/onboarding/analytics", json_body={})
        if not status.get("integration"):
            self.ha.req("POST", "/api/onboarding/integration",
                        json_body={"client_id": self.ha.client_id, "redirect_uri": self.ha.redirect})
        self.ui.ok("Écran d'accueil terminé")

    # ═════════════ 3. choix ═════════════
    def choose_plan(self):
        items = [(k, v["label"]) for k, v in PROFILES.items()]
        self.profile = self.ui.menu("profile", "Type d'instance", "Quel rôle pour cette instance ?", items, "centrale")
        prof = PROFILES[self.profile]
        cfg = self.ha.config()
        current = cfg.get("location_name") or ""
        default_name = current if current and current != "Home" else prof["name"]
        self.instance_name = self.ui.ask("instance_name", "Nom", "Nom de l'instance (affiché dans l'app) :",
                                         default=default_name)
        self.instance_slug = slugify(self.instance_name)
        items = [(s, STEP_LABELS[s], s in prof["steps"]) for s in STEP_ORDER]
        self.steps = self.ui.checklist("steps", "Ce qui sera fait",
                                       "Espace pour cocher / décocher, Entrée pour valider.\n"
                                       "Tout est relançable : une étape déjà faite est simplement vérifiée.", items)
        if not self.steps:
            raise Abort("Rien de coché.")

    # ═════════════ étapes ═════════════
    def step_general(self):
        cfg = self.ha.config()
        coords = None
        cur = (cfg.get("latitude"), cfg.get("longitude"))
        default = self.conf.get(f"coords.{self.instance_slug}", "")
        if not default and cur[0] is not None and abs(cur[0] - 52.3731) > 0.01:
            default = f"{cur[0]}, {cur[1]}"

        def check(v):
            m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$", v)
            if not m or not (-90 <= float(m.group(1)) <= 90 and -180 <= float(m.group(2)) <= 180):
                return "Forme attendue : 47.6012, 1.3335 (clic droit sur Google Maps → copier)"
            return None
        txt = self.ui.ask("coords", "Position", "Coordonnées GPS de l'instance (météo, soleil, gel) :\n"
                          "clic droit sur Google Maps → cliquer sur les chiffres pour les copier.\n"
                          "Laisse vide pour ne pas changer.", default=default, validate=check, allow_empty=True)
        if txt:
            a, b = re.split(r"[,; ]+", txt.strip(), maxsplit=1)
            coords = (round(float(a), 5), round(float(b), 5))
            self.conf[f"coords.{self.instance_slug}"] = f"{coords[0]}, {coords[1]}"
            self.save_conf()
        data = {"location_name": self.instance_name, "country": "FR", "language": "fr", "currency": "EUR",
                "time_zone": "Europe/Paris", "unit_system": "metric",
                "internal_url": f"http://{self.ha_ip}:8123"}
        if coords:
            data.update(latitude=coords[0], longitude=coords[1])
        self.ha.call("config/core/update", **data)
        return f"{self.instance_name}, France, €, Europe/Paris" + (f", {coords[0]}, {coords[1]}" if coords else "")

    def step_updates(self):
        done = []
        try:
            sup = self.ha.supervisor("GET", "/supervisor/info")
        except HAError as e:
            raise StepSkipped(f"pas de Supervisor (installation non HAOS ?) : {e.message}")
        if sup.get("update_available"):
            self.ui.say(f"Supervisor {sup.get('version')} → {sup.get('version_latest')}…")
            try:
                self.ha.supervisor("POST", "/supervisor/update", timeout=600)
            except HAError as e:  # le Supervisor redémarre pendant la réponse
                LOG.write(f"mise à jour du Supervisor : {e}")
            self._wait_supervisor()
            after = self.ha.supervisor("GET", "/supervisor/info")
            if after.get("update_available"):
                raise HAError("update", f"Supervisor toujours en {after.get('version')}")
            done.append(f"Supervisor {after.get('version')}")
        core = self.ha.supervisor("GET", "/core/info")
        if core.get("update_available"):
            self.ui.say(f"Core {core.get('version')} → {core.get('version_latest')} (quelques minutes)…")
            self._update_and_wait("/core/update", {"backup": False}, "la mise à jour du Core")
            after = self.ha.supervisor("GET", "/core/info")
            if after.get("update_available"):
                raise HAError("update", f"Core toujours en {after.get('version')} : regarde le journal du Supervisor")
            done.append(f"Core {after.get('version')}")
        osinfo = self.ha.supervisor("GET", "/os/info")
        if osinfo.get("update_available"):
            self.ui.say(f"OS {osinfo.get('version')} → {osinfo.get('version_latest')} (la VM redémarre)…")
            self._update_and_wait("/os/update", None, "le redémarrage de la VM")
            after = self.ha.supervisor("GET", "/os/info")
            if after.get("update_available"):
                raise HAError("update", f"OS toujours en {after.get('version')} : regarde le journal du Supervisor")
            done.append(f"OS {after.get('version')}")
        return ", ".join(done) or "tout est à jour"

    def _update_and_wait(self, endpoint: str, data, what: str):
        """Lance une mise à jour qui redémarre Home Assistant et attend la NOUVELLE instance."""
        try:
            self.ha.supervisor("POST", endpoint, data, timeout=1800)
        except HAError as e:
            if e.code not in ("ws", "timeout"):
                raise
        else:
            if not self.ha.wait_ws_drop(900):
                raise HAError("update", f"{endpoint} : Home Assistant n'a pas redémarré en 15 min")
        self.ha.wait_up(1800, what)

    def _wait_supervisor(self):
        time.sleep(10)
        deadline = time.time() + 600
        while time.time() < deadline:
            try:
                self.ha.supervisor("GET", "/supervisor/ping", timeout=10)
                return
            except HAError:
                time.sleep(5)
        raise HAError("timeout", "le Supervisor ne répond plus")

    # -- comptes --
    def step_users(self):
        users = self.ha.call("config/auth/list")
        by_name = {(u.get("username") or "").casefold(): u for u in users if u.get("username")}
        persons = self.ha.call("person/list")
        person_users = {p.get("user_id") for p in (persons.get("storage") or []) + (persons.get("config") or [])}
        wanted = []
        for p in PROFILES[self.profile]["people"]:
            if p.get("tenant"):
                name = self.ui.ask("tenant_name", "Locataire", "Nom du locataire (affiché) :", default="Locataire")
                username = self.ui.ask("tenant_username", "Locataire",
                                       "Identifiant UNIQUE du locataire (ex. dupont-bien1).\n"
                                       "Jamais « locataire » ou « invite » : deux biens reliés le confondraient.",
                                       default=f"{slugify(name)}-{self.instance_slug}"[:32],
                                       validate=lambda v: self.check_username(v) or (
                                           "Trop générique : ajoute le nom et le bien." if v in ("locataire", "invite", "guest", "user") else None))
                wanted.append({"name": name, "username": username, "admin": False, "person": True})
            else:
                username = self.ui.ask(f"user.{p['name']}", f"Compte de {p['name']}",
                                       f"Identifiant de connexion de {p['name']}\n(le MÊME que sur ses autres instances) :",
                                       default=self.conf.get(f"username.{p['name']}", p["username"]),
                                       validate=self.check_username)
                self.conf[f"username.{p['name']}"] = username
                wanted.append({"name": p["name"], "username": username, "admin": p["admin"], "person": True})
        if self.profile == "perso":
            while self.ui.yesno("perso.add", "Comptes", "Ajouter un compte de personne ?", default=False):
                name = self.ui.ask("perso.name", "Compte", "Nom affiché :")
                username = self.ui.ask("perso.username", "Compte", "Identifiant :", default=slugify(name),
                                       validate=self.check_username)
                admin = self.ui.yesno("perso.admin", "Compte", f"{name} administrateur ?", default=False)
                wanted.append({"name": name, "username": username, "admin": admin, "person": True})
        if "ha_mcp" in self.steps:
            wanted.append({"name": "Claude", "username": "claude", "admin": True, "person": False, "show": True})
            wanted.append({"name": "HA-MCP Server", "username": "ha-mcp", "admin": True, "local_only": True,
                           "person": False, "show": False, "service": True})
        self.save_conf()
        made, kept = [], []
        for w in wanted:
            existing = by_name.get(w["username"].casefold())
            if existing:
                kept.append(w["username"])
                user_id = existing["id"]
            else:
                pwd = gen_password()
                res = self.ha.call("config/auth/create", name=w["name"],
                                   group_ids=["system-admin" if w["admin"] else "system-users"],
                                   local_only=bool(w.get("local_only")))
                user_id = res["user"]["id"]
                try:
                    self.ha.call("config/auth_provider/homeassistant/create", user_id=user_id,
                                 username=w["username"], password=pwd)
                except HAError:
                    self.ha.call("config/auth/delete", user_id=user_id)  # pas de compte orphelin
                    raise
                made.append(w["username"])
                if w.get("service"):
                    self.service_password = pwd
                elif w.get("show", True):
                    self.secrets_out.append((f"{w['name']} (identifiant {w['username']})", pwd))
            if w.get("person") and user_id not in person_users:
                self.ha.call("person/create", name=w["name"], user_id=user_id)
        if made:
            self.next_steps.append("Donne à chacun son mot de passe provisoire (écran suivant) ; "
                                   "il le changera dans son profil.")
        return f"créés : {', '.join(made) or 'aucun'} ; déjà là : {', '.join(kept) or 'aucun'}"

    # -- apps (add-ons) --
    def store_addon(self, base_slug: str, repo_urls: list[str] | None = None) -> dict:
        def find():
            store = self.ha.supervisor("GET", "/store", timeout=60)
            for a in store.get("addons") or []:
                slug = a.get("slug", "")
                if slug == base_slug or slug.endswith("_" + base_slug):
                    if a.get("installed") or not repo_urls or self._repo_match(store, a.get("repository"), repo_urls):
                        return a
            return None
        a = find()
        if a:
            return a
        for url in repo_urls or []:
            try:
                self.ha.supervisor("POST", "/store/repositories", {"repository": url}, timeout=120)
            except HAError as e:
                if "already" not in e.message.lower() and "exist" not in e.message.lower():
                    self.ui.warn(f"dépôt {url} : {e.message}")
                    continue
            a = find()
            if a:
                return a
        raise HAError("store", f"app « {base_slug} » introuvable dans la boutique")

    @staticmethod
    def _repo_match(store: dict, repo_slug, urls: list[str]) -> bool:
        for r in store.get("repositories") or []:
            if r.get("slug") == repo_slug:
                src = (r.get("source") or r.get("url") or "").rstrip("/").lower()
                return any(src == u.rstrip("/").lower() for u in urls)
        return False

    def ensure_addon(self, base_slug: str, label: str, repo_urls=None, options: dict | None = None,
                     start: bool = True, boot: str | None = "auto") -> dict:
        a = self.store_addon(base_slug, repo_urls)
        slug = a["slug"]
        if not a.get("installed"):
            self.ui.say(f"Installation de l'app {label}…")
            self.ha.supervisor("POST", f"/store/addons/{slug}/install", {"background": True}, timeout=60)
            deadline = time.time() + 900
            while True:
                info = self.ha.supervisor("GET", f"/addons/{slug}/info")
                if info.get("version"):
                    break
                if time.time() > deadline:
                    raise HAError("timeout", f"installation de {label} trop longue")
                time.sleep(5)
            self.ui.ok(f"{label} installée")
        info = self.ha.supervisor("GET", f"/addons/{slug}/info")
        body = {}
        if options is not None:
            merged = dict(info.get("options") or {})
            merged.update(options)
            if merged != (info.get("options") or {}):
                body["options"] = merged
        if boot and info.get("boot") != boot:
            body["boot"] = boot
        if boot == "auto" and info.get("watchdog") is False:
            body["watchdog"] = True
        if body:
            self.ha.supervisor("POST", f"/addons/{slug}/options", body)
        if start:
            if info.get("state") == "started" and "options" in body:
                self.ha.supervisor("POST", f"/addons/{slug}/restart", timeout=180)
            elif info.get("state") != "started":
                self.ha.supervisor("POST", f"/addons/{slug}/start", timeout=180)
        return self.ha.supervisor("GET", f"/addons/{slug}/info")

    # -- boutique : Marketplace intégrée (HA 2026.11+) ou HACS (avant) --
    def store_kind(self) -> str:
        if not hasattr(self, "_store_kind"):
            self._store_kind = "marketplace" if self.ha.has_integration("marketplace") else "hacs"
        return self._store_kind

    def step_store(self):
        if self.store_kind() == "marketplace":
            return self.marketplace_ready()
        return self.legacy_hacs()

    def marketplace_ready(self) -> str:
        deadline = time.time() + 600
        shown = 0.0
        info = {}
        while time.time() < deadline:
            loaded = [e for e in self.ha.entries("marketplace") if e.get("state") == "loaded"]
            if loaded:
                try:
                    info = self.ha.call("marketplace/info")
                except HAError:
                    info = {}
                if info.get("disabled_reason"):
                    raise HAError("marketplace", f"Marketplace désactivée : {info['disabled_reason']}")
                if info.get("stage") == "running" and self.ha.call("marketplace/repositories/list", wait=60):
                    break
            elif any(e.get("disabled_by") for e in self.ha.entries("marketplace")):
                raise HAError("marketplace", "Marketplace désactivée dans Paramètres → Intégrations")
            if time.time() - shown > 30:
                self.ui.say("… la Marketplace charge son catalogue")
                shown = time.time()
            time.sleep(5)
        else:
            raise HAError("marketplace", "catalogue de la Marketplace non chargé en 10 min (accès Internet de la VM ?)")
        if not info.get("warning_accepted"):
            if not self.ui.yesno("store_warning", "Marketplace",
                                 "La Marketplace (l'ancien HACS, intégré à Home Assistant) installe du code "
                                 "communautaire que Home Assistant ne vérifie pas.\n\n"
                                 "Accepter cet avertissement pour ton compte ?\n"
                                 "(Nécessaire pour installer HA-MCP et Remote Home Assistant.)"):
                raise StepSkipped("avertissement de la Marketplace non accepté")
            self.ha.call("marketplace/warning/accept")
        gh = "GitHub relié" if info.get("github_connected") else "GitHub non relié (inutile pour le catalogue)"
        return f"Marketplace prête, {gh}"

    def legacy_hacs(self) -> str:
        if not self.ha.has_integration("hacs"):
            a = self.ensure_addon("get", "Get HACS", [HACS_ADDONS_REPO], start=False, boot=None)
            slug = a["slug"]
            self.ui.say("Téléchargement de HACS par l'app Get HACS…")
            self.ha.supervisor("POST", f"/addons/{slug}/start", timeout=180)
            deadline = time.time() + 300
            while time.time() < deadline:
                time.sleep(4)
                if self.ha.supervisor("GET", f"/addons/{slug}/info").get("state") != "started":
                    break
            self.ha.restart("chargement de HACS")
            if not self.ha.has_integration("hacs"):
                raise HAError("hacs", "HACS n'a pas été posé : ouvre le journal de l'app Get HACS")
        if not self.ha.entries("hacs"):
            r = self.ha.flow_start("hacs")
            if r.get("type") == "abort":
                raise HAError("hacs", f"assistant interrompu : {r.get('reason')}")
            fid = r["flow_id"]
            if r.get("type") == "form" and r.get("step_id") == "user":
                r = self.ha.flow_step(fid, {"acc_logs": True, "acc_addons": True, "acc_untested": True,
                                            "acc_disable": True})
            self.github_device(fid, r, lambda: bool(self.ha.entries("hacs")), "HACS")
        deadline = time.time() + 600
        while time.time() < deadline:
            try:
                if self.ha.call("hacs/info").get("stage") == "running" and \
                        self.ha.call("hacs/repositories/list", wait=60):
                    return "HACS installé et relié à GitHub"
            except HAError:
                pass
            time.sleep(5)
        raise HAError("hacs", "catalogue HACS non chargé en 10 min")

    def github_device(self, fid: str, r: dict, done, who: str):
        """Fait suivre le code GitHub à saisir, attend l'autorisation."""
        try:
            if not r or r.get("type") not in ("progress", "progress_done"):
                raise HAError("github", f"étape inattendue : {r}")
            ph = r.get("description_placeholders") or {}
            code, url = ph.get("code", "?"), ph.get("url", "https://github.com/login/device")
            print(f"\n   \033[1;33mCode GitHub : {code}\033[0m   ({url})\n", flush=True)
            self.ui.info("github_code", f"Relier {who} à GitHub (1 min)",
                         f"1. Ouvre {url}\n   (connecté avec ton compte GitHub Basilik41)\n"
                         f"2. Saisis le code :   {code}\n3. Clique « Authorize ».\n\n"
                         "Puis valide ici : le script vérifie tout seul.")
            deadline = time.time() + 900
            while time.time() < deadline:
                r = self.ha.flow_step(fid)
                if r is None or r.get("type") == "create_entry" or \
                        (r.get("type") == "abort" and r.get("reason") in ("reconfigure_successful", "reauth_successful")):
                    break
                if r.get("type") == "progress_done":
                    r = self.ha.flow_step(fid, {})
                    continue
                if r.get("type") == "abort":
                    raise HAError("github", f"GitHub a refusé : {r.get('reason')}")
                time.sleep(4)
            if not done():
                raise HAError("github", "autorisation GitHub non reçue en 15 min : relance l'étape")
        except Exception:
            self.ha.flow_abort(fid)
            raise
        self.ui.ok(f"GitHub relié à {who}")

    def marketplace_github(self):
        if self.ha.call("marketplace/info").get("github_connected"):
            return
        fid = self.ha.call("marketplace/github/connect")["flow_id"]
        r = self.ha.flow_step(fid)
        if r and r.get("type") == "form":
            r = self.ha.flow_step(fid, {})
        self.github_device(fid, r, lambda: self.ha.call("marketplace/info").get("github_connected"), "la Marketplace")

    def store_install(self, names: list[str], category: str = "integration") -> str:
        """Installe le premier dépôt trouvé parmi `names` (catalogue, puis ajout en dépôt personnalisé)."""
        mk = self.store_kind() == "marketplace"
        pre = "marketplace" if mk else "hacs"

        def lookup():
            for r in self.ha.call(f"{pre}/repositories/list", wait=60) or []:
                if (r.get("full_name") or "").lower() in [n.lower() for n in names]:
                    return r
            return None
        repo = lookup()
        if not repo:
            if mk:
                self.marketplace_github()  # un dépôt personnalisé exige GitHub relié
            try:
                self.ha.call(f"{pre}/repositories/add", repository=names[-1], category=category, wait=120)
            except HAError as e:
                if e.code != "repository_exists":
                    raise
            for _ in range(30):
                repo = lookup()
                if repo:
                    break
                time.sleep(2)
        if not repo:
            raise HAError("store", f"{names[-1]} introuvable dans la boutique")
        if repo.get("installed"):
            return f"{repo['full_name']} déjà installé ({repo.get('installed_version')})"
        self.ui.say(f"Installation de {repo['full_name']}…")
        for attempt in (1, 2):
            try:
                if mk:
                    self.ha.call("marketplace/repository/install", repository=str(repo["id"]), wait=600)
                else:
                    self.ha.call("hacs/repository/download", repository=str(repo["id"]), wait=600)
                break
            except HAError as e:
                if mk and e.code == "github_rate_limited" and attempt == 1:
                    self.ui.warn("GitHub limite les téléchargements anonymes : on relie la Marketplace à ton compte GitHub")
                    self.marketplace_github()
                    continue
                raise
        self.need_restart = True
        return f"{repo['full_name']} installé"

    def require_store(self):
        if self.store_kind() == "marketplace":
            if not self.ha.call("marketplace/info").get("warning_accepted"):
                raise StepSkipped("Marketplace pas prête (étape Boutique)")
        elif not self.ha.entries("hacs"):
            raise StepSkipped("HACS n'est pas en place")

    # -- téléchargements groupés, puis un seul redémarrage --
    def step_downloads(self):
        out = []
        if "ha_mcp" in self.steps and not self.ha.has_integration("ha_mcp_tools"):
            self.require_store()
            out.append(self.store_install(HA_MCP_REPOS))
        if "remote_ha" in self.steps and not self.ha.has_integration("remote_homeassistant"):
            self.require_store()
            out.append(self.store_install([REMOTE_HA_REPO]))
        if "acces_distant" in self.steps:
            out.append(self.install_acces_distant())
        return "; ".join(out) or "rien à télécharger"

    def install_acces_distant(self) -> str:
        token = self.vault.get("github_token")
        while True:
            if not token:
                token = self.ui.ask("github_token", "Jeton GitHub",
                                    f"Le dépôt {ACCES_DISTANT_REPO} est privé.\n\n"
                                    "Colle le jeton GitHub « fine-grained » en lecture seule sur ce dépôt\n"
                                    "(le même que dans secrets.yaml de la Grange : github_acces_distant).\n\n"
                                    "GitHub → Settings → Developer settings → Fine-grained tokens.",
                                    secret=True)
            s, p = http("GET", f"{GITHUB_API}/repos/{ACCES_DISTANT_REPO}/commits/main",
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            if s == 200:
                break
            self.ui.error_box("github_token", "Jeton refusé",
                              f"GitHub refuse ce jeton (HTTP {s}) : expiré, ou sans accès à {ACCES_DISTANT_REPO}.")
            token = None
            self.vault.pop("github_token", None)
        self.remember_secret("github_token", token, "le jeton GitHub (lecture seule)")
        sha = (p.get("sha") or "")[:7]
        out = self.file_editor_python(REMOTE_INSTALLER, {"token": token, "repo": ACCES_DISTANT_REPO,
                                                         "ref": "main", "component": "acces_distant",
                                                         "api": GITHUB_API})
        if not out.get("ok"):
            raise HAError("acces_distant", out.get("error", "échec"))
        if out.get("unchanged"):
            return f"Accès distant {out.get('version')} déjà à jour"
        self.need_restart = True
        prev = f" (ancienne {out['previous']} gardée en .acces_distant_precedent)" if out.get("previous") else ""
        return f"Accès distant {out.get('version')} posé, commit {sha}{prev}"

    def file_editor_python(self, code: str, params: dict) -> dict:
        info = self.ensure_addon("configurator", "File editor", start=True, boot="auto")
        entry = info.get("ingress_entry") or info.get("ingress_url")
        if not entry:
            raise HAError("file_editor", "accès ingress du File editor introuvable")
        entry = entry.rstrip("/")
        for _ in range(30):
            if self.ha.supervisor("GET", f"/addons/{info['slug']}/info").get("state") == "started":
                break
            time.sleep(2)
        sess = self.ha.supervisor("POST", "/ingress/session")
        session = sess.get("session")
        LOG.hide(session)
        headers = {"Cookie": f"ingress_session={session}"}

        def post(path: str, form: dict, timeout=60):
            for _ in range(10):
                s, p = http("POST", f"{self.ha.base}{entry}{path}", form=form, headers=headers,
                            local=True, timeout=timeout)
                if s == 200 and isinstance(p, dict):
                    return p
                if s in (502, 503):  # app encore en démarrage
                    time.sleep(3)
                    continue
                raise HAError("file_editor", f"{path} : HTTP {s}")
            raise HAError("file_editor", "File editor ne répond pas")
        # dossier de configuration vu par l'app : /homeassistant (récent) ou /config (ancien)
        for root in ("/homeassistant", "/config"):
            p = post("/api/save", {"filename": f"{root}/.hab_params.json", "text": json.dumps(params)})
            if not p.get("error"):
                break
        else:
            raise HAError("file_editor", p.get("message", "écriture refusée"))
        p = post("/api/save", {"filename": f"{root}/.hab_helper.py", "text": code})
        if p.get("error"):
            raise HAError("file_editor", p.get("message", "écriture refusée"))
        try:
            res = post("/api/exec_command", {"command": f"python3 {root}/.hab_helper.py", "timeout": "150"}, timeout=180)
        finally:
            post("/api/exec_command", {"command": f"rm -f {root}/.hab_helper.py {root}/.hab_params.json"})
        if res.get("error"):
            raise HAError("file_editor", res.get("message", "commande refusée"))
        lines = [l for l in (res.get("stdout") or "").strip().splitlines() if l.startswith("{")]
        if not lines:
            raise HAError("file_editor", f"aucun résultat : {LOG.clean(res.get('stderr') or '')[-300:]}")
        return json.loads(lines[-1])

    # -- point de redémarrage (+ proxy inverse pour le tunnel) --
    def restart_point(self):
        want_proxy = "cloudflare" in self.steps
        if want_proxy:
            changed = self.ensure_reverse_proxy()
            if changed:
                self.need_restart = False
                return "proxy inverse autorisé (redémarré)"
        if self.need_restart:
            self.ha.restart("nouvelles intégrations")
            self.need_restart = False
            return "redémarré"
        return "aucun redémarrage nécessaire"

    def ensure_reverse_proxy(self) -> bool:
        """Autorise le proxy Cloudflared (172.30.33.0/24). True si un redémarrage a eu lieu."""
        try:
            cur = self.ha.call("http/config")
        except HAError as e:
            if e.code == "unknown_command":
                raise HAError("http", "Home Assistant trop ancien pour régler le proxy sans YAML : fais d'abord la mise à jour")
            raise
        if cur.get("pending") and not cur["pending"].get("error"):
            # un essai précédent attend sa confirmation
            pend = cur["pending"]
            if pend.get("use_x_forwarded_for") and DOCKER_NET in (pend.get("trusted_proxies") or []):
                self.ha.call("http/config/promote")
                self.ui.ok("Réglage du proxy confirmé")
                return False
        stable = cur.get("stable") or {}
        if stable.get("use_x_forwarded_for") and DOCKER_NET in (stable.get("trusted_proxies") or []):
            return False
        allowed = ("server_host", "server_port", "ssl_certificate", "ssl_peer_certificate", "ssl_key",
                   "cors_allowed_origins", "use_x_forwarded_for", "trusted_proxies", "login_attempts_threshold",
                   "ip_ban_enabled", "ssl_profile", "use_x_frame_options")
        base = stable or cur.get("default") or {}
        new = {k: v for k, v in base.items() if k in allowed}
        new.setdefault("server_port", 8123)
        new["use_x_forwarded_for"] = True
        new["trusted_proxies"] = sorted(set((new.get("trusted_proxies") or []) + [DOCKER_NET]))
        self.ui.say("Autorisation du proxy Cloudflared (Home Assistant redémarre)…")
        self.ha.ws_connect()
        res = self.ha.call("http/config/configure", config=new)
        if res.get("restart"):
            self.ha.wait_ws_drop()
            self.ha.wait_up(what="le redémarrage")
        after = self.ha.call("http/config")
        pend = after.get("pending")
        if pend and pend.get("error"):
            raise HAError("http", f"réglage refusé au démarrage : {pend.get('error_message') or pend.get('error')}")
        if pend:
            self.ha.call("http/config/promote")
        self.ui.ok("Proxy Cloudflared autorisé et confirmé")
        return True

    # -- HA-MCP --
    def step_ha_mcp(self):
        if not self.ha.has_integration("ha_mcp_tools"):
            raise StepSkipped("intégration HA-MCP absente (étape HACS ?)")
        entries = self.mcp_server_entries()
        if not entries:
            pwd = getattr(self, "service_password", None)
            if not pwd:
                users = self.ha.call("config/auth/list")
                svc = next((u for u in users if (u.get("username") or "") == "ha-mcp"), None)
                if not svc:
                    raise HAError("ha_mcp", "compte de service ha-mcp absent : coche l'étape Comptes")
                if not svc.get("local_only") or "system-admin" not in (svc.get("group_ids") or []):
                    raise HAError("ha_mcp", "le compte ha-mcp existe mais n'est pas « administrateur, accès local "
                                  "seulement » : corrige-le dans Paramètres → Personnes, puis Réessayer")
                if not self.me.get("is_owner"):
                    raise HAError("ha_mcp", "seul le compte propriétaire peut renouveler le mot de passe du compte "
                                  "ha-mcp : relance le script avec ton compte")
                pwd = gen_password()
                self.ha.call("config/auth_provider/homeassistant/admin_change_password", user_id=svc["id"], password=pwd)
            r = self.ha.flow_start("ha_mcp_tools")
            fid = r.get("flow_id")
            try:
                if r.get("type") == "menu":
                    r = self.ha.flow_step(fid, {"next_step_id": "server"})
                if r.get("type") == "abort":
                    raise HAError("ha_mcp", f"refus : {r.get('reason')}")
                if r.get("type") != "form":
                    raise HAError("ha_mcp", f"étape inattendue : {r}")
                r = self.ha.flow_step(fid, {"admin_username": "ha-mcp", "admin_password": pwd,
                                            "remote_access": "ha_auth", "bind_host": "127.0.0.1"})
                if not r or r.get("type") != "create_entry":
                    raise HAError("ha_mcp", f"création refusée : {(r or {}).get('errors') or r}")
            except Exception:
                if fid:
                    self.ha.flow_abort(fid)
                raise
            self.ui.ok("Serveur HA-MCP créé (connexion Home Assistant pour claude.ai)")
            entries = self.mcp_server_entries()
        entry = entries[0]
        extra = ""
        if not any(e["entry_id"] != entry["entry_id"] for e in self.ha.entries("ha_mcp_tools")):
            # entrée « File & YAML Tools » : lecture des fichiers et du YAML par Claude (comme sur la Maison)
            r = self.ha.flow_start("ha_mcp_tools")
            fid = r.get("flow_id")
            try:
                if r.get("type") == "menu":
                    r = self.ha.flow_step(fid, {"next_step_id": "tools"})
                if r and r.get("type") == "form":
                    r = self.ha.flow_step(fid, {})
                if r and r.get("type") == "create_entry":
                    extra = " + outils fichiers/YAML"
                elif r and r.get("type") == "abort" and r.get("reason") == "already_configured":
                    pass
                else:
                    self.ui.warn(f"outils fichiers/YAML non ajoutés : {r}")
            except HAError as e:
                self.ui.warn(f"outils fichiers/YAML non ajoutés : {e.message}")
                if fid:
                    self.ha.flow_abort(fid)
        for _ in range(12):  # le chemin n'existe qu'une fois le serveur démarré
            self.webhook_path = self.mcp_webhook(entry["entry_id"])
            if self.webhook_path:
                break
            time.sleep(5)
        else:
            self.next_steps.append("HA-MCP : chemin du webhook non lu ; il est dans Intégrations → HA-MCP Server → Configurer.")
        if self.webhook_path:
            self.conf[f"webhook.{self.instance_slug}"] = self.webhook_path
            self.save_conf()
        return "serveur actif" + extra + (f", chemin {self.webhook_path[:22]}…" if self.webhook_path else "")

    def mcp_server_entries(self) -> list:
        """Entrée « serveur » de HA-MCP (titre d'origine « HA-MCP Server » ; l'autre est « File & YAML Tools »)."""
        return [e for e in self.ha.entries("ha_mcp_tools")
                if "server" in (e.get("title") or "").lower() or "tools" not in (e.get("title") or "").lower()]

    def mcp_webhook(self, entry_id: str) -> str | None:
        s, r = self.ha.req("POST", "/api/config/config_entries/options/flow",
                           json_body={"handler": entry_id, "show_advanced_options": False}, timeout=60)
        if s != 200 or not isinstance(r, dict):
            return None
        try:
            blob = json.dumps(r.get("description_placeholders") or {})
            m = re.search(r"/api/webhook/([A-Za-z0-9_\-]{8,})", blob)
            return f"/api/webhook/{m.group(1)}" if m else None
        finally:
            if r.get("flow_id"):
                self.ha.flow_abort(r["flow_id"], options=True)

    # -- Accès distant --
    def step_acces_distant(self):
        if not self.ha.has_integration("acces_distant"):
            raise StepSkipped("fichiers Accès distant absents")
        if self.ha.entries("acces_distant"):
            return "déjà configurée"
        r = self.ha.flow_start("acces_distant")
        fid = r.get("flow_id")
        try:
            if r.get("type") == "menu":
                r = self.ha.flow_step(fid, {"next_step_id": "reglages"})
            if r.get("type") == "form":
                r = self.ha.flow_step(fid, {})
            if not r or r.get("type") != "create_entry":
                raise HAError("acces_distant", f"création refusée : {r}")
        except Exception:
            if fid:
                self.ha.flow_abort(fid)
            raise
        self.next_steps.append("Accès distant : code des réglages, Wi-Fi et sites reliés → je m'en occupe par le MCP.")
        return "mode « réglages » (certificats gérés depuis la Grange)"

    # -- Cloudflare --
    def step_cloudflare(self):
        cf = self.cloudflare_client()
        zone_hint = self.conf.get("zone", "")
        default_host = f"ha-{self.instance_slug}.{zone_hint}" if zone_hint else ""
        host = self.ui.ask("host", "Adresse publique", "Sous-domaine public de cette instance :",
                           default=self.conf.get(f"host.{self.instance_slug}", default_host),
                           validate=lambda v: None if HOST_RE.match(v.lower()) else "Exemple : ha-centrale.mondomaine.fr").lower()
        zone = cf.find_zone(host)
        zid, zname = zone["id"], zone["name"]
        acc = (zone.get("account") or {}).get("id")
        if not acc:
            raise HAError("cloudflare", "compte Cloudflare de la zone introuvable")
        self.conf["zone"] = zname
        self.conf[f"host.{self.instance_slug}"] = host
        self.save_conf()
        self.public_host = host
        tname = host
        self.preflight(cf, zid, acc, host)
        if self.webhook_path is None:
            self.webhook_path = self.conf.get(f"webhook.{self.instance_slug}")

        # 0. le sous-domaine est-il libre ? (lecture seule, avant toute modification)
        recs = cf.req("GET", f"/zones/{zid}/dns_records", params={"name": host}) or []
        rec = recs[0] if recs else None
        if rec and not (rec.get("type") == "CNAME" and str(rec.get("content", "")).endswith(".cfargotunnel.com")
                        and self._tunnel_named(cf, acc, rec["content"], tname)):
            if not self.ui.yesno("dns_replace", "Enregistrement DNS existant",
                                 f"{host} existe déjà ({rec.get('type')} → {rec.get('content')}).\n\n"
                                 "Le remplacer par le tunnel de cette instance ?", default=False):
                raise StepSkipped(f"{host} déjà utilisé : choisis un autre sous-domaine")
        steps = []
        # 1. protections AVANT d'ouvrir l'accès
        steps.append(self.cf_mtls(cf, zid, host))
        steps.append(self.cf_waf(cf, zid, host))
        # 2. tunnel
        tunnels = cf.req("GET", f"/accounts/{acc}/cfd_tunnel", params={"name": tname, "is_deleted": "false"}) or []
        if tunnels:
            tid = tunnels[0]["id"]
        else:
            t = cf.req("POST", f"/accounts/{acc}/cfd_tunnel", {"name": tname, "config_src": "cloudflare"})
            tid = t["id"]
            self.ui.ok(f"Tunnel {tname} créé")
        cf.req("PUT", f"/accounts/{acc}/cfd_tunnel/{tid}/configurations",
               {"config": {"ingress": [{"hostname": host, "service": "http://homeassistant:8123",
                                        "originRequest": {}},
                                       {"service": "http_status:404"}]}})
        ttoken = cf.req("GET", f"/accounts/{acc}/cfd_tunnel/{tid}/token")
        LOG.hide(ttoken)
        # 3. DNS
        target = f"{tid}.cfargotunnel.com"
        if rec is None:
            cf.req("POST", f"/zones/{zid}/dns_records",
                   {"type": "CNAME", "name": host, "content": target, "proxied": True,
                    "comment": f"ha-bootstrap : tunnel {tname}"})
        elif not (rec.get("type") == "CNAME" and rec.get("content") == target):
            cf.req("PATCH", f"/zones/{zid}/dns_records/{rec['id']}",
                   {"type": "CNAME", "name": host, "content": target, "proxied": True})
        steps.append("DNS")
        # 4. app Cloudflared
        self.ensure_addon("cloudflared", "Cloudflared", CLOUDFLARED_REPOS, options={"tunnel_token": ttoken})
        deadline = time.time() + 240
        status = "?"
        while time.time() < deadline:
            status = (cf.req("GET", f"/accounts/{acc}/cfd_tunnel/{tid}") or {}).get("status")
            if status == "healthy":
                break
            time.sleep(6)
        if status != "healthy":
            raise HAError("cloudflare", f"tunnel « {status} » : regarde le journal de l'app Cloudflared")
        steps.append("tunnel actif")
        # 5. adresses connues de Home Assistant
        self.ha.call("config/core/update", external_url=f"https://{host}", internal_url=f"http://{self.ha_ip}:8123")
        # 6. contrôle : sans certificat, Cloudflare doit refuser
        verdict = self.public_check(host)
        steps.append(verdict)
        return ", ".join(s for s in steps if s)

    @staticmethod
    def _tunnel_named(cf: Cloudflare, acc: str, cname: str, name: str) -> bool:
        tid = cname.split(".cfargotunnel.com")[0]
        try:
            t = cf.req("GET", f"/accounts/{acc}/cfd_tunnel/{tid}") or {}
        except CFError:
            return False
        return t.get("name") == name and not t.get("deleted_at")

    def cloudflare_client(self) -> Cloudflare:
        token = self.vault.get("cloudflare_token")
        while True:
            if not token:
                token = self.ui.ask("cf_token", "Jeton Cloudflare",
                                    "Crée UNE FOIS un jeton : Cloudflare → Mon profil → Jetons API → Créer un jeton\n"
                                    "→ « Créer un jeton personnalisé », nom « ha-bootstrap », permissions :\n\n"
                                    "  Compte · Cloudflare Tunnel · Modifier (Edit)\n"
                                    "  Zone · Zone · Lire (Read)\n"
                                    "  Zone · DNS · Modifier (Edit)\n"
                                    "  Zone · SSL et certificats (SSL and Certificates) · Modifier (Edit)\n"
                                    "  Zone · WAF de zone (Zone WAF) · Modifier (Edit)\n\n"
                                    "Ressources : ton compte, et la zone de tes sous-domaines.\n\n"
                                    "Colle le jeton :", secret=True)
            cf = Cloudflare(token)
            try:
                cf.req("GET", "/zones", params={"per_page": 1})
                break
            except CFError as e:
                self.ui.error_box("cf_token", "Jeton refusé", f"Cloudflare refuse ce jeton :\n{e}")
                token = None
                self.vault.pop("cloudflare_token", None)
        self.remember_secret("cloudflare_token", token, "le jeton Cloudflare")
        return cf

    def preflight(self, cf: Cloudflare, zid: str, acc: str, host: str):
        missing = []
        for label, method, path, params in (
                ("Compte · Cloudflare Tunnel", "GET", f"/accounts/{acc}/cfd_tunnel", {"per_page": 1}),
                ("Zone · DNS", "GET", f"/zones/{zid}/dns_records", {"name": host}),
                ("Zone · SSL et certificats", "GET", f"/zones/{zid}/certificate_authorities/hostname_associations", None),
                ("Zone · WAF de zone", "GET", f"/zones/{zid}/rulesets", None)):
            try:
                cf.req(method, path, params=params)
            except CFError as e:
                if e.status in (401, 403) or e.codes & {9109, 10000}:
                    missing.append(label)
        if missing:
            raise HAError("cloudflare", "le jeton n'a pas ces permissions : " + ", ".join(missing)
                          + ". Ajoute-les au jeton (Mon profil → Jetons API → Modifier), puis Réessayer.")

    def cf_mtls(self, cf: Cloudflare, zid: str, host: str) -> str:
        cur = cf.req("GET", f"/zones/{zid}/certificate_authorities/hostname_associations") or {}
        hosts = list(cur.get("hostnames") or [])
        if host in hosts:
            return "certificat déjà demandé"
        cf.req("PUT", f"/zones/{zid}/certificate_authorities/hostname_associations", {"hostnames": hosts + [host]})
        self.ui.ok(f"Cloudflare demande maintenant le certificat client sur {host}")
        return "certificat client demandé"

    # règles WAF : on clone tes deux règles existantes pour le nouvel hôte
    HOST_COND = re.compile(r'http\.host\s+(?:eq\s+"[^"]+"|in\s+\{[^}]*\})')
    WEBHOOK_COND = re.compile(r'http\.request\.uri\.path\s+eq\s+"/api/webhook/[^"]+"')

    def _host_of(self, expr: str) -> str | None:
        m = re.search(r'http\.host\s+eq\s+"([^"]+)"', expr) or re.search(r'http\.host\s+in\s+\{\s*"([^"]+)"', expr)
        return m.group(1) if m else None

    def _clone_expr(self, expr: str, host: str) -> str:
        e = self.HOST_COND.sub(f'http.host eq "{host}"', expr, count=1)
        if self.webhook_path:
            e = self.WEBHOOK_COND.sub(f'http.request.uri.path eq "{self.webhook_path}"', e)
        else:
            e = self.WEBHOOK_COND.sub('starts_with(http.request.uri.path, "/api/webhook/")', e)
        return e

    def cf_waf(self, cf: Cloudflare, zid: str, host: str) -> str:
        try:
            rs = cf.req("GET", f"/zones/{zid}/rulesets/phases/http_request_firewall_custom/entrypoint")
        except CFError as e:
            if e.status == 404:
                rs = None
            else:
                raise
        rules = (rs or {}).get("rules") or []
        tblock = next((r for r in rules if r.get("action") == "block" and "cf.tls_client_auth" in r.get("expression", "")
                       and self._host_of(r.get("expression", "")) and self._host_of(r["expression"]) != host
                       and "— ha-bootstrap" not in (r.get("description") or "")), None)
        tskip = next((r for r in rules if r.get("action") == "skip" and ANTHROPIC_RANGE in r.get("expression", "")
                      and self._host_of(r.get("expression", "")) and self._host_of(r["expression"]) != host
                      and "— ha-bootstrap" not in (r.get("description") or "")), None)
        if not tblock:
            self.next_steps.append(f"WAF : aucune règle modèle « certificat obligatoire » trouvée. Copie la règle "
                                   f"de la carte Accès distant (Grange) en remplaçant l'hôte par {host}.")
            raise HAError("waf", "aucune règle mTLS existante à reprendre (règle « Ha- externe »)")
        backup_dir = os.path.join(CONF_DIR, "backups")
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
        bpath = os.path.join(backup_dir, f"waf-{zid[:8]}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        save_json(bpath, rs, private=True)

        plan = []
        nblock = {"action": "block", "expression": self._clone_expr(tblock["expression"], host),
                  "description": f"{(tblock.get('description') or 'Certificat obligatoire')} {host} — ha-bootstrap",
                  "enabled": True}
        plan.append(("block", tblock, nblock))
        if tskip:
            nskip = {"action": "skip", "expression": self._clone_expr(tskip["expression"], host),
                     "description": f"{(tskip.get('description') or 'Claude')} {host} — ha-bootstrap",
                     "enabled": True, "action_parameters": tskip.get("action_parameters") or {"ruleset": "current"}}
            if tskip.get("logging"):
                nskip["logging"] = tskip["logging"]
            plan.append(("skip", tskip, nskip))
        else:
            self.next_steps.append("WAF : pas de règle « HA-MCP - Claude » à reprendre ; Claude risque d'être bloqué "
                                   "par la protection anti-robots.")

        existing = {r.get("description"): r for r in rules}
        todo = []
        for kind, tpl, new in plan:
            cur = existing.get(new["description"])
            if cur and cur.get("expression") == new["expression"] and cur.get("action") == new["action"] \
                    and cur.get("enabled", True):
                continue
            todo.append((kind, tpl, new, cur))
        if not todo:
            return "règles WAF déjà en place"
        preview = "\n\n".join(f"« {n['description']} » ({'nouvelle' if not c else 'mise à jour'}, action {n['action']}) :\n"
                              f"{n['expression']}" for _, _, n, c in todo)
        if not self.ui.yesno("waf_confirm", "Règles WAF",
                             f"Ajout de règles pour {host}, copiées de tes règles existantes "
                             f"(qui ne changent pas) :\n\n{preview}\n\nSauvegarde : {bpath}\n\nAppliquer ?"):
            self.next_steps.append(f"WAF non appliqué : {host} est ouvert SANS certificat obligatoire tant que la règle manque.")
            raise StepSkipped("règles WAF refusées")
        rsid = rs["id"]
        after_id = tblock["id"]
        for kind, tpl, new, cur in todo:
            try:
                if cur:
                    res = cf.req("PATCH", f"/zones/{zid}/rulesets/{rsid}/rules/{cur['id']}", new)
                else:
                    res = cf.req("POST", f"/zones/{zid}/rulesets/{rsid}/rules", dict(new, position={"after": after_id}))
            except CFError as e:
                if any(w in e.text for w in ("limit", "maximum", "quota", "exceed")):
                    raise HAError("waf", "Cloudflare refuse une règle de plus (limite de ton offre). "
                                  "Libère une règle ou ajoute l'hôte à la main dans « Ha- externe ».")
                raise
            mine = next((r for r in (res or {}).get("rules") or [] if r.get("description") == new["description"]), None)
            if mine:
                after_id = mine["id"]
        self.ui.ok(f"Règles WAF en place pour {host}")
        return f"règles WAF ({len(todo)}) ajoutées"

    def public_check(self, host: str) -> str:
        url = PUBLIC_CHECK.format(host=host)
        for _ in range(12):
            try:
                s, _, h = http("GET", url, timeout=15, raw=True)
            except NetError:
                time.sleep(10)
                continue
            if s == 403:
                return "vérifié : refusé sans certificat ✔"
            if s == 200:
                self.next_steps.append(f"ATTENTION : {host} répond SANS certificat. Vérifie la règle WAF.")
                return "⚠ accessible sans certificat"
            time.sleep(10)
        self.next_steps.append(f"Vérifie depuis ton téléphone (4G, avec certificat) que https://{host} s'ouvre.")
        return "contrôle public impossible d'ici (DNS pas encore propagé)"

    # ═════════════ fin ═════════════
    def finish(self):
        self.ui.section("Bilan")
        lines = []
        for title, state, detail in self.report:
            mark = {"ok": "✔", "passé": "–", "erreur": "✖"}.get(state, "?")
            lines.append(f"{mark} {title} : {detail}")
        if self.public_host and self.webhook_path:
            self.next_steps.insert(0, "Ajouter Claude : claude.ai → Paramètres → Connecteurs → Ajouter un connecteur "
                                   f"personnalisé\n   Nom : HA {self.instance_name}\n"
                                   f"   URL : https://{self.public_host}{self.webhook_path}\n"
                                   "   Connexion avec le compte « claude » (depuis un appareil qui a ton certificat).")
        self.next_steps.append("Active la double authentification sur ton compte (Profil → Sécurité).")
        self.next_steps.append("Dis à Claude « c'est branché » : liaisons, tableaux de bord et réglages fins.")
        text = "\n".join(lines) + "\n\nÀ faire maintenant :\n" + "\n".join(f"• {s}" for s in self.next_steps)
        for line in text.split("\n"):
            print("  " + line)
        rep_dir = os.path.join(CONF_DIR, "rapports")
        os.makedirs(rep_dir, mode=0o700, exist_ok=True)
        rpath = os.path.join(rep_dir, f"{self.instance_slug or 'ha'}-{datetime.now().strftime('%Y%m%d-%H%M')}.txt")
        with open(rpath, "w") as f:
            f.write(LOG.clean(text) + "\n")
        os.chmod(rpath, 0o600)
        self.ui.info("summary", f"Terminé — {self.instance_name}", text + f"\n\nRapport : {rpath}")
        if self.secrets_out:
            sec = "\n".join(f"{who} :\n   {pwd}" for who, pwd in self.secrets_out)
            self.ui.info("secrets", "Mots de passe provisoires (affichés une seule fois)",
                         sec + "\n\nNote-les dans Vaultwarden MAINTENANT.\nIls ne sont écrits nulle part.")
        self.snapshot("socle", "socle prêt", "snapshot_after", default=True)
        self.close_session()

    def close_session(self):
        self._closed = True
        try:
            for t in self.ha.call("auth/refresh_tokens") or []:
                if t.get("is_current"):
                    self.ha.send_only({"type": "auth/delete_refresh_token", "refresh_token_id": t["id"]})
                    break
        except (HAError, WSClosed, OSError):
            pass
        self.ha.ws_close()

    def run(self):
        print(f"\n\033[1mha-bootstrap {VERSION}\033[0m — socle Home Assistant en une fois\n")
        if LOG.path:
            print(f"  Journal : {LOG.path}")
        self.choose_target()
        self.snapshot("avant", "avant ha-bootstrap", "snapshot_before")
        self.connect()
        self.choose_plan()
        s = self.steps
        if "general" in s:
            self.run_step("general", "Réglages généraux", self.step_general)
        if "updates" in s:
            self.run_step("updates", "Mises à jour", self.step_updates)
        if "users" in s:
            self.run_step("users", "Comptes", self.step_users)
        if "hacs" in s:
            self.run_step("hacs", "Boutique (Marketplace / HACS)", self.step_store)
        if {"ha_mcp", "remote_ha", "acces_distant"} & set(s):
            self.run_step("downloads", "Téléchargement des intégrations", self.step_downloads)
        self.run_step("restart", "Prise en compte (redémarrage)", self.restart_point)
        if "ha_mcp" in s:
            self.run_step("ha_mcp", "HA-MCP (accès de Claude)", self.step_ha_mcp)
        if "acces_distant" in s:
            self.run_step("acces_distant", "Accès distant", self.step_acces_distant)
        if "cloudflare" in s:
            self.run_step("cloudflare", "Cloudflare", self.step_cloudflare)
        self.finish()


def main():
    boot = None
    try:
        boot = Bootstrap()
        boot.run()
    except Abort as e:
        print(f"\n  ■ {e}\n  Relance le script quand tu veux : il reprendra là où il s'est arrêté.")
        if LOG.path:
            print(f"  Journal : {LOG.path}")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n  ■ Interrompu. Relance quand tu veux.")
        sys.exit(130)
    finally:
        if boot and boot.ha and boot.ha.access and not getattr(boot, "_closed", False):
            try:
                boot.close_session()
            except Exception:  # noqa: BLE001 — nettoyage au mieux
                pass


if __name__ == "__main__":
    main()
