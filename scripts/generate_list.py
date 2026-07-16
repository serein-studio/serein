#!/usr/bin/env python3
# scripts/generate_list.py
#
# Génère demarchage_france.json en assemblant :
#   - priority : numéros signalés (via l'endpoint Apps Script), vérifiés
#   - racines  : tranches MAJNUM SÉLECTIONNÉES pour tenir dans le budget CallKit
#
# ─── POURQUOI CE SCRIPT SÉLECTIONNE ─────────────────────────────────────────
# L'extension iOS écrit les plages triées par ordre E.164 croissant et s'arrête
# au budget (1 990 000). Comme 0162 < 0270 < 0377 < 0424 < 0568 < 0948, c'était
# TOUJOURS la queue de 0948 qui sautait : ~193 000 numéros, ~61 % de la racine,
# de façon déterministe. Vérifié sur 17 appels de démarchage réellement reçus :
# l'ancienne logique en bloquait 12/17, et les 5 ratés étaient TOUS des 0948.
#
# On sélectionne donc CÔTÉ SERVEUR : le JSON publié tient déjà dans le budget,
# l'extension ne tronque plus jamais (son garde-fou runtime devient un filet de
# sécurité inactif). AUCUN changement côté app : elle décode ce qu'on lui donne.
#
# ⚠️ CE SCRIPT PUBLIE DIRECTEMENT À TOUS LES UTILISATEURS, SANS REVIEW APPLE.
# Une liste vide ou aberrante désactiverait la protection de tout le monde en
# 24 h. D'où le garde-fou de plausibilité (voir MIN_PLAUSIBLE_COVERAGE).
#
# Usage : python generate_list.py <URL_SIGNALEMENTS> <sortie.json>

import sys
import os
import csv
import io
import json
import hashlib
import math
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import NamedTuple

# Les 12 racines de démarchage ARCEP (métropole, loi Naegelen n° 2020-901)
DEMARCHAGE_PREFIXES = [
    "0162", "0163", "0270", "0271", "0377", "0378",
    "0424", "0425", "0568", "0569", "0948", "0949",
]

MAJNUM_URL = "https://extranet.arcep.fr/uploads/MAJNUM.csv"

# ─── BUDGET ─────────────────────────────────────────────────────────────────
# Plafond CallKit constaté empiriquement : 2 000 000 d'entrées, COMMUN entre
# identification et blocage. On garde une marge (1 990 000), comme l'extension.
MAX_ENTRIES = 1_990_000

# Garde-fou de plausibilité : en dessous, on refuse de publier. Protège contre
# un bug qui viderait la liste et désactiverait la protection de tous.
MIN_PLAUSIBLE_COVERAGE = 1_500_000

# ─── SCORING (réglable) ─────────────────────────────────────────────────────
# Un signalement perd la moitié de son poids tous les HALF_LIFE_DAYS jours.
# C'est ce qui rend la stratégie DYNAMIQUE : une tranche neutralisée il y a
# longtemps s'efface d'elle-même au profit des tranches actives, sans règle de
# retrait à écrire. La purge à 12 mois (Apps Script) ferme la fenêtre.
HALF_LIFE_DAYS = 90

W_DIRECT = 3.0     # signalement DANS la tranche : signal le plus fort
W_GROUP = 1.0      # signalement dans le même groupe contigu : anticipe la rotation
W_OPERATOR = 0.5   # opérateur dominant : signal le plus large, le plus faible

# ─── RÈGLE DE DOMINANCE D'OPÉRATEUR ─────────────────────────────────────────
# Un opérateur n'est "dominant" que s'il concentre une grande part des
# signalements ET les disperse sur plusieurs tranches.
# Rationnel : 10 signalements dans UNE tranche = un client isolé. 10 sur 8
# tranches = l'opérateur vend en masse à des acteurs agressifs.
OPERATOR_DOMINANCE_THRESHOLD = 0.60
OPERATOR_MIN_DISTINCT_TRANCHES = 5

# GARDE-FOU : ne JAMAIS déclencher sur un échantillon faible. Sans ces seuils,
# 17 signalements d'UNE seule personne suffiraient à désigner un opérateur —
# du surapprentissage pur. La règle s'activera d'elle-même quand la base
# d'utilisateurs le justifiera. Ne JAMAIS coder un opérateur en dur.
OPERATOR_RULE_MIN_REPORTS = 100
OPERATOR_RULE_MIN_DISTINCT_UUIDS = 30

MIN_DISTINCT_REPORTS = 3   # personnes distinctes requises hors préfixes connus


class Tranche(NamedTuple):
    start: int          # E.164, ex: 33948257000
    end: int            # E.164, ex: 33948257999
    racine: str         # ex: "0948"
    operateur: str      # code Mnémo, ex: "KAVE"

    @property
    def size(self) -> int:
        return self.end - self.start + 1


# ════════════════════════════════════════════════════════════════════════════
# MAJNUM
# ════════════════════════════════════════════════════════════════════════════

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
    ⚠️ PIÈGE : le 0 initial est absent du MAJNUM en ligne ('162000000' au lieu
    de '0162000000'). D'où le zfill(10).
    """
    val = parse_int_safe(raw)
    if val is None:
        return None
    national = str(val).zfill(10)
    if len(national) != 10 or not national.startswith("0"):
        return None
    return int("33" + national[1:])


def build_tranches(majnum_text: str) -> list:
    """
    Parse MAJNUM → liste de Tranche, triée par start croissant.
    ⚠️ Conserve la colonne 'Mnémo' (l'opérateur attributaire) : tout le scoring
    en dépend. NB : l'opérateur est l'ATTRIBUTAIRE, pas le démarcheur final —
    il revend à de nombreux clients (voir le dossier de reprise).
    """
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
        debut_national = str(val).zfill(10)
        racine = debut_national[:4]
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


def build_contiguous_groups(tranches: list) -> dict:
    """
    Identifie les GROUPES CONTIGUS : suites de tranches où end[i]+1 == start[i+1]
    ET même opérateur. C'est le "lot d'achat", l'unité naturelle d'un démarcheur.

    Constat empirique : KAVE détient 0270290→0270296 en bloc contigu de 7
    tranches, et les appels observés viennent de 290 (10 mars), 292 (23 juin),
    puis 294 (3 juillet) — un acteur qui fait tourner ses numéros dans son lot,
    en montant. D'où W_GROUP : anticiper le prochain saut.

    Renvoie {index_tranche: id_groupe}.
    """
    group_of = {}
    gid = 0
    for i, t in enumerate(tranches):
        if i > 0:
            prev = tranches[i - 1]
            if prev.operateur == t.operateur and prev.end + 1 == t.start:
                group_of[i] = group_of[i - 1]
                continue
            gid += 1
        group_of[i] = gid
    return group_of


# ════════════════════════════════════════════════════════════════════════════
# SIGNALEMENTS
# ════════════════════════════════════════════════════════════════════════════

def national_to_e164(national: str):
    """
    Convertit un numéro national en E.164.
    Gère : 0759254806, 759254806 (0 perdu par le Sheet), 33759254806.
    """
    digits = "".join(c for c in national if c.isdigit())
    if not digits:
        return None
    if digits.startswith("33") and len(digits) >= 11:
        return int(digits)
    if digits.startswith("0"):
        return int("33" + digits[1:])
    # ⚠️ Le 0 initial a été perdu (Sheet qui traite le numéro comme un nombre)
    if len(digits) == 9:
        return int("33" + digits)
    return None


def fetch_signalements(url: str) -> list:
    """Récupère les signalements depuis l'endpoint Apps Script (JSON)."""
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
    """
    Couche priority : numéros individuels signalés. Budget RÉSERVÉ, jamais
    tronqué par l'extension. Mécanisme inchangé.

    Règles :
      - TEST : "Vérifié" == "TEST" → toujours retenu.
      - PRÉFIXE CONNU : 1 signalement suffit (< 1 an) — le préfixe est déjà un
        garde-fou réglementaire.
      - HORS PRÉFIXE : ≥ MIN_DISTINCT_REPORTS UUID distincts (< 1 an).
    """
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
        if info["test"]:
            priority.append(e164)
        elif info["known"]:
            priority.append(e164)
        elif len(info["uuids"]) >= MIN_DISTINCT_REPORTS:
            priority.append(e164)

    priority.sort()
    return priority


def extract_scoring_reports(signalements: list) -> list:
    """
    Extrait les signalements exploitables pour le SCORING (distinct de priority).
    Ne garde que ceux qui tombent dans une racine connue et datent de moins d'un an.
    Renvoie [(e164, poids_décroissant, uuid)].
    """
    now = datetime.now(timezone.utc)
    one_year_ago = now - timedelta(days=365)
    out = []

    for sig in signalements:
        if str(sig.get("verifie", "")).strip().upper() == "TEST":
            continue  # les numéros de test ne doivent pas biaiser le scoring

        e164 = national_to_e164(str(sig.get("numero", "")).strip())
        if e164 is None:
            continue

        d = parse_date(sig.get("date", ""))
        if d is None or d < one_year_ago:
            continue

        age_days = max(0.0, (now - d).total_seconds() / 86400.0)
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        uuid = str(sig.get("uuid", "")).strip()
        out.append((e164, weight, uuid))

    return out


# ════════════════════════════════════════════════════════════════════════════
# SÉLECTION
# ════════════════════════════════════════════════════════════════════════════

def locate(tranches: list, e164: int):
    """Index de la tranche contenant e164, ou None. Dichotomie (tranches triées)."""
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


def compute_dominant_operators(tranches: list, reports: list) -> tuple:
    """
    Détermine les opérateurs dominants. Renvoie (set_dominants, diagnostic).
    Inerte tant que les garde-fous ne sont pas franchis.
    """
    diag = {
        "reports_on_known_racines": 0,
        "distinct_uuids": 0,
        "rule_active": False,
        "dominant": [],
    }

    by_op_weight = defaultdict(float)
    by_op_tranches = defaultdict(set)
    uuids = set()
    total_weight = 0.0
    n_reports = 0

    for e164, weight, uuid in reports:
        i = locate(tranches, e164)
        if i is None:
            continue  # hors racines connues : ne compte pas dans la dominance
        op = tranches[i].operateur
        by_op_weight[op] += weight
        by_op_tranches[op].add(i)
        total_weight += weight
        n_reports += 1
        if uuid:
            uuids.add(uuid)

    diag["reports_on_known_racines"] = n_reports
    diag["distinct_uuids"] = len(uuids)

    # ⚠️ GARDE-FOU : échantillon trop faible → règle inerte.
    if n_reports < OPERATOR_RULE_MIN_REPORTS or len(uuids) < OPERATOR_RULE_MIN_DISTINCT_UUIDS:
        return set(), diag

    diag["rule_active"] = True
    dominant = set()
    for op, w in by_op_weight.items():
        share = w / total_weight if total_weight else 0.0
        spread = len(by_op_tranches[op])
        if share > OPERATOR_DOMINANCE_THRESHOLD and spread >= OPERATOR_MIN_DISTINCT_TRANCHES:
            dominant.add(op)
            diag["dominant"].append({"operateur": op, "part": round(share, 3), "tranches": spread})

    return dominant, diag


def compute_scores(tranches: list, group_of: dict, reports: list, dominant: set) -> list:
    """
    Score continu par tranche :
        W_DIRECT   * Σ(poids des signalements DANS la tranche)
      + W_GROUP    * Σ(poids des signalements dans le MÊME GROUPE CONTIGU)
      + W_OPERATOR * (1 si opérateur dominant)

    Zéro signalement → tous les scores à 0 → le tri retombe sur start croissant,
    c'est-à-dire exactement la stratégie "quota proportionnel" pure. Le
    comportement dégradé est donc gratuit et sûr.
    """
    direct = defaultdict(float)
    group_weight = defaultdict(float)

    for e164, weight, _ in reports:
        i = locate(tranches, e164)
        if i is None:
            continue
        direct[i] += weight
        group_weight[group_of[i]] += weight

    scores = []
    for i, t in enumerate(tranches):
        s = W_DIRECT * direct.get(i, 0.0)
        s += W_GROUP * group_weight.get(group_of[i], 0.0)
        if t.operateur in dominant:
            s += W_OPERATOR
        scores.append(s)
    return scores


def select_tranches(tranches: list, scores: list, budget: int) -> tuple:
    """
    Sélection : quota proportionnel par racine, servi par score décroissant.

    Le quota garantit qu'aucune racine n'est sacrifiée entièrement (l'ancien
    défaut : 0948 amputé de 61 %). Le score décide, À L'INTÉRIEUR du quota,
    quelles tranches passent en premier.

    Renvoie (set des index retenus, quotas).
    """
    size_by_racine = defaultdict(int)
    for t in tranches:
        size_by_racine[t.racine] += t.size
    total = sum(size_by_racine.values())
    if total == 0:
        return set(), {}

    quota = {r: int(n / total * budget) for r, n in size_by_racine.items()}

    # Tri par (score décroissant, start croissant) — le start départage pour
    # rester déterministe (même entrée → même sortie → hash stable).
    order = sorted(range(len(tranches)), key=lambda i: (-scores[i], tranches[i].start))

    selected = set()
    used = defaultdict(int)
    for i in order:
        t = tranches[i]
        if used[t.racine] + t.size <= quota[t.racine]:
            selected.add(i)
            used[t.racine] += t.size

    return selected, quota


def to_racines_dict(tranches: list, selected: set) -> dict:
    """Format attendu par l'app : {prefix: [[start, end], ...]}, trié croissant."""
    out = defaultdict(list)
    for i in sorted(selected, key=lambda i: tranches[i].start):
        t = tranches[i]
        out[t.racine].append([t.start, t.end])
    return dict(out)


# ════════════════════════════════════════════════════════════════════════════
# SORTIE
# ════════════════════════════════════════════════════════════════════════════

def content_hash(priority: list, racines: dict) -> str:
    """
    Empreinte du CONTENU PUBLIÉ (priority + tranches retenues).

    ⚠️ N'inclut PAS les scores. Les scores décroissent chaque jour : les hacher
    republierait le fichier quotidiennement pour rien. Seul un basculement
    réel d'une tranche dedans/dehors doit produire une nouvelle version.
    """
    payload = json.dumps(
        {"priority": priority, "racines": racines},
        sort_keys=True, separators=(",", ":")
    )
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


def build_stats(tranches: list, selected: set, reports: list, group_of: dict, diag: dict) -> dict:
    """
    Observabilité : comptage des signalements par opérateur et par groupe.
    Ne change RIEN au filtrage. Sert à savoir, dans six mois, si la domination
    d'un opérateur est un fait national ou l'anecdote d'un seul utilisateur —
    et donc s'il faut laisser la composante opérateur s'activer.
    """
    by_op = defaultdict(lambda: {"signalements": 0, "poids": 0.0, "tranches": set()})
    by_group = defaultdict(lambda: {"signalements": 0, "operateur": None, "racine": None})

    for e164, weight, _ in reports:
        i = locate(tranches, e164)
        if i is None:
            continue
        t = tranches[i]
        by_op[t.operateur]["signalements"] += 1
        by_op[t.operateur]["poids"] += weight
        by_op[t.operateur]["tranches"].add(i)
        g = by_group[group_of[i]]
        g["signalements"] += 1
        g["operateur"] = t.operateur
        g["racine"] = t.racine

    return {
        "regle_operateur": diag,
        "par_operateur": sorted(
            [
                {
                    "operateur": op,
                    "signalements": v["signalements"],
                    "poids_decroissant": round(v["poids"], 2),
                    "tranches_distinctes": len(v["tranches"]),
                }
                for op, v in by_op.items()
            ],
            key=lambda x: -x["signalements"],
        )[:20],
        "groupes_les_plus_signales": sorted(
            [
                {"groupe": g, "operateur": v["operateur"], "racine": v["racine"],
                 "signalements": v["signalements"]}
                for g, v in by_group.items()
            ],
            key=lambda x: -x["signalements"],
        )[:20],
    }


def main():
    if len(sys.argv) < 3:
        print("Usage: generate_list.py <URL_SIGNALEMENTS> <sortie.json>")
        sys.exit(1)

    signalements_url = sys.argv[1]
    output_path = sys.argv[2]

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

    group_of = build_contiguous_groups(tranches)
    print(f"  {len(set(group_of.values()))} groupes contigus (lots d'achat)")

    print("Récupération des signalements...")
    try:
        signalements = fetch_signalements(signalements_url)
        print(f"  {len(signalements)} signalements bruts")
    except Exception as e:
        print(f"  Erreur signalements ({e}), on continue sans priority")
        signalements = []

    priority = build_priority(signalements)
    reports = extract_scoring_reports(signalements)
    print(f"  {len(priority)} numéros prioritaires, {len(reports)} signalements pour le scoring")

    # Budget MAJNUM = plafond moins le budget réservé aux priority
    budget = max(0, MAX_ENTRIES - len(priority))

    dominant, diag = compute_dominant_operators(tranches, reports)
    if diag["rule_active"]:
        print(f"  Règle opérateur ACTIVE — dominants : {sorted(dominant) or 'aucun'}")
    else:
        print(f"  Règle opérateur inerte ({diag['reports_on_known_racines']} signalements, "
              f"{diag['distinct_uuids']} UUID — seuils : {OPERATOR_RULE_MIN_REPORTS}/"
              f"{OPERATOR_RULE_MIN_DISTINCT_UUIDS})")

    scores = compute_scores(tranches, group_of, reports, dominant)
    selected, quota = select_tranches(tranches, scores, budget)
    racines = to_racines_dict(tranches, selected)
    covered = sum(tranches[i].size for i in selected)

    print(f"\nSélection : {len(selected)}/{len(tranches)} tranches, "
          f"{covered:,}/{budget:,} numéros ({covered/total_known*100:.1f}% de MAJNUM)")

    # ⚠️ GARDE-FOU DE PLAUSIBILITÉ. Ce script publie sans review : une liste
    # aberrante désactiverait la protection de tous les utilisateurs en 24 h.
    if covered < MIN_PLAUSIBLE_COVERAGE:
        print(f"ERREUR : couverture {covered:,} < seuil {MIN_PLAUSIBLE_COVERAGE:,}. "
              f"Probable bug — on NE PUBLIE PAS.", file=sys.stderr)
        sys.exit(1)
    if covered > budget:
        print(f"ERREUR : {covered:,} dépasse le budget {budget:,}.", file=sys.stderr)
        sys.exit(1)

    coverage_by_racine = {}
    for r in racines_actives:
        tot = sum(t.size for t in tranches if t.racine == r)
        cov = sum(tranches[i].size for i in selected if tranches[i].racine == r)
        coverage_by_racine[r] = {"total": tot, "covered": cov}
        print(f"  {r} : {cov:>7,} / {tot:>7,}  ({cov/tot*100:5.1f}%)  quota {quota[r]:,}")

    new_hash = content_hash(priority, racines)
    old_hash, old_version = read_existing(output_path)

    if old_hash == new_hash:
        print(f"\nContenu identique (hash {new_hash}). Aucune mise à jour.")
        print(f"  version conservée : {old_version}")
        return

    new_version = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"\nContenu modifié (ancien hash {old_hash}, nouveau {new_hash})")

    payload = {
        "version": new_version,
        "hash": new_hash,
        # ─── Métadonnées de transparence ───────────────────────────────────
        # Le JSON étant pré-tronqué, l'app perdrait sinon le dénominateur réel
        # et annoncerait une protection qu'elle ne fournit pas.
        # NB : l'app 1.0 les ignore (JSONDecoder ignore les clés inconnues).
        "total_known": total_known,
        "covered": covered,
        "coverage_by_racine": coverage_by_racine,
        "priority": priority,
        "racines": racines,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    # Fichier de stats séparé : observation uniquement, aucun impact filtrage.
    stats_path = os.path.join(os.path.dirname(output_path) or ".", "stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(build_stats(tranches, selected, reports, group_of, diag),
                  f, indent=2, ensure_ascii=False)

    size = os.path.getsize(output_path)
    print(f"Fichier écrit : {output_path} ({size:,} octets)")
    print(f"Stats écrites : {stats_path}")
    print(f"  version  : {new_version}")
    print(f"  hash     : {new_hash}")
    print(f"  priority : {len(priority)}")
    print(f"  couvert  : {covered:,} / {total_known:,}")


if __name__ == "__main__":
    main()
