"""ÉTAPE 2 — MARQUER CHAQUE PHRASE : c'est toi, ou c'est du contenu ?

Choix de conception : transcrire d'abord, puis marquer la voix —
parce qu'on a besoin du texte du contenu pour le wiki, et parce qu'une phrase
est l'unité qu'on veut lire.

Ce que le module fait, pour chaque phrase d'une case de 5 minutes :

  1. il découpe la phrase en fenêtres de 2,5 s (pas de 1,25 s) ;
  2. il jette les fenêtres sans parole (le silence ne donne pas une empreinte
     de voix, il donne du bruit qui peut ressembler à n'importe qui) ;
  3. il compare chaque fenêtre au profil construit sur les références de
     le propriétaire (`speakers/reference.py`) ;
  4. il VOTE : moitié des fenêtres au-dessus du seuil fort = « toi » ;
     35 % au moins = « probable » ; sinon « contenu ».

Sortie : deux fichiers markdown par case.
  - `<case>.md`      le transcript complet, les phrases du propriétaire en ROUGE ;
  - `<case>_TOI.md`  seulement ses phrases, pour une relecture rapide.

⚠ Le rouge dit « le programme pense que c'est toi », pas « c'est prouvé ».
Le seuil est provisoire tant que le test à l'aveugle n'a pas tranché.

Usage :
    python3 outils/marquer_voix.py                 # toutes les cases
    python3 outils/marquer_voix.py --session 73
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

# Coupes validées à l'écoute
# Elles passent DEVANT la découpe automatique, parce que c'est lui qui entend
# et que la machine ne sait pas distinguer un cri ou un bruit de fond d'une
# autre voix.
COUPES = Path(config.WORK) / "coupes-manuelles.tsv"

ROUGE = "#c0392b"      # décidé : c'est le propriétaire
ORANGE = "#d68910"     # probable


def _iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")


COUPES_MANU: dict = {}


def _heure(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _coupes_manuelles() -> dict:
    """{heure iso du segment: texte qui est au propriétaire} — décisions du propriétaire."""
    out = {}
    if not COUPES.is_file():
        return out
    for ligne in COUPES.read_text(encoding="utf-8").splitlines():
        if not ligne.strip() or ligne.startswith("heure"):
            continue
        parts = ligne.split("\t")
        if len(parts) >= 2 and parts[1].strip():
            out[parts[0].strip()] = parts[1].strip()
    return out


def _repartir(texte: str, bornes: list[tuple[float, float]]) -> list[str]:
    """Répartit les mots d'une phrase sur ses morceaux, au prorata du temps.

    Partition STRICTE : chaque mot est rendu une seule fois, jamais dupliqué
    (c'est la règle qui évite de perdre des mots en fin de passage)."""
    mots = texte.split()
    if not mots or not bornes:
        return [texte]
    total = sum(max(0.0, f - d) for d, f in bornes) or 1.0
    out, i = [], 0
    for k, (d, f) in enumerate(bornes):
        if k == len(bornes) - 1:
            j = len(mots)
        else:
            j = i + int(round(len(mots) * max(0.0, f - d) / total))
        out.append(" ".join(mots[i:j]))
        i = j
    return out


def _ligne(t: float, texte: str, verdict: str) -> str:
    if verdict == "proprietaire":
        return f'<span style="color:{ROUGE}">{_heure(t)} — {texte}</span>'
    if verdict == "probable":
        return (f'<span style="color:{ORANGE}">{_heure(t)} — {texte}'
                f"</span>  <sub>(probable)</sub>")
    return f"{_heure(t)} — {texte}"


def marquer_case(con: sqlite3.Connection, t_debut: float,
                 base_profil=None) -> dict:
    import soundfile as sf

    chemin = blocs.chemin_case(t_debut)
    if not chemin.is_file():
        return {"case": blocs.nom_case(t_debut), "statut": "absente"}

    x, sr = sf.read(str(chemin), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    profil = ref.matrice() if base_profil is None else base_profil
    global COUPES_MANU
    if not COUPES_MANU:
        COUPES_MANU = _coupes_manuelles()

    segs = con.execute(
        "SELECT start_time, end_time, text FROM segment"
        " WHERE start_time >= ? AND start_time < ? AND TRIM(COALESCE(text,'')) <> ''"
        " ORDER BY start_time", (t_debut, t_debut + blocs.DUREE_CASE)).fetchall()

    lignes, toi, compte = [], [], {"proprietaire": 0, "probable": 0, "autre": 0}
    courtes = 0
    coupes = 0
    for s in segs:
        a = max(0.0, blocs.position_dans_case(t_debut, s["start_time"]))
        b = min(len(x) / sr, blocs.position_dans_case(t_debut, s["end_time"]))
        # ⚠ Une phrase de moins de 1,5 s (« Oui. », « D'accord. ») ne peut pas
        # donner une empreinte fiable. Sans contexte elle tombait en « contenu »
        # par défaut, donc TOUTES les interjections étaient perdues : jugées
        # sans un seul avis, faute d'une fenêtre assez longue.
        # On élargit donc autour, en restant dans la case.
        if b - a < config.MIN_EXTRAIT_EMPREINTE:
            courtes += 1
            manque = (config.MIN_EXTRAIT_EMPREINTE - (b - a)) / 2.0
            a = max(0.0, a - manque)
            b = min(len(x) / sr, b + manque)
        morceau = x[int(a * sr):int(b * sr)]
        # La voix peut changer AU MILIEU d'une phrase de Whisper (quelques
        # secondes du propriétaire, puis la voix du contenu écouté). On coupe
        # donc aux changements de voix et on répartit le texte au prorata de
        # la durée : approximatif au mot, mais on ne fait jamais dire à
        # quelqu'un ce qu'il n'a pas dit.
        # 1) une coupe décidée par le propriétaire prime sur tout (il entend, la machine non)
        coupe = COUPES_MANU.get(_iso(s["start_time"]))
        if coupe and coupe in s["text"]:
            avant, apres = s["text"].split(coupe, 1)
            avant = (avant + coupe).strip()
            for txt, v in ((avant, "proprietaire"), (apres.strip(), "autre")):
                if not txt:
                    continue
                compte[v] = compte.get(v, 0) + 1
                lignes.append(_ligne(s["start_time"], txt, v))
                if v == "proprietaire":
                    toi.append(_ligne(s["start_time"], txt, v))
            continue
        morceaux = ref.decouper(morceau, sr, profil)
        if not morceaux:
            morceaux = [{"debut": 0.0, "fin": round(b - a, 2),
                         "verdict": "autre", "score": 0.0}]
        if len(morceaux) > 1:
            coupes += 1
        textes = _repartir(s["text"], [(m["debut"], m["fin"]) for m in morceaux])
        for m, txt in zip(morceaux, textes):
            if not txt.strip():
                continue
            t_m = s["start_time"] + m["debut"]
            compte[m["verdict"]] = compte.get(m["verdict"], 0) + 1
            lignes.append(_ligne(t_m, txt, m["verdict"]))
            if m["verdict"] in ("proprietaire", "probable"):
                toi.append(_ligne(t_m, txt, m["verdict"]))

    nom = blocs.nom_case(t_debut).replace(".wav", "")
    entete = [
        f"# {nom}",
        "",
        f"Case de 5 minutes : **{_heure(t_debut)} → {_heure(t_debut + blocs.DUREE_CASE)}**  ",
        f"{len(segs)} phrases — "
        f'<span style="color:{ROUGE}">{compte["proprietaire"]} sûres</span>, '
        f'<span style="color:{ORANGE}">{compte["probable"]} probables</span>, '
        f'{compte["autre"]} contenu.',
        "",
        f'<span style="color:{ROUGE}">■</span> = ta voix (le programme en est sûr) · '
        f'<span style="color:{ORANGE}">■</span> = probable · noir = contenu',
        "",
        "---",
        "",
    ]
    (config.WORK_TRACE / f"{nom}.md").write_text(
        "\n".join(entete + lignes) + "\n", encoding="utf-8")

    entete_toi = [f"# {nom} — ce que TU dis", "",
                  f"{len(toi)} phrase(s) sur {len(segs)}.", "", "---", ""]
    (config.WORK_TRACE / f"{nom}_TOI.md").write_text(
        "\n".join(entete_toi + (toi or ["*(rien de reconnu comme toi)*"])) + "\n",
        encoding="utf-8")

    return {"case": nom, "phrases": len(segs), "coupees": coupes,
            "statut": "ok", **compte}


def marquer(con: sqlite3.Connection, session_id: int | None = None) -> dict:
    q = "SELECT start_time FROM bloc ORDER BY start_time"
    debuts = [r["start_time"] for r in con.execute(q)]
    profil = ref.matrice()
    out = [marquer_case(con, d, profil) for d in debuts]

    total = {"proprietaire": 0, "probable": 0, "autre": 0}
    for r in out:
        for k in total:
            total[k] += r.get(k, 0)

    # un seul fichier avec TOUTES ses phrases du jour, pour relecture rapide
    jour = datetime.date.today().isoformat()
    lignes = [f"# Ce que le propriétaire a dit — {jour}", "",
              f"{total['proprietaire']} phrases sûres, {total['probable']} probables.", "",
              "---", ""]
    for r in out:
        p = config.WORK_TRACE / f"{r['case']}_TOI.md"
        if p.is_file():
            corps = p.read_text(encoding="utf-8").split("---", 1)[-1].strip()
            if corps and "rien de reconnu" not in corps:
                lignes += [f"## {r['case']}", "", corps, ""]
    (config.WORK_TRACE / f"TOI-{jour}.md").write_text(
        "\n".join(lignes) + "\n", encoding="utf-8")

    return {"cases": out, **total}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=int)
    a = ap.parse_args()
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    r = marquer(con, a.session)
    for c in r["cases"]:
        print(f"  {c['case']} : {c.get('phrases', 0):3} phrases -> "
              f"{c.get('proprietaire', 0):3} toi, {c.get('probable', 0):3} probable, "
              f"{c.get('autre', 0):3} contenu")
    print(f"\ntotal : {r['proprietaire']} sûres · {r['probable']} probables · {r['autre']} contenu")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
