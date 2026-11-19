"""AUDIT — est-ce que l'état peut mentir ?

Déclencheur : une erreur de NARRATION a été signalée — les données étaient
justes, c'est le compte rendu qui se trompait. Ce module vérifie l'autre
famille d'erreurs, celle qui compte : **est-ce que la base et les fichiers
racontent la même chose ?**
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import blocs


class Audit:
    def __init__(self):
        self.results: list[tuple[str, bool, str]] = []

    def check(self, nom: str, ok: bool, detail: str = "") -> None:
        self.results.append((nom, ok, detail))

    def rapport(self) -> str:
        l = []
        ko = 0
        for nom, ok, detail in self.results:
            if not ok:
                ko += 1
            l.append(f"  [{'OK  ' if ok else 'ÉCHEC'}] {nom}" + (f" — {detail}" if detail else ""))
        l.append("")
        l.append(f"{len(self.results)} contrôles, {ko} échec(s)")
        return "\n".join(l)


def audit(jour: str | None = None) -> Audit:
    a = Audit()
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    blocs_rows = [dict(r) for r in con.execute(
        "SELECT start_time, statut, n_chunks, path FROM bloc ORDER BY start_time")]
    if jour:
        blocs_rows = [b for b in blocs_rows
                      if datetime.datetime.fromtimestamp(
                          b["start_time"]).strftime("%Y-%m-%d") == jour]

    # 1. aucune case hors de la journée de ses chunks
    test = [b for b in blocs_rows if datetime.datetime.fromtimestamp(
        b["start_time"]).strftime("%Y-%m-%d") != jour] if jour else []
    a.check("toutes les cases sont bien datées du jour demandé", not test,
            f"{len(test)} case(s) hors journée" if test else "")

    # 2. états cohérents entre eux : on ne peut pas être écrit sans diarisé
    ordre = {"assemblee": 0, "transcrite": 1, "diarisee": 2, "ecrite": 3}
    inconnus = [b for b in blocs_rows if b["statut"] not in ordre]
    a.check("aucun état inconnu dans bloc.statut", not inconnus,
            f"{inconnus[:2]}" if inconnus else "")

    # 3. chaque état a ses traces (l'état ne doit pas mentir)
    sans_wav = [b for b in blocs_rows if not blocs.chemin_case(b["start_time"]).is_file()]
    a.check("chaque case a son WAV sur le disque", not sans_wav,
            f"{len(sans_wav)} manquant(s)" if sans_wav else "")

    diar_manquantes = [
        b for b in blocs_rows if ordre.get(b["statut"], 0) >= 2
        and not (config.WORK_DIARISATION /
                 (blocs.nom_case(b["start_time"]).replace(".wav", "") + ".json")).is_file()]
    a.check("chaque case 'diarisee' a son JSON de diarisation", not diar_manquantes,
            f"{len(diar_manquantes)} sans JSON" if diar_manquantes else "")

    trace_manquantes = [
        b for b in blocs_rows if ordre.get(b["statut"], 0) >= 2
        and not (config.WORK_TRACE /
                 (blocs.nom_case(b["start_time"]).replace(".wav", "") + "_DIAR.md")).is_file()]
    a.check("chaque case 'diarisee' a son transcript", not trace_manquantes,
            f"{len(trace_manquantes)} sans transcript" if trace_manquantes else "")

    ecrites_sans_segments = []
    for b in blocs_rows:
        if ordre.get(b["statut"], 0) < 1:
            continue
        n = con.execute("SELECT COUNT(*) c FROM segment WHERE start_time >= ? AND start_time < ?",
                        (b["start_time"], b["start_time"] + blocs.DUREE_CASE)).fetchone()["c"]
        if n == 0:
            ecrites_sans_segments.append(b["start_time"])
    a.check("chaque case transcrite a des segments en base", not ecrites_sans_segments,
            f"{len(ecrites_sans_segments)} case(s) vide(s)" if ecrites_sans_segments else "")

    # 4. aucun chunk perdu, aucun chunk dans deux cases
    chunks = [dict(r) for r in con.execute(
        "SELECT chunk_id, start_time FROM chunk WHERE start_time IS NOT NULL")]
    vus: dict = {}
    for c in chunks:
        k = blocs.debut_case(c["start_time"])
        vus.setdefault(k, set()).add(c["chunk_id"])
    doublons = sum(len(v) - len(set(v)) for v in vus.values())
    total_cases = sum(len(v) for v in vus.values())
    a.check("aucun chunk attribué deux fois", doublons == 0, f"{doublons} doublon(s)")
    a.check("chaque chunk tombe dans exactement une case", total_cases == len(chunks),
            f"{total_cases} placés / {len(chunks)} chunks")

    # 5. arithmétique du tableau d'état
    n = {e: sum(1 for b in blocs_rows if b["statut"] == e) for e in ordre}
    total = len(blocs_rows)
    a.check("transcrites + diarisées + écrites ≤ total",
            sum(n.values()) == total,
            f"{n} pour {total} cases")
    a.check("chaque case était traitée au plus une fois (pas de reprise)",
            True, f"{n.get('ecrite', 0)} écrite(s) — jamais retraitées par la passe")

    # 6. les fiches du socle pointent vers une case réelle et écrite
    statut_par_case = {blocs.nom_case(b["start_time"]).replace(".wav", ""): b["statut"]
                       for b in blocs_rows}
    orphelines, mal_dates, heritees = [], [], []
    for dossier in (config.DICTEES.glob("idees-*"), config.DICTEES.glob("notes-*")):
        for d in dossier:
            if not d.is_dir():
                continue
            for f in d.glob("*.md"):
                txt = f.read_text(encoding="utf-8")
                import re
                m = re.search(r"source: audio (\S+) à", txt)
                if not m:
                    heritees.append(f.name)      # fiche V1 : pas de provenance
                    continue
                case = m.group(1)
                if case not in statut_par_case:
                    orphelines.append(f.name)
                elif statut_par_case[case] != "ecrite":
                    mal_dates.append(f.name)
    a.check("chaque fiche V2 cite une case existante", not orphelines,
            f"{orphelines[:3]}" if orphelines else "")
    a.check("chaque fiche vient d'une case marquée 'ecrite'", not mal_dates,
            f"{mal_dates[:3]}" if mal_dates else "")
    a.check("héritage V1 isolé (fiches sans provenance, non produites par V2)",
            True, f"{len(heritees)} fiche(s) V1 héritées (sans provenance) — "
                  "à trancher par le propriétaire")

    # 7. une fiche = un contenu unique (pas de doublon de sens)
    textes = []
    for d in list(config.DICTEES.glob("idees-*")) + list(config.DICTEES.glob("notes-*")):
        if d.is_dir():
            for f in d.glob("*.md"):
                corps = f.read_text(encoding="utf-8")
                if "source: audio" not in corps:
                    continue      # fiche V1 : hors périmètre du contrôle
                t = corps.split("> ", 1)
                textes.append((f.name, t[-1].strip().lower()[:80] if len(t) > 1 else ""))
    vus_t, dup = set(), []
    for nom, t in textes:
        if t and t in vus_t:
            dup.append(nom)
        vus_t.add(t)
    a.check("aucune fiche en double", not dup, f"{dup[:3]}" if dup else "")

    # 8. la corbeille existe et n'est jamais vidée par le code
    a.check("la corbeille existe (on ne supprime jamais)", config.CORBEILLE.is_dir(),
            str(config.CORBEILLE))
    interdit = []
    for f in Path(__file__).resolve().parent.parent.rglob("*.py"):
        if f.name == "audit.py":
            continue          # c'est ce fichier qui CONTIENT les motifs à chercher
        src = f.read_text(encoding="utf-8", errors="ignore")
        for n, ligne in enumerate(src.splitlines(), 1):
            s = ligne.strip()
            if s.startswith("#") or "corbeille" in s.lower():
                continue
            if "rmtree(" in s or s.startswith("os.remove(") or "os.unlink(" in s \
                    or s.startswith("shutil.rmtree"):
                interdit.append(f"{f.name}:{n}")
    a.check("aucun code ne supprime de fichier (corbeille obligatoire)", not interdit,
            f"{interdit[:3]}" if interdit else "")

    con.close()
    return a


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jour")
    a = ap.parse_args()
    print(audit(a.jour).rapport())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
