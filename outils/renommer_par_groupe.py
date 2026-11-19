#!/usr/bin/env python3
"""Renommer les WAV de groupe pour que le NUMERO DE GROUPE soit en tete.

Pourquoi :
(9, 108, 206…) mais les fichiers s'appellent `s2_AAAA-MM-JJ_HH-MM-SS_5ch.wav`.
Le numero de session `s2` n'a AUCUN rapport avec le numero de groupe 9 : on ne
peut pas se reperer. On met donc le groupe au debut :

    s2_AAAA-MM-JJ_HH-MM-SS_5ch.wav   ->   g9_s2_AAAA-MM-JJ_HH-MM-SS_5ch.wav

On GARDE la fin du nom d'origine (session + heure + taille) : le fichier reste
lisible meme si la base est perdue, et on peut revenir en arriere.

POINT CRITIQUE : le nom est stocke en base (`chunk_group.audio_path`). Renommer
le fichier sans mettre a jour la base casse le lien (transcription, diarisation,
empreintes vocales, poubelle lisent tous `audio_path`). Ce script fait LES DEUX,
dans une seule transaction.

Tout est reversible :
  - `--annuler` remet les noms d'origine depuis le plan ecrit sur disque ;
  - le plan est un TSV lisible (`work/renommage_groupes.tsv`).

Usage :
    python3 outils/renommer_par_groupe.py              # simulation (defaut)
    python3 outils/renommer_par_groupe.py --appliquer  # renomme vraiment
    python3 outils/renommer_par_groupe.py --annuler    # remet les noms d'origine
"""
import argparse
import sqlite3
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
BASE = RACINE / "database" / "memorybrain.sqlite"
PLAN = RACINE / "work" / "renommage_groupes.tsv"


def charger_lignes() -> list[tuple[int, Path, Path]]:
    """[(group_id, ancien, nouveau)] pour chaque groupe ayant un WAV."""
    con = sqlite3.connect(BASE)
    rows = con.execute(
        "SELECT group_id, audio_path FROM chunk_group"
        " WHERE audio_path IS NOT NULL ORDER BY group_id").fetchall()
    con.close()
    out = []
    for gid, ap in rows:
        ancien = Path(ap)
        nouveau = ancien.with_name(f"g{gid}_{ancien.name}")
        out.append((gid, ancien, nouveau))
    return out


def ecrire_plan(lignes) -> None:
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    with PLAN.open("w", encoding="utf-8") as f:
        f.write("groupe\tancien\tnouveau\n")
        for gid, a, n in lignes:
            f.write(f"{gid}\t{a}\t{n}\n")


def appliquer(lignes, forcer: bool = False) -> dict:
    """Renomme fichier + .txt voisin + met a jour la base. Transaction unique."""
    con = sqlite3.connect(BASE)
    faits = ignores = erreurs = 0
    for gid, ancien, nouveau in lignes:
        if ancien.name.startswith(f"g{gid}_"):
            ignores += 1
            continue
        if not ancien.is_file():
            print(f"  ! groupe {gid} : fichier absent ({ancien.name})")
            erreurs += 1
            continue
        if nouveau.exists() and not forcer:
            print(f"  ! groupe {gid} : cible deja presente ({nouveau.name})")
            erreurs += 1
            continue
        try:
            ancien.rename(nouveau)
        except OSError as e:
            print(f"  ! groupe {gid} : renommage impossible ({e})")
            erreurs += 1
            continue
        # le .txt voisin (texte de controle) suit le meme sort
        txt_a = ancien.with_suffix(".txt")
        if txt_a.is_file():
            try:
                txt_a.rename(nouveau.with_suffix(".txt"))
            except OSError:
                pass
        con.execute("UPDATE chunk_group SET audio_path=? WHERE group_id=?",
                    (str(nouveau), gid))
        faits += 1
    con.commit()
    con.close()
    return {"renommes": faits, "deja_ok": ignores, "erreurs": erreurs}


def annuler() -> dict:
    """Remet les noms d'origine depuis le plan."""
    if not PLAN.is_file():
        print(f"plan introuvable : {PLAN}")
        return {"erreurs": 1}
    con = sqlite3.connect(BASE)
    faits = erreurs = 0
    for ligne in PLAN.read_text(encoding="utf-8").splitlines()[1:]:
        gid_s, ancien_s, nouveau_s = ligne.split("\t")
        ancien, nouveau = Path(ancien_s), Path(nouveau_s)
        if not nouveau.is_file():
            if ancien.is_file():
                continue
            erreurs += 1
            continue
        try:
            nouveau.rename(ancien)
        except OSError:
            erreurs += 1
            continue
        txt_n = nouveau.with_suffix(".txt")
        if txt_n.is_file():
            try:
                txt_n.rename(ancien.with_suffix(".txt"))
            except OSError:
                pass
        con.execute("UPDATE chunk_group SET audio_path=? WHERE group_id=?",
                    (str(ancien), int(gid_s)))
        faits += 1
    con.commit()
    con.close()
    return {"annules": faits, "erreurs": erreurs}


def main() -> int:
    ap = argparse.ArgumentParser(description="groupe au debut du nom de fichier")
    ap.add_argument("--appliquer", action="store_true",
                    help="renomme vraiment (sinon simulation)")
    ap.add_argument("--annuler", action="store_true",
                    help="remet les noms d'origine")
    ap.add_argument("--forcer", action="store_true",
                    help="ecrase une cible deja presente")
    a = ap.parse_args()

    if not BASE.is_file():
        print(f"base absente : {BASE}", file=sys.stderr)
        return 1

    if a.annuler:
        r = annuler()
        print(f"annulation : {r.get('annules', 0)} fichiers remis, "
              f"{r.get('erreurs', 0)} erreurs")
        return 0

    lignes = charger_lignes()
    a_renommer = [l for l in lignes if not l[1].name.startswith(f"g{l[0]}_")]
    print(f"{len(lignes)} groupes en base, {len(a_renommer)} a renommer\n")

    if not a.appliquer:
        print("SIMULATION (rien n'est modifie). Apercu :")
        for gid, ancien, nouveau in a_renommer[:12]:
            print(f"  {ancien.name}  ->  {nouveau.name}")
        if len(a_renommer) > 12:
            print(f"  … et {len(a_renommer) - 12} autres")
        print(f"\npour appliquer : python3 {Path(__file__).name} --appliquer")
        return 0

    ecrire_plan(lignes)
    print(f"plan ecrit : {PLAN}")
    r = appliquer(lignes, forcer=a.forcer)
    print(f"\nrenommes : {r['renommes']}  deja ok : {r['deja_ok']}  "
          f"erreurs : {r['erreurs']}")
    print(f"pour revenir en arriere : python3 {Path(__file__).name} --annuler")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
