# ha-bootstrap

Met en place **le socle d'une instance Home Assistant OS en une fois**, depuis le shell Proxmox.
Il pose des questions simples à l'écran, s'occupe du reste, et te guide pour les rares gestes manuels.

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Basilik41/ha-bootstrap/main/install.sh)"
```

Il ne faut rien installer : le script utilise uniquement Python 3.9 ou plus récent (présent sur Proxmox 7, 8 et 9) et `whiptail`.
On peut le **relancer autant de fois qu'on veut** : chaque étape vérifie d'abord l'état réel, et ne refait que ce qui manque.

## Ce qu'il fait

| Étape | Détail |
|---|---|
| Choix de la VM | Il liste les VM Proxmox et lit l'adresse IP par l'agent QEMU. Il peut aussi créer une VM HAOS avec le script community-scripts. |
| Instantanés | Un instantané Proxmox est pris **avant** et **après** (sur proposition). C'est le point de retour. |
| Compte et écran d'accueil | Il crée ton compte sur une instance neuve, ou se connecte (double authentification gérée). Il termine aussi l'écran d'accueil. |
| Réglages généraux | Nom, France, euro, Europe/Paris, unités métriques, coordonnées GPS, adresse interne. |
| Mises à jour | Supervisor, puis Core, puis OS. Le script attend chaque redémarrage et vérifie la version. |
| Comptes | Famille (avec le même identifiant que sur les autres instances), compte `claude` (administrateur, utilisé par claude.ai), compte de service `ha-mcp` (administrateur, accès local seulement). Il crée aussi les personnes. |
| Boutique | Marketplace intégrée (HA 2026.11 et plus) : il accepte l'avertissement avec ton accord et relie GitHub seulement si nécessaire. Sur un HA plus ancien : HACS avec l'app Get HACS, et le code GitHub est guidé. |
| HA-MCP | Installé depuis le catalogue, serveur en mode « connexion Home Assistant », plus les outils fichiers et YAML. Le chemin du connecteur est retrouvé tout seul. |
| Accès distant | Intégration `Basilik41/hass-acces-distant` (dépôt privé, jeton GitHub en lecture seule). Il la pose par le File editor, en mode « réglages ». |
| Remote Home Assistant | Installé ; les liaisons sont faites ensuite par Claude, par le MCP. |
| Proxy inverse | Autorise `172.30.33.0/24` par l'API de HA 2026.11 (pas de YAML), puis confirme le réglage. |
| Cloudflare | Dans cet ordre : certificat client demandé sur le nouvel hôte, puis **règles WAF**, puis tunnel, DNS, app Cloudflared et adresse externe. Il finit par un contrôle : sans certificat, l'accès doit être refusé. |

### Règles WAF

Le script **ne modifie pas tes règles existantes**. Il clone « Ha- externe » (certificat obligatoire) et « HA-MCP - Claude » pour le nouvel hôte. Les nouvelles règles se terminent par `— ha-bootstrap`, sont placées juste après la règle modèle et suivent cette logique :

- l'hôte est remplacé ;
- le chemin du webhook HA-MCP de la nouvelle instance remplace celui de la Grange ;
- l'empreinte de l'autorité de certification, l'exception `/auth/token`, OAuth et la plage Anthropic restent identiques.

Une sauvegarde complète des règles est écrite avant toute modification dans `~/.config/ha-bootstrap/backups/`.
Si l'offre Cloudflare refuse une règle de plus, le script l'explique et n'ouvre pas le tunnel.
Le tunnel n'est jamais ouvert avant que la protection soit en place.

## Ce qui reste manuel (guidé à l'écran)

1. **Jeton Cloudflare**, une seule fois. Cloudflare → Mon profil → Jetons API → jeton personnalisé « ha-bootstrap », avec :
   - Compte · Cloudflare Tunnel · Modifier
   - Zone · Zone · Lire
   - Zone · DNS · Modifier
   - Zone · SSL et certificats · Modifier
   - Zone · WAF de zone · Modifier

   Ressources : ton compte et ta zone.
2. **Jeton GitHub**, une seule fois : le jeton *fine-grained* en lecture seule sur `hass-acces-distant`.
3. **Code GitHub** (rare) : seulement si GitHub limite les téléchargements anonymes de la Marketplace.
4. À la fin :
   - ajouter le connecteur dans claude.ai (l'URL exacte est affichée) ;
   - noter les mots de passe provisoires dans Vaultwarden (ils ne sont affichés qu'une fois) ;
   - activer la double authentification.

Les deux jetons peuvent être gardés dans `~/.config/ha-bootstrap/secrets.json`, lisible par root seulement, si tu le choisis.
Le journal et les rapports ne contiennent aucun secret : ils sont masqués.

## En cas de problème

- Chaque étape en échec propose **Réessayer / Passer / Arrêter**. Relancer le script reprend là où il s'était arrêté.
- Retour arrière complet : l'instantané Proxmox `hab_avant_…`.
- Règles WAF : sauvegarde JSON dans `~/.config/ha-bootstrap/backups/`. Les règles ajoutées se terminent par `— ha-bootstrap`.
- Journal détaillé : `~/.config/ha-bootstrap/logs/`.

## Tests

```bash
python3 tests/test_units.py                       # clonage des règles, validations
python3 tests/run_e2e.py <venv>/bin/hass <clone de hass-acces-distant>
```

Le banc de bout en bout fait tourner un **vrai Home Assistant 2026.11** et la **vraie intégration Accès distant**. Le reste est simulé et fidèle aux API vérifiées dans leur code source :

- Supervisor, apps et ingress du File editor ;
- Marketplace (le catalogue n'est pas joignable depuis le banc) ;
- HA-MCP ;
- Cloudflare, avec tes règles réelles reproduites ;
- GitHub et `qm`.

Scénarios couverts :

- instance neuve complète ;
- relance sans aucun changement ;
- mauvais mot de passe ;
- jeton Cloudflare sans permission ;
- sous-domaine déjà pris ;
- limite de règles atteinte ;
- absence de règle modèle ;
- mises à jour Supervisor, Core et OS ;
- instantané impossible.

Interpréteurs testés : Python 3.9 et 3.13.
