# Installer Wildwatch — Design

Date : 2026-05-04

## Contexte

Aujourd'hui le déploiement complet sur un RPi neuf demande sept à huit étapes
manuelles (rsync, SSH, exécution de scripts, génération de clé API, édition de
config.toml, redémarrage de service). Documenté dans `docs/SETUP-RPI.md` mais
fastidieux à reproduire et facile à rater. On veut un script orchestrateur unique
qui pilote tout depuis le PC de l'opérateur.

## Objectif

Un seul fichier `_recovery/install_wildwatch.py` lancé depuis la racine du repo
sur Mac ou Linux, qui découvre la cible, sait s'adapter à un RPi neuf ou déjà
configuré, gère la clé d'API, déploie le code, configure le service systemd,
et confirme que tout tourne.

## Stack

| Outil        | Rôle                                                          |
|--------------|---------------------------------------------------------------|
| Python 3.11+ | Langage. Inline metadata PEP 723 → pas de pyproject à part.   |
| `uv run`     | Crée un venv éphémère avec les deps déclarées en tête.        |
| `rich`       | Panels, status, log timestampé, syntax highlight des commandes.|
| `questionary`| Prompts interactifs : sélection liste, confirm, text input.   |
| `subprocess` | SSH et rsync — outils déjà configurés et disponibles.         |

PEP 723 inline metadata permet `uv run install_wildwatch.py` sans setup. Pas
besoin de venv dédié, pas de pyproject à maintenir.

## Pré-requis utilisateur

- `uv` installé sur le PC.
- `rsync` installé sur le PC (déjà requis pour les déploiements actuels).
- Clé SSH déjà configurée pour `dietpi@<host>` (le script vérifie en mode
  `BatchMode=yes` et affiche une erreur claire avec la commande `ssh-copy-id` si KO).
- Le RPi doit avoir DietPi installé, le WiFi configuré et être joignable.
- Le script doit être lancé depuis la racine du repo (sinon il échoue tôt).

## Flux

1. **Découverte cible**
   - Test de `dietpi.local` via ping. Si répond, demande confirmation.
   - Sinon, scan ARP filtré par OUI Raspberry Pi (`b8:27:eb`, `dc:a6:32`,
     `e4:5f:01`, `2c:cf:67`). Préalablement, ping broadcast pour peupler ARP.
   - Sinon, saisie manuelle de l'IP ou du hostname.

2. **Vérification SSH**
   - `ssh -o BatchMode=yes -o ConnectTimeout=5 dietpi@<target> true`.
   - Si échec, message d'erreur avec `ssh-copy-id dietpi@<target>` à lancer.

3. **Inspection du RPi**
   - Une seule connexion SSH groupée qui collecte : présence de
     `~/wildwatch/config.toml`, état actuel du service, valeurs de `server_url`
     et `api_key` si elles existent.
   - Détermine le scénario : « réinstall » (config existe) vs « neuf ».

4. **Décision serveur**
   - Cas réinstall : on garde l'URL et la clé API existantes.
   - Cas neuf : prompt « Le serveur est-il déjà déployé ailleurs ? »
     - Oui : demande URL et clé API à saisir.
     - Non : génère une nouvelle clé via `secrets.token_urlsafe(32)`,
       sauvegarde dans `_recovery/api_key.secret` (chmod 600, gitignored
       par `*.secret`), et propose de configurer l'URL serveur sur l'IP
       locale du PC + port 8000. À la fin, le script affiche la commande
       exacte `WILDWATCH_API_KEY=… uv run uvicorn …` à lancer dans un autre
       terminal pour démarrer le serveur.

5. **Setup système**
   - Lance `_recovery/setup_rpi.sh` via SSH (apt + groupes + uv + avahi +
     blacklists + gpu_mem). Le script existant est idempotent.
   - Si `gpu_mem_1024` a été modifié (détecté par grep avant/après), reboot et
     attente du retour de la machine.

6. **Déploiement code**
   - `rsync` du repo vers `~/wildwatch-src/` avec les exclusions habituelles
     (`.venv`, `__pycache__`, `data/`, `_recovery/sd_backup/`, `.git/`).

7. **Venv et dépendances**
   - `uv venv --system-site-packages --python /usr/bin/python3` puis
     `uv sync --no-dev --active` côté RPi.

8. **Configuration runtime**
   - Génère le `~/wildwatch/config.toml` côté RPi via heredoc (template
     embarqué dans le Python). Les valeurs préservées (URL, clé API) sont
     substituées.

9. **Service systemd**
   - Lance `_recovery/install_systemd.sh` via SSH. Idempotent. Le service est
     restart pour reprendre la nouvelle config.

10. **Vérifications finales**
    - `systemctl is-active wildwatch-capture` → doit être `active`.
    - Affiche les cinq dernières lignes du journal pour confirmer la boucle de
      surveillance démarrée.
    - Affiche un récap final : commande pour suivre les logs, commande pour
      lancer le serveur si setup local.

## UX

- `rich.console.Console` pour le rendu, avec timestamp pour chaque étape via
  `console.log()`.
- `Status` (spinner) pendant les commandes longues (apt install, reboot wait).
- `Panel` pour le titre du script en haut, et pour le récap final en bas.
- Couleurs minimales : vert pour succès, rouge pour erreurs, bleu pour les
  étapes en cours, jaune pour les warnings.
- `questionary.select` pour les choix de liste (flèches), `questionary.confirm`
  pour les Y/N, `questionary.text` pour les saisies libres avec validation.
- Si SIGINT, message clair « Annulé par l'utilisateur » et exit propre.

## Idempotence et erreurs

- Une étape qui échoue interrompt le script avec un code de sortie non nul et
  un message exposant la commande à relancer manuellement pour reprendre.
- Les opérations sont conçues pour être rejouables sans casser l'état :
  rsync `--delete` mais hors `~/wildwatch/` runtime, `uv sync` réutilise le
  venv existant, le heredoc config écrase le fichier mais utilise les valeurs
  préservées par l'inspection initiale.
- Le script ne crée jamais de fichier hors du repo et de `~/wildwatch*` côté
  RPi, donc l'utilisateur peut tout supprimer manuellement en cas de pépin.
