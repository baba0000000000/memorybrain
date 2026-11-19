"""LA CHAÎNE COMPLÈTE AVEC DIARISATION.

La diarisation découpe d'abord ; on identifie ensuite le locuteur qui est le
propriétaire du micro, puis on attribue chaque phrase à qui la prononce.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import blocs
from speakers import reference as ref
from transcription import hallucinations

ROUGE = "#c0392b"
BLEU = "#2471a3"
GRIS = "#7f8c8d"


def diariser(chemin: Path, forcer: bool = False) -> list[dict]:
    """Tours de parole d'une case. Résultat mis en cache sur disque."""
    config.WORK_DIARISATION.mkdir(parents=True, exist_ok=True)
    cache = config.WORK_DIARISATION / (chemin.stem + ".json")
    if cache.is_file() and not forcer:
        return json.loads(cache.read_text(encoding="utf-8"))["segments"]

    cmd = [str(config.NEMO_SPEECH), "diarize", str(chemin),
           "-m", str(config.MODELE_DIARISATION_GGUF),
           "--device", "cpu", "--format", "json"] + list(config.DIARISATION_OPTS)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"diarisation échouée : {r.stderr[-300:]}")
    data = json.loads(r.stdout)
    cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data["segments"]


def identifier(con, x, sr, tours: list[dict], profil) -> dict:
    """Quel locuteur est le propriétaire ? Une empreinte par locuteur, une décision.

    Renvoie {locuteur: {"duree", "score", "nom"}} où nom vaut 'le propriétaire' quand
    la décision est prise, sinon 'voix non identifiée' / 'contenu'.
    """
    import numpy as np

    par: dict = {}
    for t in tours:
        par.setdefault(t["speaker"], []).append(t)

    out = {}
    for sp, liste in par.items():
        duree = sum(t["end"] - t["start"] for t in liste)
        info = {"duree": round(duree, 1), "score": None, "nom": "voix non identifiée",
                "tours": len(liste)}
        if duree >= config.MIN_DUREE_IDENTITE:
            gros = sorted(liste, key=lambda t: -(t["end"] - t["start"]))[:6]
            morceaux = [x[int(t["start"] * sr):int(t["end"] * sr)] for t in gros]
            morceaux = [m for m in morceaux if len(m)]
            if morceaux:
                y = np.concatenate(morceaux)[:int(60 * sr)]
                sc = ref.score_fenetre(y, sr, profil)
                info["score"] = round(sc, 3) if sc is not None else None
                if sc is not None:
                    if sc >= config.VOIX_SEUIL_FORT:
                        info["nom"] = "le propriétaire"
                    else:
                        info["nom"] = "contenu"
        out[sp] = info
    return out


def locuteur_du_segment(tours: list[dict], debut: float, fin: float):
    """Le locuteur qui couvre le plus longtemps cette phrase (recouvrement)."""
    meilleur, part_max = None, 0.0
    for t in tours:
        rec = max(0.0, min(fin, t["end"]) - max(debut, t["start"]))
        if rec > part_max:
            meilleur, part_max = t["speaker"], rec
    if meilleur is None or part_max < 0.25 * max(1e-6, fin - debut):
        return None
    return meilleur


def traiter_case(con, t_debut: float, forcer: bool = False) -> dict:
    import soundfile as sf

    chemin = blocs.chemin_case(t_debut)
    if not chemin.is_file():
        return {"case": blocs.nom_case(t_debut), "statut": "absente"}

    tours = diariser(chemin, forcer)
    x, sr = sf.read(str(chemin), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    profil = ref.matrice(con)
    qui = identifier(con, x, sr, tours, profil)

    segs = con.execute(
        "SELECT start_time, end_time, text FROM segment"
        " WHERE start_time >= ? AND start_time < ? AND TRIM(COALESCE(text,'')) <> ''"
        " ORDER BY start_time", (t_debut, t_debut + blocs.DUREE_CASE)).fetchall()

    lignes, compte = [], {}
    filtres = 0
    bruit = 0
    for s in segs:
        # Whisper invente des phrases dans le silence (« Sous-titrage ST' 501 »,
        # « Merci. ») : la plupart des segments « non identifiés » étaient ces
        # hallucinations. On ne les écrit pas.
        if hallucinations.est_hallucination(s["text"]):
            filtres += 1
            continue
        a = blocs.position_dans_case(t_debut, s["start_time"])
        b = blocs.position_dans_case(t_debut, s["end_time"])
        sp = locuteur_du_segment(tours, a, b)
        nom = qui.get(sp, {}).get("nom", "voix non identifiée")
        # RECOURS quand la diarisation ne tranche pas (segment hors de tout
        # tour, ou locuteur trop court) : on demande à l'empreinte, comme
        # avant. C'est ce qui récupère les phrases noyées dans le bruit
        # (score ≥ 0,7) et écarte « Merci. » / « Sous-titrage FR ? »
        # (score < 0,4). L'ordre reste : diarisation d'abord, car elle
        # décide sur des minutes de voix et non sur 2,5 s.
        if nom == "voix non identifiée":
            j = ref.juger_segment(x[int(max(0.0, a) * sr):int(max(0.0, b) * sr)], sr, profil)
            if j["verdict"] == "proprietaire":
                nom = "le propriétaire"
            elif j["fenetres"] == 0:
                nom = "bruit"          # aucun avis : ni toi, ni du contenu parlé
            else:
                nom = "contenu"
        compte[nom] = compte.get(nom, 0) + 1
        h = datetime.datetime.fromtimestamp(s["start_time"]).strftime("%H:%M:%S")
        if nom == "le propriétaire":
            lignes.append(f'<span style="color:{ROUGE}">{h} — {s["text"]}</span>')
        elif nom == "contenu":
            lignes.append(f"{h} — {s['text']}")
        elif nom == "bruit":
            bruit += 1
            continue
        else:
            lignes.append(f'<span style="color:{GRIS}">{h} — {s["text"]}</span>')

    nom_case = blocs.nom_case(t_debut).replace(".wav", "")
    entete = [f"# {nom_case} — avec diarisation", "",
              f"Case : **{datetime.datetime.fromtimestamp(t_debut):%H:%M:%S} → "
              f"{datetime.datetime.fromtimestamp(t_debut + blocs.DUREE_CASE):%H:%M:%S}**", "",
              "| locuteur | tours | durée | score | verdict |", "|---|---|---|---|---|"]
    for sp, i in sorted(qui.items()):
        entete.append(f"| {sp} | {i['tours']} | {i['duree']} s | "
                      f"{i['score'] if i['score'] is not None else '—'} | {i['nom']} |")
    entete += ["", f'<span style="color:{ROUGE}">■</span> = ta voix · '
                   f'<span style="color:{GRIS}">■</span> = voix non identifiée · '
                   'noir = contenu', "", "---", ""]
    (config.WORK_TRACE / f"{nom_case}_DIAR.md").write_text(
        "\n".join(entete + lignes) + "\n", encoding="utf-8")

    return {"case": nom_case, "statut": "ok", "locuteurs": len(qui),
            "phrases": len(segs) - filtres - bruit, "hallucinations": filtres,
            "bruit": bruit, **compte,
            "detail": {str(k): v for k, v in qui.items()}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--forcer", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    debuts = [r["start_time"] for r in
              con.execute("SELECT start_time FROM bloc ORDER BY start_time")]
    tot: dict = {}
    for d in debuts:
        r = traiter_case(con, d, a.forcer)
        if r.get("statut") != "ok":
            print(f"  {r['case']} : {r['statut']}")
            continue
        print(f"  {r['case']} : {r['locuteurs']} locuteur(s) · {r['phrases']} phrases "
              f"({r['hallucinations']} hal. + {r['bruit']} bruits écartés) → "
              f"le propriétaire {r.get('le propriétaire', 0)} · contenu {r.get('contenu', 0)} · "
              f"non identifié {r.get('voix non identifiée', 0)}")
        for sp, i in r["detail"].items():
            print(f"       locuteur {sp} : {i['duree']:6.1f}s score={i['score']} → {i['nom']}")
        for k in ("le propriétaire", "contenu", "bruit"):
            tot[k] = tot.get(k, 0) + r.get(k, 0)
    print(f"\nTOTAL : {tot}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
