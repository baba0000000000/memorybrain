"""LA PASSE — une seule commande, un seul état, rien de refait deux fois.

Question traitée : comment savoir qu'un travail est fait, et ne pas le
refaire.

LA RÈGLE UNIQUE
---------------
**L'état est dans la base, jamais dans les fichiers.** Un fichier de travail
n'est qu'un cache, et son nom est son identité (`AAAA-MM-JJ_17h05.wav` = la
case de 17h05). S'il existe, c'est le bon ; s'il est enlevé, il se
reconstruit identique.

Par case, une machine à cinq états :

    assemblee -> transcrite -> diarisee -> ecrite

Une case n'est FINIE qu'à `ecrite` (ses fiches sont dans le socle). Tant
qu'elle ne l'est pas, la passe reprend là où elle s'est arrêtée — et elle ne
refait jamais une case déjà `ecrite`, parce qu'une case de 5 minutes, quand
elle est faite, elle est faite.

Usage :
    python3 passe.py --etat                 # où on en est, jour par jour
    python3 passe.py --etat --jour AAAA-MM-JJ
    python3 passe.py --plage AAAA-MM-JJTHH:MM:SS AAAA-MM-JJTHH:MM:SS
    python3 passe.py --ecrire --jour AAAA-MM-JJ
    python3 passe.py --menage              # vieux dossiers -> CORBEILLE
"""
from __future__ import annotations

import argparse
import datetime
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import blocs

ETATS = ("assemblee", "transcrite", "diarisee", "ecrite")


def _h(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def marquer(con: sqlite3.Connection, t_debut: float, statut: str) -> None:
    """Écrit l'état d'une case. Un seul endroit qui décide de l'avancement."""
    con.execute("UPDATE bloc SET statut=? WHERE start_time=?", (statut, t_debut))
    con.commit()


def cases_du_jour(con: sqlite3.Connection, jour: str) -> list:
    debut = datetime.datetime.fromisoformat(jour + "T00:00:00").timestamp()
    fin = debut + 86400
    return [dict(r) for r in con.execute(
        "SELECT start_time, statut, n_chunks FROM bloc"
        " WHERE start_time >= ? AND start_time < ? ORDER BY start_time",
        (debut, fin))]


def etat(con: sqlite3.Connection, jour: str | None = None) -> str:
    """Le tableau qui répond à « est-ce que c'est fait ? »."""
    q = ("SELECT start_time, statut, n_chunks FROM bloc ORDER BY start_time")
    rows = [dict(r) for r in con.execute(q)]
    if jour:
        rows = [r for r in rows if _h(r["start_time"]).startswith(jour)]
    if not rows:
        return "aucune case assemblée"

    par_jour: dict = {}
    for r in rows:
        par_jour.setdefault(_h(r["start_time"])[:10], []).append(r)

    lignes = [f"{'jour':12} {'cases':>5} {'transcrites':>11} "
              f"{'diarisées':>9} {'écrites':>7} {'restantes':>9}"]
    for j, liste in sorted(par_jour.items()):
        n = {e: sum(1 for r in liste if r["statut"] == e) for e in ETATS}
        # une case `ecrite` a franchi tous les états : on les compte comme acquises
        ecrites = n["ecrite"]
        lignes.append(
            f"{j:12} {len(liste):5} "
            f"{sum(1 for r in liste if r['statut'] in ('transcrite','diarisee','ecrite')):11} "
            f"{sum(1 for r in liste if r['statut'] in ('diarisee','ecrite')):9} "
            f"{ecrites:7} {len(liste)-ecrites:9}")
    return "\n".join(lignes)


def plage(con: sqlite3.Connection, depuis: str, jusqu_a: str,
          forcer_diar: bool = False) -> dict:
    """Traite une plage horaire : assemblage -> transcription -> diarisation."""
    import importlib
    tc = importlib.import_module("transcription.transcrire_case")
    td = importlib.import_module("outils.transcript_diarise")

    t0 = datetime.datetime.fromisoformat(depuis).timestamp()
    t1 = datetime.datetime.fromisoformat(jusqu_a).timestamp()
    r = blocs.assembler(con, depuis=t0, jusqu_a=t1)
    for d in r["detail"]:
        marquer(con, d["case"], "assemblee")

    bilan = []
    for d in r["detail"]:
        t = d["case"]
        et = con.execute("SELECT statut FROM bloc WHERE start_time=?", (t,)).fetchone()
        statut = et["statut"] if et else "assemblee"
        if statut == "ecrite" and not forcer_diar:
            bilan.append({"case": blocs.nom_case(t), "statut": "deja_ecrite"})
            continue
        x = tc.transcrire_case(con, t)
        if x.get("statut") in ("ok", "déjà_transcrite"):
            marquer(con, t, "transcrite")
        y = td.traiter_case(con, t, forcer=forcer_diar)
        if y.get("statut") == "ok":
            marquer(con, t, "diarisee")
        bilan.append({"case": blocs.nom_case(t), "etat": "diarisee",
                      "proprietaire": y.get("le propriétaire", 0), "contenu": y.get("contenu", 0)})
    return {"cases": bilan}


def corbeille(chemin: Path, lot: str) -> dict:
    """Déplace dans la corbeille datée. ON NE SUPPRIME JAMAIS (règle absolue)."""
    if not chemin.exists():
        return {"statut": "absent", "chemin": str(chemin)}
    dest = config.CORBEILLE / f"{datetime.date.today():%Y-%m-%d}-{lot}"
    dest.mkdir(parents=True, exist_ok=True)
    cible = dest / chemin.name
    if cible.exists():
        cible = dest / f"{chemin.name}-{datetime.datetime.now():%H%M%S}"
    shutil.move(str(chemin), str(cible))
    return {"statut": "deplace", "de": str(chemin), "vers": str(cible)}


def menage(con: sqlite3.Connection, appliquer: bool = False) -> list:
    """Met à la corbeille ce qui n'a plus lieu d'être dans l'atelier.

    Rien n'est supprimé : tout part dans la corbeille, sous `<date>-<lot>/`.
    """
    cibles = [
        (Path(config.WORK_GROUPES), "work-groupes-ancienne-methode"),
    ]
    out = []
    for chemin, lot in cibles:
        if not chemin.exists():
            out.append({"chemin": str(chemin), "statut": "absent"})
            continue
        taille = sum(f.stat().st_size for f in chemin.rglob("*") if f.is_file())
        if appliquer:
            out.append({"chemin": str(chemin), **corbeille(chemin, lot),
                        "octets": taille})
        else:
            out.append({"chemin": str(chemin), "statut": "a_deplacer",
                        "octets": taille})
    return out


def compte_rendu(con: sqlite3.Connection, jour: str | None = None) -> str:
    """Le compte rendu est GÉNÉRÉ, jamais tapé à la main.

    Défaut corrigé : un compte rendu étiquetait en « hier » des lignes du
    jour même. Les chiffres étaient bons, l'étiquette était
    inventée — et une étiquette inventée ne se teste pas, elle se supprime.
    Désormais on affiche ce que la base dit, tel quel.
    """
    rows = [dict(r) for r in con.execute(
        "SELECT start_time, statut, n_chunks FROM bloc ORDER BY start_time")]
    if jour:
        rows = [r for r in rows if _h(r["start_time"]).startswith(jour)]
    if not rows:
        return "aucune case"
    jours = sorted({_h(r["start_time"])[:10] for r in rows})
    n = {e: sum(1 for r in rows if r["statut"] == e) for e in ETATS}
    faites = n["ecrite"]
    minutes = 0.0
    for r in rows:
        c = con.execute("SELECT SUM(duration_sec)/60.0 m FROM chunk"
                        " WHERE start_time >= ? AND start_time < ?",
                        (r["start_time"], r["start_time"] + blocs.DUREE_CASE)).fetchone()
        minutes += (c["m"] or 0)
    # ce qui a été déposé dans le socle pour ces jours
    idees = notes = 0
    transcripts = 0
    for j in jours:
        for d, cible in ((config.DICTEES.glob(f"idees-{j[:4]}"), "idee"),
                         (config.DICTEES.glob(f"notes-{j[:4]}"), "note")):
            for dossier in d:
                for f in dossier.glob(f"*{j}*.md"):
                    if "source: audio" not in f.read_text(encoding="utf-8"):
                        continue
                    if cible == "idee":
                        idees += 1
                    else:
                        notes += 1
        transcripts += len(list(config.dossier_transcripts(int(j[:4])).glob(f"{j}_*.txt")))
    l = [f"COMPTE RENDU {', '.join(jours)} — généré depuis la base, aucun chiffre saisi à la main",
         f"  cases          : {len(rows)} (écrites {faites}, diarisées {n['diarisee']}, "
         f"transcrites {n['transcrite']}, assemblées {n['assemblee']})",
         f"  audio couvert  : {minutes:.0f} min",
         f"  restantes      : {len(rows) - faites} case(s)",
         f"  socle          : {idees} idée(s), {notes} note(s), {transcripts} transcript(s)"]
    return "\n".join(l)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--etat", action="store_true")
    ap.add_argument("--jour")
    ap.add_argument("--plage", nargs=2, metavar=("DEBUT", "FIN"))
    ap.add_argument("--ecrire", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--menage", action="store_true")
    ap.add_argument("--compte-rendu", action="store_true")
    ap.add_argument("--force-diar", action="store_true")
    a = ap.parse_args()

    con = db.connect(config.DB_PATH)

    if a.compte_rendu:
        print(compte_rendu(con, a.jour))
        con.close()
        return 0

    if a.menage:
        for r in menage(con, appliquer=not a.dry_run):
            if r.get("statut") == "deplace":
                print(f"  {r['chemin']} ({r['octets']/1e6:.0f} Mo) -> {r['vers']}")
            elif r.get("statut") == "a_deplacer":
                print(f"  à déplacer : {r['chemin']} ({r['octets']/1e6:.0f} Mo)")
            else:
                print(f"  {r['chemin']} : {r['statut']}")
        con.close()
        return 0

    if a.ecrire:
        import importlib
        w = importlib.import_module("outils.ecrire_socle")
        r = w.ecrire(con, jour=a.jour, dry_run=a.dry_run)
        print(w.resume(r))
        con.close()
        return 0

    if a.plage:
        r = plage(con, a.plage[0], a.plage[1], forcer_diar=a.force_diar)
        for c in r["cases"]:
            print(f"  {c['case']} : {c}")
        con.close()
        return 0

    print(etat(con, a.jour))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
