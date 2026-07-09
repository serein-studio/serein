#!/usr/bin/env python3
# scripts/generate_list.py
#
# Génère demarchage_france.json en assemblant :
#   - priority : numéros signalés (via l'endpoint Apps Script), vérifiés
#   - racines  : tranches MAJNUM des racines de démarchage
#
# Filtrage des signalements (couche priority) :
#   1. Le numéro appartient à une des 12 racines de démarchage attribuées
#   2. Pas de doublon (liste unique)
#   3. Signalé il y a moins d'un an
#
# Usage : python generate_list.py <URL_SIGNALEMENTS> <sortie.json>

import sys
import os
import csv
import io
import json
import hashlib
import urllib.request
from datetime import datetime, timezone, timedelta

# Les 12 racines de démarchage ARCEP (métropole)
DEMARCHAGE_PREFIXES = [
    "0162", "0163", "0270", "0271", "0377", "0378",
    "0424", "0425", "0568", "0569", "0948", "0949",
]

MAJNUM_URL = "https://extranet.arcep.fr/uploads/MAJNUM.csv"


def download_majnum() -> str:
    """Télécharge MAJNUM et le renvoie en texte décodé (Windows-1252)."""
    req = urllib.request.Request(MAJNUM_URL, headers={"User-Agent": "Serein-Bot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    # Le fichier ARCEP est en ANSI / Windows-1252
    return raw.decode("cp1252", errors="replace")


def parse_int_safe(value: str):
    """Parse un entier même en notation scientifique (ex: '1.62E+10')."""
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            # Gère la notation scientifique apparue dans certaines MàJ ARCEP
            return int(float(value))
        except ValueError:
            return None


def to_e164_from_national(raw: str):
    """
    Convertit un Tranche_Debut/Fin MAJNUM en E.164 Int64, robuste au format.
    MAJNUM donne un numéro national de 10 chiffres commençant par 0
    (ex: '0162000000'). Selon la source, le 0 initial peut manquer
    ('162000000') ou le nombre être en notation scientifique.
    Résultat : 33 + les 9 chiffres après le 0 initial.
    """
    val = parse_int_safe(raw)
    if val is None:
        return None
    # Rembourre à 10 chiffres pour reconstituer le 0 initial perdu par int()
    national = str(val).zfill(10)
    if len(national) != 10 or not national.startswith("0"):
        return None
    return int("33" + national[1:])


def build_racines(majnum_text: str) -> dict:
    """Construit le dictionnaire racine -> liste de tranches E.164."""
    racines = {p: [] for p in DEMARCHAGE_PREFIXES}
    reader = csv.DictReader(io.StringIO(majnum_text), delimiter=";")

    for row in reader:
        debut_raw = (row.get("Tranche_Debut") or "").strip()
        fin_raw = (row.get("Tranche_Fin") or "").strip()
        if not debut_raw:
            continue

        # On normalise d'abord le début en national à 10 chiffres pour
        # tester le préfixe de façon fiable (avec ou sans 0 de tête).
        val = parse_int_safe(debut_raw)
        if val is None:
            continue
        debut_national = str(val).zfill(10)

        for prefix in DEMARCHAGE_PREFIXES:
            if debut_national.startswith(prefix):
                debut_e164 = to_e164_from_national(debut_raw)
                fin_e164 = to_e164_from_national(fin_raw)
                if debut_e164 is None or fin_e164 is None:
                    break
                racines[prefix].append([debut_e164, fin_e164])
                break

    # Retire les racines vides, trie les tranches
    result = {}
    for prefix, tranches in racines.items():
        if tranches:
            tranches.sort()
            result[prefix] = tranches
    return result

    # Retire les racines vides, trie les tranches
    result = {}
    for prefix, tranches in racines.items():
        if tranches:
            tranches.sort()
            result[prefix] = tranches
    return result


def national_to_e164(national: str):
    """
    Convertit un numéro national en E.164.
    Gère les cas : 0759254806, 759254806 (0 perdu par le Sheet), 33759254806.
    Renvoie None si invalide.
    """
    digits = "".join(c for c in national if c.isdigit())
    if not digits:
        return None
    # Déjà en E.164 (33...)
    if digits.startswith("33") and len(digits) >= 11:
        return int(digits)
    # Format national avec 0 initial
    if digits.startswith("0"):
        return int("33" + digits[1:])
    # Le 0 initial a été perdu (ex: Sheet qui traite le numéro comme un nombre)
    # Un numéro français national fait 9 chiffres après le 0 (ex: 759254806)
    if len(digits) == 9:
        return int("33" + digits)
    return None


def fetch_signalements(url: str) -> list:
    """Récupère les signalements depuis l'endpoint Apps Script (JSON)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Serein-Bot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("signalements", [])


MIN_DISTINCT_REPORTS = 3   # nb de personnes distinctes requises hors préfixes connus


def build_priority(signalements: list, racines: dict) -> list:
    """
    Détermine les numéros de la couche priority.

    Règles :
      - CAS TEST : "Vérifié" == "TEST" → toujours retenu (préfixe/date ignorés).
      - CAS PRÉFIXE CONNU : numéro dont le préfixe est une des 12 racines de
        démarchage → 1 signalement suffit (le préfixe est déjà un garde-fou),
        sous réserve d'être signalé il y a moins d'un an.
      - CAS HORS PRÉFIXE : numéro hors des 12 racines → retenu seulement s'il a
        été signalé par au moins MIN_DISTINCT_REPORTS personnes DISTINCTES
        (UUID anonymes différents), toutes de moins d'un an.

    Résultat dédupliqué, trié.
    """
    one_year_ago = datetime.now(timezone.utc) - timedelta(days=365)

    def parse_date(s: str):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
        except (ValueError, AttributeError):
            return None

    def prefix_of(national: str) -> str:
        p = national[:4]
        if p and not p.startswith("0") and len(p) == 3:
            p = "0" + p
        return p

    # 1) On agrège par numéro E.164 : liste des UUID distincts récents + flags
    #    { e164: {"uuids": set(), "known": bool, "test": bool} }
    agg = {}

    for sig in signalements:
        national = str(sig.get("numero", "")).strip()
        prefixe = str(sig.get("prefixe", "")).strip()
        date_str = str(sig.get("date", "")).strip()
        verifie = str(sig.get("verifie", "")).strip().upper()
        uuid = str(sig.get("uuid", "")).strip()

        # Normalise le préfixe (0 initial éventuellement perdu par le Sheet)
        if prefixe and not prefixe.startswith("0") and len(prefixe) == 3:
            prefixe = "0" + prefixe

        e164 = national_to_e164(national)
        if e164 is None:
            continue

        entry = agg.setdefault(e164, {"uuids": set(), "known": False, "test": False})

        # Cas TEST : marque et continue (pas de contrainte)
        if verifie == "TEST":
            entry["test"] = True
            continue

        # Date : ignore les signalements de plus d'un an
        d = parse_date(date_str)
        if d is None or d < one_year_ago:
            continue

        # Préfixe connu ?
        is_known = (prefixe in DEMARCHAGE_PREFIXES) or \
                   any(national.startswith(p) for p in DEMARCHAGE_PREFIXES) or \
                   (prefix_of(national) in DEMARCHAGE_PREFIXES)
        if is_known:
            entry["known"] = True

        # Compte l'UUID (une chaîne vide compte comme un signalement anonyme
        # unique fallback, pour ne pas perdre les anciens signalements sans uuid)
        entry["uuids"].add(uuid if uuid else f"legacy-{len(entry['uuids'])}")

    # 2) On applique les règles
    priority = []
    for e164, info in agg.items():
        if info["test"]:
            priority.append(e164)                      # TEST : toujours
        elif info["known"]:
            priority.append(e164)                      # préfixe connu : 1 suffit
        elif len(info["uuids"]) >= MIN_DISTINCT_REPORTS:
            priority.append(e164)                      # hors préfixe : ≥ N distincts

    priority.sort()
    return priority


def content_hash(priority: list, racines: dict) -> str:
    """Empreinte stable du contenu (indépendante de la date)."""
    payload = json.dumps(
        {"priority": priority, "racines": racines},
        sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def read_existing(path: str):
    """Lit le fichier existant s'il y en a un. Renvoie (hash, version) ou (None, None)."""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("hash"), data.get("version")
    except (json.JSONDecodeError, OSError):
        return None, None


def main():
    if len(sys.argv) < 3:
        print("Usage: generate_list.py <URL_SIGNALEMENTS> <sortie.json>")
        sys.exit(1)

    signalements_url = sys.argv[1]
    output_path = sys.argv[2]

    print("Téléchargement de MAJNUM...")
    majnum_text = download_majnum()
    racines = build_racines(majnum_text)
    total_majnum = sum(
        sum(f - d + 1 for d, f in tranches) for tranches in racines.values()
    )
    print(f"  {len(racines)} racines actives, {total_majnum:,} numéros MAJNUM")

    print("Récupération des signalements...")
    try:
        signalements = fetch_signalements(signalements_url)
        print(f"  {len(signalements)} signalements bruts")
    except Exception as e:
        print(f"  Erreur signalements ({e}), on continue sans priority")
        signalements = []

    priority = build_priority(signalements, racines)
    print(f"  {len(priority)} numéros prioritaires après filtrage")

    # Empreinte du contenu réel
    new_hash = content_hash(priority, racines)
    old_hash, old_version = read_existing(output_path)

    if old_hash == new_hash:
        # Les données n'ont pas changé : on ne touche à rien.
        print(f"\nContenu identique (hash {new_hash}). Aucune mise à jour.")
        print(f"  version conservée : {old_version}")
        return

    # Les données ont changé : nouvelle date de version
    new_version = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"\nContenu modifié (ancien hash {old_hash}, nouveau {new_hash})")

    payload = {
        "version": new_version,   # date lisible, affichée à l'utilisateur
        "hash": new_hash,         # empreinte, pour la détection de changement
        "priority": priority,
        "racines": racines,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    size = os.path.getsize(output_path)
    print(f"Fichier écrit : {output_path} ({size:,} octets)")
    print(f"  version  : {new_version}")
    print(f"  hash     : {new_hash}")
    print(f"  priority : {len(priority)}")
    print(f"  racines  : {list(racines.keys())}")


if __name__ == "__main__":
    main()
