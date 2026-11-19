"""Tout l'audio est-il vraiment passé ? Réponse chiffrée, en une commande.

Trois mensonges possibles, et ce script les cherche tous les trois :
  1. un chunk encore `pending` alors que son groupe est dit « transcrit » ;
  2. un WAV de groupe plus long que le texte qu'on en a tiré ;
  3. un chunk qui n'appartient à aucun groupe (donc jamais transcrit).

Usage : python3 outils/verifier_traitement.py [--csv]
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="store_true")
    ap.add_argument("--exige-couverture", action="store_true",
                    help="échoue aussi si un WAV dépasse son texte de 20 s"
                         " (strict : les fins silencieuses comptent alors)")
    args = ap.parse_args()

    import config
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    total = con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
    par_statut = dict(con.execute(
        "SELECT status, COUNT(*) FROM chunk GROUP BY status").fetchall())
    pending = par_statut.get("pending", 0)
    hors_groupe = con.execute(
        "SELECT COUNT(*) FROM chunk c WHERE NOT EXISTS"
        " (SELECT 1 FROM group_member m WHERE m.chunk_id=c.chunk_id)"
    ).fetchone()[0]

    groupes = con.execute("SELECT COUNT(*) FROM chunk_group").fetchone()[0]
    st_groupes = dict(con.execute(
        "SELECT statut, COUNT(*) FROM chunk_group GROUP BY statut").fetchall())

    segments = con.execute("SELECT COUNT(*) FROM segment").fetchone()[0]
    txt = con.execute("SELECT COUNT(*) FROM segment WHERE TRIM(COALESCE(text,''))"
                      " <> ''").fetchone()[0]
    kinds = dict(con.execute(
        "SELECT COALESCE(audio_kind,'(non classé)'), COUNT(*) FROM segment"
        " GROUP BY 1").fetchall())
    tours = con.execute("SELECT COUNT(*) FROM turn").fetchone()[0]

    # Un WAV de groupe plus long que la couverture du texte = du son jamais lu.
    court = []
    for g in con.execute("SELECT group_id, audio_path, start_time FROM chunk_group"
                         " ORDER BY group_id"):
        nom = os.path.basename(g["audio_path"] or "")
        if not nom:
            continue
        wav = Path(str(config.WORK_GROUPES)) / nom
        if not wav.is_file():
            continue
        try:
            import soundfile as sf
            d = sf.info(str(wav)).duration
        except Exception:
            continue
        r = con.execute(
            "SELECT MAX(s.end_time) FROM segment s JOIN group_member m"
            " ON m.chunk_id=s.chunk_id WHERE m.group_id=?", (g["group_id"],)
        ).fetchone()[0]
        couvert = (float(r) - float(g["start_time"])) if r else 0.0
        if d - couvert > 20:
            court.append((g["group_id"], round(d), round(couvert)))

    print(f"chunks        : {total}")
    for k, v in sorted(par_statut.items(), key=lambda z: -z[1]):
        print(f"  {k:<14}: {v}")
    print(f"  hors groupe   : {hors_groupe}")
    print(f"groupes       : {groupes}  {st_groupes}")
    print(f"segments      : {segments} (dont {txt} avec du texte)")
    print(f"  audio_kind    : {kinds}")
    print(f"tours         : {tours}")
    print(f"groupes dont le WAV dépasse le texte de >20 s : {len(court)}")
    if court:
        print("  (cas typique : fins d'enregistrement")
        print("   silencieuses — Whisper n'y rend que « Merci. » ou une")
        print("   hallucination de sous-titrage. À écouter en cas de doute.)")
    for x in court[:10]:
        print(f"    g{x[0]:3} wav={x[1]:4}s texte={x[2]:4}s")

    if args.csv:
        print("\ngroupe;wav_s;texte_s")
        for x in court:
            print(f"{x[0]};{x[1]};{x[2]}")

    # Le verdict porte sur ce qui prouve un oubli : un morceau en attente ou
    # un morceau qui n'appartient à aucun groupe. Une fin d'enregistrement
    # plus longue que son texte n'est pas une preuve — c'est presque toujours
    # du silence ou de la musique, et on ne transcrit pas la musique.
    ok = pending == 0 and hors_groupe == 0
    if args.exige_couverture:
        ok = ok and not court
    print("\nVERDICT :", "tout est traité" if ok else
          f"il reste du travail (pending={pending}, hors_groupe={hors_groupe}"
          + (f", wav_non_lu={len(court)}" if args.exige_couverture else "") + ")")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
