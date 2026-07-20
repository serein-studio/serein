#!/usr/bin/env python3
# scripts/generate_list.py
#
# Génère demarchage_france.json en assemblant :
#   - priority : numéros signalés (via l'endpoint Apps Script), vérifiés
#   - racines  : TOUTES les tranches MAJNUM des 12 racines de démarchage
#
# ─── POURQUOI CE SCRIPT NE SÉLECTIONNE PLUS ─────────────────────────────────
# Historique : l'extension CallKit plafonnait à ~1,99 M entrées par chargement.
# Le script devait donc TRONQUER (quota par racine, scoring, garanties...) pour
# tenir sous ce plafond.
#
# Depuis le chargement INCRÉMENTAL par lots (feature/incremental-callkit-load),
# l'app charge la liste en plusieurs cycles et n'est plus limitée à 2 M. Le
# script publie donc l'INTÉGRALITÉ des 12 racines (~2,18 M numéros), sans aucune
# sélection. Toute la logique quota/score/garanties/blacklist a été retirée —
# elle reste dans l'historique git si jamais on en a besoin.
#
# ⚠️ CONSÉQUENCE POUR L'APP 1.0 EN PRODUCTION (sans chargement incrémental) :
# elle re-tronquera elle-même à 1,99 M par ordre croissant → l'angle mort 0948
# revient temporairement pour les utilisateurs 1.0, JUSQU'À ce que le build
# incrémental soit publié. Choix assumé le temps du développement.
#
# ⚠️ CE SCRIPT PUBLIE DIRECTEMENT À TOUS LES UTILISATEURS, SANS REVIEW APPLE.
# D'où le garde-fou de plausibilité (MIN_PLAUSIBLE_COVERAGE).
#
# Usage : python generate_list.py <URL_SIGNALEMENTS> <sortie.json>

import sys
import os
import csv
import io
import json
import hashlib
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import NamedTuple

DEMARCHAGE_PREFIXES = [
    "0162", "0163", "0270", "0271", "0377", "0378",
    "0424", "0425", "0568", "0569", "0948", "0949",
]

MAJNUM_URL = "https://extranet.arcep.fr/uploads/MAJNUM.csv"

# Garde-fou : en dessous, on refuse de publier (protège contre un parsing raté
# qui viderait la liste et désactiverait la protection de tous).
MIN_PLAUSIBLE_COVERAGE = 1_500_000

# Numéros hors préfixes connus : nb d'UUID distincts requis pour entrer en priority.
MIN_DISTINCT_REPORTS = 3


class Tranche(NamedTuple):
    start: int
    end: int
    racine: str
    operateur: str

    @property
    def size(self) -> int:
        return self.end - self.start + 1


# ── MAJNUM ──────────────────────────────────────────────────────────────────

def download_majnum() -> str:
    req = urllib.request.Request(MAJNUM_URL, headers={"User-Agent": "Serein-Bot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return raw.decode("cp1252", errors="replace")


def parse_int_safe(value: str):
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return None


def to_e164_from_national(raw: str):
    val = parse_int_safe(raw)
    if val is None:
        return None
    national = str(val).zfill(10)
    if len(national) != 10 or not national.startswith("0"):
        return None
    return int("33" + national[1:])


def build_tranches(majnum_text: str) -> list:
    """Toutes les tranches des 12 racines, triées croissantes. Opérateur conservé."""
    tranches = []
    reader = csv.DictReader(io.StringIO(majnum_text), delimiter=";")
    for row in reader:
        debut_raw = (row.get("Tranche_Debut") or "").strip()
        fin_raw = (row.get("Tranche_Fin") or "").strip()
        if not debut_raw:
            continue
        val = parse_int_safe(debut_raw)
        if val is None:
            continue
        racine = str(val).zfill(10)[:4]
        if racine not in DEMARCHAGE_PREFIXES:
            continue
        debut_e164 = to_e164_from_national(debut_raw)
        fin_e164 = to_e164_from_national(fin_raw)
        if debut_e164 is None or fin_e164 is None or fin_e164 < debut_e164:
            continue
        operateur = (row.get("Mnémo") or "").strip() or "?"
        tranches.append(Tranche(debut_e164, fin_e164, racine, operateur))
    tranches.sort()
    return tranches


# ── Signalements → priority ─────────────────────────────────────────────────

def national_to_e164(national: str):
    digits = "".join(c for c in national if c.isdigit())
    if not digits:
        return None
    if digits.startswith("33") and len(digits) >= 11:
        return int(digits)
    if digits.startswith("0"):
        return int("33" + digits[1:])
    if len(digits) == 9:
        return int("33" + digits)
    return None


def fetch_signalements(url: str) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "Serein-Bot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("signalements", [])


def parse_date(s: str):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
    except (ValueError, AttributeError):
        return None


def prefix_of(national: str) -> str:
    p = national[:4]
    if p and not p.startswith("0") and len(p) == 3:
        p = "0" + p
    return p


def build_priority(signalements: list) -> list:
    one_year_ago = datetime.now(timezone.utc) - timedelta(days=365)
    agg = {}
    for sig in signalements:
        national = str(sig.get("numero", "")).strip()
        prefixe = str(sig.get("prefixe", "")).strip()
        verifie = str(sig.get("verifie", "")).strip().upper()
        uuid = str(sig.get("uuid", "")).strip()
        if prefixe and not prefixe.startswith("0") and len(prefixe) == 3:
            prefixe = "0" + prefixe
        e164 = national_to_e164(national)
        if e164 is None:
            continue
        entry = agg.setdefault(e164, {"uuids": set(), "known": False, "test": False})
        if verifie == "TEST":
            entry["test"] = True
            continue
        d = parse_date(sig.get("date", ""))
        if d is None or d < one_year_ago:
            continue
        is_known = (prefixe in DEMARCHAGE_PREFIXES) or \
                   any(national.startswith(p) for p in DEMARCHAGE_PREFIXES) or \
                   (prefix_of(national) in DEMARCHAGE_PREFIXES)
        if is_known:
            entry["known"] = True
        entry["uuids"].add(uuid if uuid else f"legacy-{len(entry['uuids'])}")
    priority = []
    for e164, info in agg.items():
        if info["test"] or info["known"]:
            priority.append(e164)
        elif len(info["uuids"]) >= MIN_DISTINCT_REPORTS:
            priority.append(e164)
    priority.sort()
    return priority


# ── Observabilité (aucun impact filtrage) ───────────────────────────────────

def locate(tranches: list, e164: int):
    lo, hi = 0, len(tranches) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        t = tranches[mid]
        if e164 < t.start:
            hi = mid - 1
        elif e164 > t.end:
            lo = mid + 1
        else:
            return mid
    return None


def build_stats(tranches: list, signalements: list) -> dict:
    now = datetime.now(timezone.utc)
    one_year_ago = now - timedelta(days=365)
    by_op = defaultdict(lambda: {"signalements": 0, "tranches": set()})
    for sig in signalements:
        if str(sig.get("verifie", "")).strip().upper() == "TEST":
            continue
        d = parse_date(sig.get("date", ""))
        if d is None or d < one_year_ago:
            continue
        e164 = national_to_e164(str(sig.get("numero", "")).strip())
        if e164 is None:
            continue
        i = locate(tranches, e164)
        if i is None:
            continue
        op = tranches[i].operateur
        by_op[op]["signalements"] += 1
        by_op[op]["tranches"].add(i)
    return {
        "generated": now.strftime("%Y-%m-%d"),
        "par_operateur": sorted(
            [{"operateur": op, "signalements": v["signalements"],
              "tranches_distinctes": len(v["tranches"])} for op, v in by_op.items()],
            key=lambda x: -x["signalements"],
        )[:30],
    }


# ── Sortie ──────────────────────────────────────────────────────────────────

def to_racines_dict(tranches: list) -> dict:
    out = defaultdict(list)
    for t in tranches:
        out[t.racine].append([t.start, t.end])
    return dict(out)


def content_hash(priority: list, racines: dict) -> str:
    payload = json.dumps({"priority": priority, "racines": racines},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def read_existing(path: str):
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
    out_dir = os.path.dirname(output_path) or "."

    print("Téléchargement de MAJNUM...")
    majnum_text = download_majnum()
    tranches = build_tranches(majnum_text)
    total_known = sum(t.size for t in tranches)
    racines_actives = sorted({t.racine for t in tranches})
    print(f"  {len(tranches)} tranches, {len(racines_actives)} racines actives, "
          f"{total_known:,} numéros MAJNUM")

    if not tranches:
        print("ERREUR : aucune tranche MAJNUM. On ne publie rien.", file=sys.stderr)
        sys.exit(1)

    print("Récupération des signalements...")
    try:
        signalements = fetch_signalements(signalements_url)
        print(f"  {len(signalements)} signalements bruts")
    except Exception as e:
        print(f"  Erreur signalements ({e}), on continue sans priority")
        signalements = []

    priority = build_priority(signalements)
    print(f"  {len(priority)} numéros prioritaires")

    # Table tranche→opérateur (Apps Script). AVANT le hash (ne dépend que de MAJNUM).
    ops_path = os.path.join(out_dir, "tranches_operateurs.json")
    with open(ops_path, "w", encoding="utf-8") as f:
        json.dump({"tranches": [[t.start, t.end, t.operateur] for t in tranches]},
                  f, separators=(",", ":"), ensure_ascii=False)
    print(f"Table écrite  : {ops_path} ({os.path.getsize(ops_path):,} octets, "
          f"{len(tranches)} tranches)")

    stats_path = os.path.join(out_dir, "stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(build_stats(tranches, signalements), f, indent=2, ensure_ascii=False)

    racines = to_racines_dict(tranches)
    covered = total_known

    print(f"\nPublication de l'INTÉGRALITÉ : {covered:,} numéros "
          f"(+ {len(priority)} prioritaires)")

    if covered < MIN_PLAUSIBLE_COVERAGE:
        print(f"ERREUR : couverture {covered:,} < seuil {MIN_PLAUSIBLE_COVERAGE:,}. "
              f"Probable bug — on NE PUBLIE PAS.", file=sys.stderr)
        sys.exit(1)

    coverage_by_racine = {
        r: {"total": sum(t.size for t in tranches if t.racine == r),
            "covered": sum(t.size for t in tranches if t.racine == r)}
        for r in racines_actives
    }
    for r in racines_actives:
        print(f"  {r} : {coverage_by_racine[r]['total']:>7,}  (100 %)")

    new_hash = content_hash(priority, racines)
    old_hash, old_version = read_existing(output_path)

    if old_hash == new_hash:
        print(f"\nContenu identique (hash {new_hash}). Aucune mise à jour de la liste.")
        print(f"  version conservée : {old_version}")
        return

    new_version = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"\nContenu modifié (ancien hash {old_hash}, nouveau {new_hash})")

    payload = {
        "version": new_version,
        "hash": new_hash,
        "total_known": total_known,
        "covered": covered,
        "priority_count": len(priority),
        "coverage_by_racine": coverage_by_racine,
        "priority": priority,
        "racines": racines,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    print(f"Fichier écrit : {output_path} ({os.path.getsize(output_path):,} octets)")
    print(f"  version  : {new_version}")
    print(f"  total    : {covered:,} numéros + {len(priority)} prioritaires")


if __name__ == "__main__":
    main()
