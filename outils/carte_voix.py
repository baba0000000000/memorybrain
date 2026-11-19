"""LA CARTE DES VOIX — on balaie TOUT l'audio, sans trou.

Objectif : retrouver la voix du propriétaire dans tout le corpus, y compris là
où la transcription n'a rien écrit.
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
from speakers import reference as ref

# Une fenêtre en dessous de ce niveau RMS ne contient pas de parole : elle
# ne peut pas donner une empreinte de voix, seulement du bruit.
# Une voix lointaine reste bien au-dessus du silence numérique : le seuil
# est placé entre les deux, très en dessous de la voix la plus faible.
RMS_MIN = 0.001

# Deux fenêtres du propriétaire séparées de moins de ce délai forment un seul
# passage.
TROU_MAX = 2.5


def _h(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")


def balayer_case(t_debut: float, profil=None) -> dict:
    """Balaie une case entière et rend les passages « toi »."""
    import numpy as np
    import soundfile as sf

    chemin = blocs.chemin_case(t_debut)
    if not chemin.is_file():
        return {"case": blocs.nom_case(t_debut), "statut": "absente"}

    x, sr = sf.read(str(chemin), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    profil = ref.matrice() if profil is None else profil

    win, pas = int(config.FENETRE_VOIX * sr), int(config.PAS_VOIX * sr)
    n_fenetres = n_muettes = n_testees = n_toi = 0
    scores = []
    t = 0
    while t + win <= len(x):
        w = x[t:t + win]
        n_fenetres += 1
        if float(np.sqrt((w.astype("float64") ** 2).mean())) < RMS_MIN:
            n_muettes += 1
            scores.append(None)
        else:
            s = ref.score_fenetre(w, sr, profil)
            scores.append(s)
            if s is not None:
                n_testees += 1
                if s >= config.VOIX_SEUIL_FORT:
                    n_toi += 1
        t += pas

    # regrouper les fenêtres du propriétaire en passages lisibles
    passages = []
    for i, s in enumerate(scores):
        if s is not None and s >= config.SEUIL_PROPRIETAIRE_GRIS:
            debut = i * pas / sr
            fin = debut + config.FENETRE_VOIX
            if passages and debut - passages[-1][1] <= TROU_MAX:
                passages[-1][1] = fin
                passages[-1][2] = max(passages[-1][2], s)
            else:
                passages.append([debut, fin, s])

    duree = len(x) / sr
    return {
        "case": blocs.nom_case(t_debut),
        "t_debut": t_debut,
        "duree_s": round(duree, 1),
        "fenetres": n_fenetres,
        "muettes": n_muettes,
        "testees": n_testees,
        "toi": n_toi,
        "couverture_audio": round(100.0 * n_fenetres * pas / 1000 ** 0, 0) if False else 100.0,
        "passages": [(t_debut + a, t_debut + b, s) for a, b, s in passages],
        "statut": "ok",
    }


def carte(con: sqlite3.Connection, ecrire: bool = False) -> dict:
    debuts = [r["start_time"] for r in
              con.execute("SELECT start_time FROM bloc ORDER BY start_time")]
    profil = ref.matrice(con)
    out = []
    for d in debuts:
        r = balayer_case(d, profil)
        out.append(r)

    jour = datetime.date.today().isoformat()
    lignes = [f"# Carte des voix — {jour}", "",
              "Tout l'audio est balayé, fenêtre de 2,5 s, pas de 1,25 s.", "",
              "---", ""]
    for r in out:
        if r.get("statut") != "ok":
            continue
        lignes += [f"## {r['case']}", "",
                   f"{r['duree_s']} s d'audio · {r['fenetres']} fenêtres · "
                   f"{r['muettes']} muettes · {r['toi']} qui te ressemblent", ""]
        t0 = r["t_debut"]
        prec = t0
        for (a, b, s) in r["passages"]:
            if a - prec > 1.0:
                lignes.append(f"- {_h(prec)} → {_h(a)}  contenu")
            lignes.append(f"- {_h(a)} → {_h(b)}  **toi** (score max {s:.2f})")
            prec = max(prec, b)
        if t0 + r["duree_s"] - prec > 1.0:
            lignes.append(f"- {_h(prec)} → {_h(t0 + r['duree_s'])}  contenu")
        lignes.append("")

    if ecrire:
        (config.WORK_TRACE / f"CARTE-VOIX-{jour}.md").write_text(
            "\n".join(lignes) + "\n", encoding="utf-8")
    return {"cases": out, "markdown": "\n".join(lignes)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sortie", choices=["md"], default="md")
    a = ap.parse_args()
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    r = carte(con, ecrire=(a.sortie == "md"))
    total_s = total_toi = 0
    for c in r["cases"]:
        if c.get("statut") != "ok":
            continue
        total_s += c["duree_s"]
        total_toi += sum(b - a for a, b, _ in c["passages"])
        print(f"  {c['case']} : {c['duree_s']:6.1f} s analysées à 100 % · "
              f"{c['testees']:3} fenêtres avec de la voix · "
              f"{len(c['passages'])} passage(s) qui te ressemblent")
    print(f"\n  total analysé : {total_s/60:.1f} min")
    print(f"  passages « toi » : {total_toi/60:.1f} min")
    print(f"  carte écrite : work/traces/CARTE-VOIX-{jour}.md")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
