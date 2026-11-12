"""Tests unitaires : clonage des règles WAF, validations, détection des modèles."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HAB_CONF_DIR", "/tmp/hab_units")
import ha_bootstrap as hb  # noqa: E402

SKI = "9D9721B71DD6781A5DBBF7421E38392D9FB9402F"
OAUTH = ('starts_with(http.request.uri.path, "/.well-known/")'
         ' or starts_with(http.request.uri.path, "/api/ha_mcp_tools/oauth/")')


def ad_expr(host, webhook=None, token=True, claude=True):
    """Copie de manager.waf_expression() d'Accès distant 1.7."""
    e = (f'(http.host eq "{host}")\nand not (cf.tls_client_auth.cert_verified\n'
         "         and not cf.tls_client_auth.cert_revoked\n"
         f'         and cf.tls_client_auth.cert_issuer_ski eq "{SKI}")')
    if token:
        e += '\nand not (http.request.uri.path eq "/auth/token")'
    if claude:
        w = f'http.request.uri.path eq "{webhook}"' if webhook else 'starts_with(http.request.uri.path, "/api/webhook/")'
        o = OAUTH if token else 'http.request.uri.path eq "/auth/token" or ' + OAUTH
        e += f"\nand not (ip.src in {{160.79.104.0/21}}\n         and ({w}\n              or {o}))"
    return e


def run():
    b = hb.Bootstrap.__new__(hb.Bootstrap)
    ok = fail = 0

    def t(name, cond):
        nonlocal ok, fail
        print(("✔ " if cond else "✖ ") + name)
        ok, fail = ok + bool(cond), fail + (not cond)

    src = ad_expr("ha.exemple.fr", "/api/webhook/ANCIEN123456")
    b.webhook_path = "/api/webhook/NOUVEAU987654"
    out = b._clone_expr(src, "ha-centrale.exemple.fr")
    t("hôte remplacé", 'http.host eq "ha-centrale.exemple.fr"' in out and '"ha.exemple.fr"' not in out)
    t("webhook remplacé", '"/api/webhook/NOUVEAU987654"' in out and "ANCIEN" not in out)
    t("SKI, /auth/token et OAuth conservés", SKI in out and '"/auth/token"' in out and "/api/ha_mcp_tools/oauth/" in out)
    t("structure identique hors hôte/webhook",
      out.replace("ha-centrale.exemple.fr", "H").replace("NOUVEAU987654", "W") ==
      src.replace("ha.exemple.fr", "H").replace("ANCIEN123456", "W"))

    b.webhook_path = None
    out = b._clone_expr(src, "ha-x.exemple.fr")
    t("sans webhook connu → tous les webhooks (forme générique d'Accès distant)",
      'starts_with(http.request.uri.path, "/api/webhook/")' in out and "ANCIEN" not in out)

    src2 = '(http.host in {"ha.exemple.fr" "ha-maison.exemple.fr"} and ip.src in {160.79.104.0/21})'
    out = b._clone_expr(src2, "ha-c.exemple.fr")
    t("modèle « http.host in {...} » ramené à un seul hôte", out == '(http.host eq "ha-c.exemple.fr" and ip.src in {160.79.104.0/21})')
    t("hôte lu dans eq", b._host_of(src) == "ha.exemple.fr")
    t("hôte lu dans in", b._host_of(src2) == "ha.exemple.fr")

    src3 = ad_expr("ha.exemple.fr", None, token=False)
    out = b._clone_expr(src3, "ha-c.exemple.fr")
    t("variante sans exception /auth/token conservée telle quelle",
      'http.request.uri.path eq "/auth/token" or' in out and 'and not (http.request.uri.path eq "/auth/token")' not in out)

    b.webhook_path = "/api/webhook/NOUVEAU987654"
    m = b._merge_expr(src, "ha-c.exemple.fr")
    t("fusion : hôte ajouté à l'ensemble", 'http.host in {"ha.exemple.fr" "ha-c.exemple.fr"}' in m)
    t("fusion : webhook ajouté à côté de l'ancien",
      'http.request.uri.path in {"/api/webhook/ANCIEN123456" "/api/webhook/NOUVEAU987654"}' in m)
    t("fusion : /auth/token intact", 'and not (http.request.uri.path eq "/auth/token")' in m)
    t("fusion idempotente", b._merge_expr(m, "ha-c.exemple.fr") == m)
    m2 = b._merge_expr(m, "ha-d.exemple.fr")
    t("fusion d'un 3e hôte", 'http.host in {"ha.exemple.fr" "ha-c.exemple.fr" "ha-d.exemple.fr"}' in m2)

    t("identifiants valides", all(hb.Bootstrap.check_username(u) is None for u in ("richard", "julie", "dupont-bien1", "a.b_c")))
    t("identifiants refusés", all(hb.Bootstrap.check_username(u) for u in ("Richard", "a", "jean dupont", "-x", "é")))
    t("hôtes", all(hb.HOST_RE.match(h) for h in ("ha-centrale.degestmultiservices.cc", "a.b.fr"))
      and not any(hb.HOST_RE.match(h) for h in ("ha_centrale.fr", "https://x.fr", "x", "-a.fr")))
    t("slug", hb.slugify("Bien loué n°2 — Été") == "bien-loue-n-2-ete" and hb.slugify("!!!") == "ha")
    pw = [hb.gen_password() for _ in range(200)]
    t("mots de passe : 20 caractères, variés, sans ambiguïté",
      all(len(p) == 20 and not set(p) & set("0OolI1") for p in pw) and len(set(pw)) == 200)
    hb.LOG.hide("secret-tres-long")
    t("masquage des secrets", hb.LOG.clean("a secret-tres-long b") == "a ••• b")
    print(f"\n{ok}/{ok + fail} OK")
    return fail == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
