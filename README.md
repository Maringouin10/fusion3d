# Fusion3D VR

Voir et manipuler ses modèles **Fusion 360** dans un casque **Meta Quest 3 / 3S**, en direct.

```
┌──────────────┐  corps maillés   ┌─────────────────────┐  HTTPS + WebXR   ┌──────────────┐
│  Fusion 360  │ ───────────────► │  Fusion3D VR (.app) │ ───────────────► │ Meta Quest 3 │
│  complément  │  127.0.0.1:8765  │  pont sur le Mac    │  <ip-mac>:8443   │  navigateur  │
└──────────────┘                  └─────────────────────┘                  └──────────────┘
```

1. **Complément Fusion** (`addin/Fusion3DVR`) : au clic droit sur un ou plusieurs corps, un
   dossier de corps ou un composant (dans l'arbre ou la vue 3D) :
   - **Ajouter à la VR** : ajoute la sélection à ce qui est déjà dans le casque ;
   - **Voir seul en VR** : le casque montre uniquement la sélection ;
   - **Retirer de la VR**.

   Les mêmes boutons sont dans le panneau **CASQUE VR** de l'onglet SOLIDE, avec « Tout envoyer »,
   « Vider le casque », « Connexion du casque… » et « Mettre à jour maintenant ».
   Les corps marqués sont renvoyés automatiquement après chaque modification : synchro en direct.
2. **App Mac « Fusion3D VR »** (`bridge/`) : distribuée en `.dmg`. Icône dans la barre des menus,
   installe le complément dans Fusion, et sert le site WebXR au casque en HTTPS (certificat local
   généré automatiquement).
3. **Site WebXR** (`bridge/web/`, three.js) : ouvert dans le navigateur du Quest. Modes **VR** et
   **réalité mixte (passthrough)**.

## Utilisation

1. Ouvrir le `.dmg`, glisser **Fusion3D VR** dans Applications, puis *clic droit › Ouvrir* la
   première fois (app non signée par Apple).
2. Dans Fusion : **Utilitaires › Compléments** (`Maj+S`) › **Fusion3DVR** › **Exécuter**
   (une seule fois ; ensuite il démarre avec Fusion).
3. Clic droit sur **Body1** dans l'arbre › **Ajouter à la VR** (ou **Voir seul en VR**).
   Cmd+clic pour sélectionner plusieurs corps ; clic droit sur un dossier ou un composant pour tout son contenu.
4. Dans le casque (même Wi-Fi que le Mac) : navigateur › adresse affichée par l'app
   (ex. `https://192.168.1.23:8443`) › *Paramètres avancés › Continuer* › **Entrer en VR**.

### Commandes dans le casque

| Action | Manette | Mains nues |
|---|---|---|
| Saisir / déplacer **une seule pièce** | gâchette ou poignée, sur la pièce ou près d'elle | pincer sur ou près de la pièce |
| Agrandir / réduire cette pièce | deuxième main sur la pièce tenue, écarter les mains | idem en pinçant |
| Tourner / zoomer tout le modèle | joystick droit ← → / ↑ ↓, ou panneau de la paume | panneau de la paume |

On ne saisit jamais tout le modèle d'un coup : la main prend la pièce visée, ou à défaut la plus
proche (à moins de 25 cm). S'il n'y a rien à portée, rien ne bouge.
| Réinitialiser | A (ou Y) | — |
| Taille réelle ↔ taille ajustée | B (ou X) | — |
| Panneau de commande | flotte au-dessus de la manette gauche : viser + gâchette | tourner la **paume gauche** vers soi, toucher avec l'index droit |

Le panneau de commande affiche le nom du document, les **dimensions réelles en mm** et l'échelle
(par ex. « taille réelle (1:1) » ou « agrandi ×2 »). Ses boutons : Réinitialiser, Taille réelle/ajustée,
Tourner 90°, Zoom +, Zoom −, Quitter la VR.

Le modèle s'affiche **en taille réelle** par défaut. Fusion envoie toujours en cm, quelle que soit
l'unité du document (mm, po…), donc les dimensions sont justes. Seuls les modèles de plus de 2,5 m
démarrent en « taille ajustée » (40 cm).

## Mettre à jour maintenant

Le bouton **Mettre à jour maintenant** télécharge le dernier code du dépôt GitHub
(branche par défaut) et l'installe sans réinstaller le `.dmg`. On le trouve à trois endroits :
- dans Fusion : panneau **CASQUE VR** › **Mettre à jour maintenant**. Le complément se recharge
  tout seul, sans redémarrer Fusion ;
- dans la barre des menus : icône casque › **Mettre à jour maintenant** ;
- sur la page d'état (`http://127.0.0.1:8765/status`).

L'app, le site du casque et le complément Fusion sont mis à jour ensemble. L'app redémarre si son
propre code a changé ; dans le casque, il suffit de recharger la page. Une version GitHub qui
contient une erreur de syntaxe, ou qui est plus ancienne que l'app, est refusée. Les fichiers
téléchargés sont rangés dans `~/Library/Application Support/Fusion3D VR/current/`. Les nouvelles
bibliothèques Python ne peuvent pas arriver par ce chemin : il faut alors un nouveau `.dmg`.

## À propos du bouton « à côté de l'œil »

L'API de Fusion 360 ne permet **pas** d'ajouter une icône directement dans les lignes de l'arbre
(à côté de l'œil de visibilité) : cette zone n'est pas personnalisable par les compléments.
L'équivalent le plus proche, utilisé ici, est le **clic droit sur le corps dans l'arbre**
(« Ajouter à la VR », « Voir seul en VR », « Retirer de la VR »), plus le panneau CASQUE VR.

## Construire le .dmg

Le `.dmg` se construit sur un Mac (ou automatiquement par GitHub Actions, voir
`.github/workflows/build-dmg.yml`, qui publie un `.dmg` Apple Silicon et un Intel en artefacts,
et en release pour un tag `v*`).

```bash
./packaging/build_dmg.sh
```

Résultat : `dist/Fusion3D-VR-arm64.dmg` (ou `x86_64`).

## Développement

```bash
pip install -r bridge/requirements.txt
python3 bridge/fusion3d_bridge.py            # barre des menus (macOS)
python3 bridge/fusion3d_bridge.py --headless # sans interface
python3 tests/test_bridge.py                 # tests du pont
python3 tests/test_update.py                 # tests de « Mettre à jour maintenant »
```

Pour tester le complément sans l'app : copier `addin/Fusion3DVR` dans
`~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`.

## Limites connues

- Le certificat HTTPS est auto-signé : le Quest affiche un avertissement la première fois.
- Les couleurs viennent de l'apparence Fusion (couleur de base seulement, pas de textures).
- Les pièces déplacées à la main dans le casque gardent leur position tant que le document
  ne change pas (bouton A pour revenir à la position Fusion).
- Fusion installé depuis le Mac App Store utilise un autre dossier de compléments : utilisez
  alors l'installation manuelle ci-dessus.
