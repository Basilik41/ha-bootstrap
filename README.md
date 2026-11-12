# ha-bootstrap

Met en place **le socle d'une instance Home Assistant OS en une fois**, depuis le shell Proxmox.
Il pose des questions simples à l'écran, s'occupe du reste, et te guide pour les rares gestes manuels.
Rien n'est propre à une maison : noms, comptes, domaine et options se choisissent à l'écran. Tu peux donc le donner à quelqu'un d'autre.

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Basilik41/ha-bootstrap/main/install.sh)"
```

À lancer en root dans le shell Proxmox.

Il ne faut rien installer : le script utilise uniquement Python 3.9 ou plus récent (présent sur Proxmox 7, 8 et 9), `whiptail` et `openssl`.
On peut le **relancer autant de fois qu'on veut** : chaque étape vérifie d'abord l'état réel, et ne refait que ce qui manque.
Les choix (étapes cochées, personnes créées, domaine) sont retenus **sur ce serveur Proxmox seulement**, dans `~/.config/ha-bootstrap/`. Ils sont reproposés à la prochaine instance.

## Les profils

| Profil | Pour |
|---|---|
| Maison principale | La famille et les appareils de la maison. |
| Autre bâtiment | Une résidence secondaire, un atelier… |
| Logement loué | Le compte du locataire, avec un identifiant unique obligatoire. Les comptes de la famille ne sont pas repris. |
| Centrale | Une instance qui regarde plusieurs autres instances (Remote Home Assistant). |
| Personnalisé | Tu coches toi-même. |

Le profil ne fait que pré-cocher des étapes. Tout reste modifiable dans la liste.

## Ce qu'il fait

| Étape | Détail |
|---|---|
| Choix de la VM | Il liste les VM Proxmox et lit l'adresse IP par l'agent QEMU. Il peut aussi créer une VM HAOS avec le script community-scripts. |
| Instantanés | Un instantané Proxmox est pris **avant** et **après** (sur proposition). C'est le point de retour. |
| Compte et écran d'accueil | Il crée le compte du propriétaire sur une instance neuve, ou se connecte (double authentification gérée). Il termine aussi l'écran d'accueil. |
| Réglages généraux | Nom, France, euro, Europe/Paris, unités métriques, coordonnées GPS, adresse interne. |
| Mises à jour | Supervisor, puis Core, puis OS. Le script attend chaque redémarrage et vérifie la version. |
| Comptes | Saisis à l'écran : nom, identifiant, administrateur ou non, accès depuis l'extérieur ou non. Il propose de reprendre les personnes créées pour une instance précédente, et crée aussi les personnes. |
| Boutique | Marketplace intégrée (HA 2026.11 et plus) : il accepte l'avertissement avec ton accord et relie GitHub seulement si nécessaire. Sur un HA plus ancien : HACS avec l'app Get HACS, et le code GitHub est guidé. |
| HA-MCP *(option)* | Laisse Claude (claude.ai) lire et régler l'instance : compte claude.ai (identifiant au choix), compte de service `ha-mcp` (administrateur, accès local seulement), serveur en « connexion Home Assistant », outils fichiers et YAML. L'URL du connecteur est affichée. |
| Remote Home Assistant *(option)* | Installé pour qu'une instance voie les entités d'autres instances. |
| Accès distant *(option)* | Intégration privée `Basilik41/hass-acces-distant`, uniquement pour qui a un jeton d'accès. Il la pose par le File editor, en mode « réglages ». |
| Proxy inverse | Autorise `172.30.33.0/24` par l'API de HA 2026.11 (pas de YAML), puis confirme le réglage. |
| Cloudflare *(option)* | Choix de la protection, puis, dans cet ordre : certificat client demandé, règles WAF, tunnel, DNS, app Cloudflared et adresse externe. Il finit par un contrôle depuis l'extérieur. |

### Protection de l'accès extérieur

- **Certificat obligatoire (recommandé)** : seuls les appareils munis d'un certificat passent.
  - Les certificats sont créés par le script : un fichier `.p12` par appareil, avec un mot de passe affiché une seule fois. Ils sont signés par l'autorité de certificats gérée par Cloudflare, et les fichiers sont rangés dans `~/.config/ha-bootstrap/certificats/<hôte>/`.
  - Exceptions : `/auth/token`, dont l'app Android a besoin pour renouveler sa session, et, si HA-MCP est coché, la plage d'adresses d'Anthropic, limitée au webhook et à OAuth.
- **Mot de passe Home Assistant seulement** : plus simple, moins sûr. Aucune règle de certificat.

### Règles WAF

Le script **ne modifie jamais les règles existantes**. Les règles qu'il ajoute se terminent par `— ha-bootstrap`.

- **Zone qui a déjà une règle « certificat obligatoire »**, par exemple créée par Accès distant : il la recopie pour le nouvel hôte, ainsi que la règle Claude. Seuls l'hôte et le chemin du webhook changent. Les copies sont placées juste après la règle d'origine.
- **Zone sans règle** : il crée la règle complète (certificat vérifié et non révoqué) en tête de liste.
- **Offre sans Super Bot Fight Mode** : la règle Claude saute seulement les règles personnalisées.

Une sauvegarde complète des règles est écrite avant toute modification dans `~/.config/ha-bootstrap/backups/`.
Si l'offre Cloudflare refuse une règle de plus, le script l'explique et **n'ouvre pas le tunnel**.
Le tunnel n'est jamais ouvert avant que la protection soit en place.

## Ce qui reste manuel (guidé à l'écran)

1. **Jeton Cloudflare**, une seule fois par serveur. Cloudflare → Mon profil → Jetons API → jeton personnalisé « ha-bootstrap », avec :
   - Compte · Cloudflare Tunnel · Modifier
   - Zone · Zone · Lire
   - Zone · DNS · Modifier
   - Zone · SSL et certificats · Modifier
   - Zone · WAF de zone · Modifier

   Ressources : ton compte et ta zone.
2. **Code GitHub** (rare) : seulement si GitHub limite les téléchargements anonymes de la Marketplace.
3. À la fin :
   - noter les mots de passe provisoires (comptes, certificats) : ils ne sont affichés qu'une fois ;
   - copier chaque `.p12` sur son appareil ;
   - si HA-MCP est coché, ajouter le connecteur dans claude.ai (l'URL exacte est affichée) ;
   - activer la double authentification.

Les jetons peuvent être gardés dans `~/.config/ha-bootstrap/secrets.json`, lisible par root seulement, si tu le choisis.
Le journal et les rapports ne contiennent aucun secret : ils sont masqués.

## En cas de problème

- Chaque étape en échec propose **Réessayer / Passer / Arrêter**. Relancer le script reprend là où il s'était arrêté.
- Retour arrière complet : l'instantané Proxmox `hab_avant_…`.
- Règles WAF : sauvegarde JSON dans `~/.config/ha-bootstrap/backups/`. Les règles ajoutées se terminent par `— ha-bootstrap`.
- Journal détaillé : `~/.config/ha-bootstrap/logs/`.

## Tests

```bash
python3 tests/test_units.py                       # clonage et génération des règles, validations
python3 tests/run_e2e.py <venv>/bin/hass <clone de hass-acces-distant>
```

Le banc de bout en bout fait tourner un **vrai Home Assistant 2026.11** et la **vraie intégration Accès distant**. Le reste est simulé et fidèle aux API vérifiées dans leur code source :

- Supervisor, apps et ingress du File editor ;
- Marketplace ;
- HA-MCP ;
- Cloudflare (règles, certificats clients signés par une vraie autorité de test) ;
- GitHub et `qm`.

Scénarios couverts, sur Python 3.9 et 3.13 :

- instance neuve complète ;
- relance sans aucun changement ;
- mauvais mot de passe ;
- double authentification ;
- jeton Cloudflare sans permission ;
- sous-domaine déjà pris ;
- limite de règles atteinte ;
- zone sans aucune règle, avec certificats d'appareils ;
- accès par mot de passe seulement ;
- reprise des personnes d'une instance précédente ;
- compte de locataire ;
- mises à jour Supervisor, Core et OS ;
- instantané impossible.
