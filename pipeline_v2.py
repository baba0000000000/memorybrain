"""Le pipeline V2 de bout en bout, une étape après l'autre.

Chaque étape est une fonction qui peut tourner SEULE, et se teste seule.
Le pipeline ne fait que les appeler dans l'ordre. Si une étape échoue, les suivantes ne sont
pas tentées et le motif est écrit — le pipeline ne s'arrête jamais sur une
erreur (règle 3), il continue sur le groupe suivant.

    python3 pipeline_v2.py --etape groupement    --n 3
    python3 pipeline_v2.py --etape transcription --groupes 1,2,3
    python3 pipeline_v2.py --etape tout          --n 3
    python3 pipeline_v2.py --status
    python3 pipeline_v2.py --verifier

L'ordre des étapes suit le graphe Canva memory brain :

    1. groupement    5 fichiers → 1 morceau
    2. transcription Whisper v3 large fast        (règle 6)
    3. diarisation   qui parle, et quand          (pyannote)
    4. profils       qui est-ce                   (profil 0 = le propriétaire)
    5. alignement    à qui appartient ce texte
    6. markdown      le livrable lisible           (règle 17)
    7. classification PERSONNEL / MEDIA / BUSINESS
    8. extraction    idées et TODO via le LLM
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import db
import os
import re

# Un segment sans une seule lettre (« ... », « ♪ ») n'est pas une phrase.
_MOT = re.compile(r"[0-9A-Za-zÀ-ÿ]")

from diarization import diarize
from grouping import group as gmod
# `identify` n'est PLUS importe : `attribuer_profils` creeait un profil
# neuf par voix sans jamais verifier si elle etait deja connue
# (des dizaines de profils, dont beaucoup vides). C'est `speakers.rattacher` qui fait le
# matching par empreinte, via le profil 0 = le propriétaire.
from speakers import align
from transcription import transcribe_group as tg

# Chaque étape : (nom, fonction). L'ordre EST le graphe. La
# classification passe AVANT l'extraction, parce que l'extraction ne
# produit rien pour un groupe MEDIA ou UNKNOWN.
ETAPES = ("groupement", "transcription", "voix",
          "alignement", "classification", "markdown",
          "extraction")


def _groupes(con, n: int | None = None, ids: str | None = None) -> list[int]:
    """Quels groupes traiter. `--groupes 1,2` pour cibler, sinon tous."""
    ids_out = []
    if ids:
        for x in ids.replace(" ", "").split(","):
            if not x:
                continue
            if "-" in x:
                a, b = x.split("-", 1)
                ids_out.extend(range(int(a), int(b) + 1))
            else:
                ids_out.append(int(x))
    if ids_out:
        return ids_out
    q = "SELECT group_id FROM chunk_group"
    if n:
        return [r["group_id"] for r in con.execute(
            f"SELECT group_id FROM chunk_group ORDER BY group_id LIMIT {int(n)}")]
    return [r["group_id"] for r in con.execute(q + " ORDER BY group_id")]


def _marquer(con, group_id: int, etape: str, statut: str,
             erreur: str | None = None) -> None:
    """Journalise l'avancement (règle 17 : la base est l'état technique)."""
    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"etape:{etape}:{group_id}", f"{statut}|{time.time():.0f}"))
    if erreur:
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"erreur:{etape}:{group_id}", erreur[:400]))
    con.commit()


def etape_groupement(con, n: int | None = None, **_kw) -> dict:
    """1. Regroupement par 5."""
    t0 = time.time()
    r = gmod.construire_tout(con, n_groupes=n, avec_audio=True)
    r["elapsed"] = round(time.time() - t0, 1)
    return r


def etape_transcription(con, groupes: list[int], **_kw) -> dict:
    """2. Transcription du groupe (Whisper v3 large fast)."""
    out = []
    for gid in groupes:
        r = tg.transcrire_groupe(con, gid)
        out.append(r)
        _marquer(con, gid, "transcription", r.get("statut", "?"), r.get("erreur"))
        print(f"    transcription groupe {gid} : {r.get('statut')} "
              f"rtf={r.get('rtf')} segments={r.get('ecrits', r.get('segments'))}",
              flush=True)
    return {"groupes": out}


def etape_diarisation(con, groupes: list[int], **_kw) -> dict:
    """3. Diarisation (pyannote)."""
    out = []
    for gid in groupes:
        r = diarize.diariser_groupe(con, gid)
        out.append(r)
        _marquer(con, gid, "diarisation", r.get("statut", "?"), r.get("erreur"))
        print(f"    diarisation groupe {gid} : {r.get('statut')} "
              f"voix={r.get('locuteurs')} tours={r.get('tours')} "
              f"rtf={r.get('rtf')}", flush=True)
    return {"groupes": out}


def _wav_du_groupe(con, group_id: int) -> str | None:
    """Chemin du WAV concaténé du groupe, ou None.

    ⚠ `chunk_group.audio_path` est un chemin absolu, écrit au moment de la
    création du groupe : il devient périmé dès que le projet est déplacé,
    `os.path.exists()` échoue et l'étape entière tombe en « WAV
    introuvable ». On résout donc par NOM DE FICHIER dans
    `config.WORK_GROUPES`, seule vérité qui survit à un déplacement.
    """
    r = con.execute(
        "SELECT audio_path FROM chunk_group WHERE group_id=?"
        " AND audio_path IS NOT NULL LIMIT 1", (group_id,)).fetchone()
    if not r:
        return None
    chemin = r[0]
    if os.path.exists(chemin):
        return chemin
    local = os.path.join(config.WORK_GROUPES, os.path.basename(chemin))
    return local if os.path.exists(local) else None


def _colonne_audio_kind(con) -> None:
    """Ajoute `segment.audio_kind` si la base date d'avant la musique."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(segment)")}
    if "audio_kind" not in cols:
        con.execute("ALTER TABLE segment ADD COLUMN audio_kind TEXT")
        con.commit()


def etape_voix(con, groupes: list[int], **_kw) -> dict:
    """3. QUI PARLE — et est-ce de la musique ? (remplace diarisation+profils)

    Deux étapes de la V2 disparaissent ici :

      * `diarisation` (pyannote) : coût CPU du même ordre que la durée de
        l'audio, pour le seul but de numéroter des voix dont on n'a rien à
        faire ;
      * `profils` (rattacher) : fabriquait un profil par voix inconnue
        (des dizaines de profils, un seul nommé) et transformait chaque tour en
        « MATCH » de sa propre invention.

    Ce qui les remplace, pour chaque segment déjà transcrit :

      1. empreinte par fenêtres de 2,5 s → verdict proprietaire / probable / autre,
         donc la voix du propriétaire est retrouvée même s'il parle par-dessus une
         vidéo (toutes les fenêtres quand sa voix domine, la majorité à
         niveau égal) ;
      2. score musique AudioSet par bloc de 10 s → un segment musical n'est
         pas transcrit, sauf si le propriétaire parle dessus ;
      3. reconstruction des `turn` (PROPRIETAIRE / AUTRE) pour que l'alignement, le
         markdown et l'extraction continuent de fonctionner sans y toucher.

    Aucun profil n'est créé. Les voix autres ne sont pas identifiées : elles
    sont du contenu, pas des interlocuteurs.
    """
    from classification import musique as mus
    from speakers import proprietaire as bx
    from transcription import hallucinations as hallu
    import soundfile as sf

    _colonne_audio_kind(con)
    out = []
    for gid in groupes:
        wav = _wav_du_groupe(con, gid)
        if not wav:
            _marquer(con, gid, "voix", "echec", "WAV du groupe introuvable")
            print(f"    voix groupe {gid} : WAV introuvable", flush=True)
            out.append({"groupe": gid, "statut": "echec"})
            continue

        x, sr = sf.read(wav, dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        base = con.execute(
            "SELECT MIN(c.start_time) FROM chunk_group g"
            " JOIN group_member m ON m.group_id=g.group_id"
            " JOIN chunk c ON c.chunk_id=m.chunk_id WHERE g.group_id=?",
            (gid,)).fetchone()[0]
        if base is None:
            _marquer(con, gid, "voix", "echec", "groupe sans chunk")
            out.append({"groupe": gid, "statut": "echec"})
            continue
        base = float(base)

        segs = con.execute(
            "SELECT s.segment_id, s.start_time, s.end_time, s.text"
            " FROM segment s JOIN group_member m ON m.chunk_id=s.chunk_id"
            " WHERE m.group_id=? ORDER BY s.start_time", (gid,)).fetchall()

        blocs: dict[int, dict] = {}

        def musique_du_bloc(t: float) -> dict:
            """Score musique du bloc de 10 s qui contient `t` (mis en cache)."""
            k = int(t // config.BLOC_MUSIQUE)
            if k not in blocs:
                a = int(k * config.BLOC_MUSIQUE * sr)
                b = min(len(x), a + int(config.BLOC_MUSIQUE * sr))
                blocs[k] = (mus.scores(x[a:b], sr) if b - a >= sr
                            else {"music": 0.0, "speech": 0.0, "singing": 0.0})
            return blocs[k]

        morceaux = []          # (debut, fin, verdict) pour construire les tours
        n_proprietaire = n_musique = n_vides = 0
        for s in segs:
            texte = (s["text"] or "").strip()
            # « ... », « ♪ », ou une phrase inventée par Whisper sur du
            # silence (« Sous-titrage ST' 501 ») : ce n'est pas de la
            # parole, on ne l'attribue pas et on ne l'affiche pas.
            if not _MOT.search(texte) or hallu.est_hallucination(texte):
                # `audio_kind` porte la raison de l'exclusion : « hallucination »
                # pour une phrase inventée, NULL pour un marqueur sans mot.
                con.execute("UPDATE segment SET speaker_id=NULL, audio_kind=?"
                            " WHERE segment_id=?",
                            ("hallucination" if hallu.est_hallucination(texte)
                             else None, s["segment_id"]))
                n_vides += 1
                continue
            d0 = s["start_time"] - base
            d1 = s["end_time"] - base
            if d1 <= 0 or d0 >= len(x) / sr:
                continue
            d0 = max(0.0, d0)
            d1 = min(len(x) / sr, d1)
            # Deux fenêtres, on garde la meilleure des deux :
            #   * SERRÉE — le segment et 0,25 s autour, allongé juste assez
            #     pour qu'une empreinte ait un sens (1,8 s). C'est elle qui
            #     récupère les interjections courtes (« D'accord », « Hop »)
            #     lancées par-dessus une vidéo ;
            #   * LARGE — 2,5 s centrées, qui donne du contexte quand le
            #     segment est trop pauvre en voix pour être jugé seul.
            # Prendre le maximum est un choix assumé : le propriétaire veut être
            # reconnu, et le risque inverse (attribuer au propriétaire une seconde
            # de média) ne coûte rien — la classification interdit
            # l'extraction d'idées dans un groupe média.
            verdict = None
            for pad in (max(0.25, (1.8 - (d1 - d0)) / 2),
                        max(0.5, (config.FENETRE_VOIX - (d1 - d0)) / 2)):
                a = int(max(0.0, d0 - pad) * sr)
                b = int(min(len(x) / sr, d1 + pad) * sr)
                v = bx.analyser(x[a:b], sr, con)
                if verdict is None or (v["part"], v["score"]) > \
                        (verdict["part"], verdict["score"]):
                    verdict = v
            speaker_id, confiance = bx.attribut(verdict)
            parle = verdict["verdict"] in ("proprietaire", "probable")

            p = musique_du_bloc(0.5 * (d0 + d1))
            musicale = (not parle) and p["music"] >= config.SEUIL_MUSIQUE \
                and p["music"] > p["speech"]

            con.execute("UPDATE segment SET speaker_id=?, audio_kind=? WHERE segment_id=?",
                        (speaker_id, "musique" if musicale else "parole",
                         s["segment_id"]))
            if musicale:
                n_musique += 1
                continue
            morceaux.append({"d": s["start_time"], "f": s["end_time"],
                             "v": verdict["verdict"], "sid": s["segment_id"],
                             "dur": d1 - d0})

        # ---- consensus de voisinage ----
        # Une interjection de moins de 1,8 s coincée entre deux segments de
        # du propriétaire est du propriétaire : c'est l'interjection au milieu d'une
        # dictée. L'empreinte seule ne peut pas trancher sur si peu de
        # signal ; le contexte, si. On ne le fait QUE si les deux voisins
        # sont à lui — jamais à côté d'un segment de contenu.
        for i in range(1, len(morceaux) - 1):
            m = morceaux[i]
            if (m["v"] == "autre" and m["dur"] < 1.8
                    and morceaux[i - 1]["v"] in ("proprietaire", "probable")
                    and morceaux[i + 1]["v"] in ("proprietaire", "probable")):
                m["v"] = "proprietaire"
                con.execute("UPDATE segment SET speaker_id=?, audio_kind='parole'"
                            " WHERE segment_id=?",
                            (config.PROFIL_PROPRIETAIRE, m["sid"]))
        n_proprietaire = sum(1 for m in morceaux if m["v"] in ("proprietaire", "probable"))

        # ---- reconstruction des tours (PROPRIETAIRE / AUTRE) ----
        tours = []
        for m in morceaux:
            etiquette = "PROPRIETAIRE" if m["v"] in ("proprietaire", "probable") else "AUTRE"
            if tours and tours[-1][2] == etiquette and m["d"] - tours[-1][1] <= 2.0:
                tours[-1][1] = max(tours[-1][1], m["f"])
            else:
                tours.append([m["d"], m["f"], etiquette])
        con.execute("DELETE FROM turn WHERE group_id=?", (gid,))
        for i, (debut, fin, etiquette) in enumerate(tours):
            if etiquette == "PROPRIETAIRE":
                sid, conf = config.PROFIL_PROPRIETAIRE, "MATCH"
            else:
                sid, conf = None, "UNKNOWN"
            con.execute(
                "INSERT INTO turn(group_id, idx, start_time, end_time,"
                " label_local, speaker_id, confiance) VALUES(?,?,?,?,?,?,?)",
                (gid, i, debut, fin, etiquette, sid, conf))

        part_musique = n_musique / len(segs) if segs else 0.0
        if segs and part_musique >= config.PART_MUSIQUE_GROUPE and n_proprietaire == 0:
            con.execute("UPDATE chunk_group SET statut='musique',"
                        " note=COALESCE(note,'')||? WHERE group_id=?",
                        (f" | musique {part_musique:.0%}", gid))
        con.commit()

        r = {"groupe": gid, "segments": len(segs), "proprietaire": n_proprietaire,
             "musique": n_musique, "vides": n_vides, "tours": len(tours),
             "part_musique": round(part_musique, 3)}
        _marquer(con, gid, "voix", "ok")
        print(f"    voix groupe {gid} : {n_proprietaire}/{len(segs)} segments le propriétaire, "
              f"{n_musique} musique, {n_vides} vides, {len(tours)} tours",
              flush=True)
        out.append(r)
    return {"groupes": out}



def etape_profils(con, groupes: list[int], **_kw) -> dict:
    """4. Profils vocaux. Profil 0 = le propriétaire, existe avant tout le reste.

    Elle utilise `rattacher`, PAS `identify`. `identify.attribuer_profils`
    cree un profil NEUF pour chaque voix, sans jamais verifier si la
    voix est deja connue : c'est exactement ce qui a produit des profils en série
    (dont beaucoup de vides) sur une poignée de groupes.

    `rattacher` fait ce qu'on attend :
      - profil 0 = le propriétaire, N empreintes du dossier de référence privé ;
      - cosinus contre tous les profils connus ;
        >= SEUIL_MATCH -> on reutilise le profil (MATCH)
        >= SEUIL_PROBABLE -> PROBABLE, l'utilisateur tranche
        sinon -> NOUVEAU, un seul profil cree pour cette voix ;
      - une voix qui parle moins de MIN_SECONDE_PAR_PROFIL (8 s) ne
        cree RIEN : elle reste non attribuee plutot que de fabriquer
        une identite.
    """
    from speakers import rattacher
    out = []
    for gid in groupes:
        wav = _wav_du_groupe(con, gid)
        if not wav or not os.path.exists(wav):
            _marquer(con, gid, "profils", "echec", "WAV introuvable")
            print(f"    profils groupe {gid} : WAV introuvable, "
                  f"aucun profil cree", flush=True)
            out.append({"groupe": gid, "voix": [],
                        "message": "WAV introuvable"})
            continue
        r = rattacher.rattacher_au_repertoire(con, gid, wav, trace=False)
        _marquer(con, gid, "profils", "ok")
        for v in r.get("voix", {}).values() if isinstance(r.get("voix"), dict) \
                else []:
            print(f"    profils groupe {gid} : {v.get('label')} -> "
                  f"profil {v.get('profil')} {v.get('statut')}", flush=True)
        out.append(r)
    return {"groupes": out}


def etape_alignement(con, groupes: list[int], **_kw) -> dict:
    """5. Alignement texte ↔ voix."""
    out = []
    for gid in groupes:
        r = align.aligner_groupe(con, gid)
        out.append({k: v for k, v in r.items() if k != "interventions"})
        _marquer(con, gid, "alignement", "ok")
        print(f"    alignement groupe {gid} : {r.get('couverts')}/{r.get('segments')}"
              f" segments attribués, voix={r.get('voix')}", flush=True)
    return {"groupes": out}


def etape_markdown(con, groupes: list[int], **_kw) -> dict:
    """6. Le livrable lisible (règle 17)."""
    noms = {r["speaker_id"]: r["name"] for r in con.execute(
        "SELECT speaker_id, name FROM speaker WHERE name IS NOT NULL")}
    out = []
    for gid in groupes:
        r = align.aligner_groupe(con, gid)
        g = con.execute("SELECT * FROM chunk_group WHERE group_id=?",
                        (gid,)).fetchone()
        chemin = config.WORK_TRACE / f"groupe_{gid:04d}_transcript.md"
        md = _markdown(g, r["interventions"], noms)
        chemin.write_text(md, encoding="utf-8")
        out.append({"group_id": gid, "chemin": str(chemin),
                    "interventions": len(r["interventions"])})
        _marquer(con, gid, "markdown", "ok")
        print(f"    markdown groupe {gid} : {len(r['interventions'])} "
              f"interventions -> {chemin.name}", flush=True)
    return {"groupes": out}


def _markdown(g, interventions: list[dict], noms: dict) -> str:
    """Le transcript final d'un groupe, en Markdown."""
    from datetime import datetime
    lignes = [
        f"# Transcript — groupe {g['group_id']}",
        "",
        f"- **Session** : {g['session_id']}",
        f"- **Début** : {datetime.fromtimestamp(g['start_time']):%Y-%m-%d %H:%M:%S}"
        if g["start_time"] else "- **Début** : inconnu",
        f"- **Durée** : {g['duration_sec']} s",
        f"- **Chunks regroupés** : {g['n_chunks']} × {g['taille']}",
        f"- **Modèle** : {config.MODELE_ASR_NOM} + empreinte le propriétaire"
        f" (wespeaker-resnet34) + musique ({config.MODELE_MUSIQUE})",
        "",
        "---",
        "",
    ]
    if not interventions:
        lignes.append("*Aucune parole détectée dans ce groupe.*")
    else:
        lignes.append(align.en_markdown(interventions, noms))
    lignes.append("")
    return "\n".join(lignes)


def etape_classification(con, groupes: list[int], **_kw) -> dict:
    """7. Classification PERSONNEL / MEDIA / BUSINESS (déterministe)."""
    from classification import classify
    out = []
    for gid in groupes:
        r = classify.classer_groupe(con, gid)
        out.append(r)
        _marquer(con, gid, "classification", "ok")
        print(f"    classification groupe {gid} : {r['classe']} "
              f"({r['motif']}, {r['mots']} mots)", flush=True)
    return {"groupes": out}


def etape_extraction(con, groupes: list[int], **_kw) -> dict:
    """8. Extraction d'idées et TODO par le LLM local.

    Ne produit RIEN pour un groupe MEDIA ou UNKNOWN : on ne veut pas
    remplir la mémoire du propriétaire avec ce qu'un YouTubeur a dit. C'est le
    garde-fou le plus important de cette étape.
    """
    from extraction import extract
    out = []
    for gid in groupes:
        r = extract.extraire_groupe(con, gid)
        out.append({k: v for k, v in r.items()})
        _marquer(con, gid, "extraction", r.get("statut", "?"), r.get("erreur"))
        print(f"    extraction groupe {gid} : {r.get('statut')} "
              f"{r.get('ecrits', 0)} item(s), "
              f"{r.get('refuses_invention', 0)} refusés", flush=True)
    return {"groupes": out}


RUNNERS = {
    "groupement": etape_groupement,
    "transcription": etape_transcription,
    "voix": etape_voix,
    "alignement": etape_alignement,
    "classification": etape_classification,
    "markdown": etape_markdown,
    "extraction": etape_extraction,
}


def tout(con, n: int | None = None, forcer: bool = False) -> dict:
    """Enchaîne TOUTES les étapes sur les mêmes groupes, dans l'ordre.

    C'est le test « la chaîne complète marche ».
    groupes sont les MÊMES d'une étape à l'autre : c'est ce qui prouve que
    les étapes s'enchaînent et ne perdent rien entre elles.

    Depuis, chaque étape passe son CONTRÔLE avant que la suivante ne
    démarre ; si un contrôle échoue, la chaîne s'arrête. `forcer=True` (CLI `--force`)
    continue malgré un contrôle rouge, pour diagnostic.
    """
    from verification import controles
    resultats: dict = {}
    r = etape_groupement(con, n=n)
    resultats["groupement"] = r
    groupes = _groupes(con, n=n)
    print(f"  → {len(groupes)} groupe(s) à traiter : {groupes[:10]}", flush=True)

    ctrl = controles.controler(con, "groupement", groupes)
    controles.afficher(ctrl)
    resultats["controle:groupement"] = ctrl
    if not ctrl["ok"] and not forcer:
        print("  ⛔ étape « groupement » : contrôle en échec, chaîne arrêtée.")
        return resultats

    for nom in ETAPES[1:]:
        if nom not in RUNNERS:      # branche de secours (inutilisée : tous
            print(f"\n  == {nom} ==", flush=True)     # les runners ont un contrôle)
            resultats[nom] = RUNNERS[nom](con, groupes)
            continue
        print(f"\n  == {nom} ==", flush=True)
        resultats[nom] = RUNNERS[nom](con, groupes)
        ctrl = controles.controler(con, nom, groupes)
        controles.afficher(ctrl)
        resultats[f"controle:{nom}"] = ctrl
        if not ctrl["ok"] and not forcer:
            print(f"  ⛔ étape « {nom} » : contrôle en échec, chaîne arrêtée.")
            break
    return resultats


def verifier(con, groupes: list[int] | None = None) -> dict:
    """Les contrôles de cohérence. C'est ce qu'on lance à la fin.

    Un pipeline « qui marche » ne veut pas dire « qui ne se trompe pas ».
    Ces contrôles sont la différence entre les deux.
    """
    groupes = groupes or [r["group_id"] for r in
                          con.execute("SELECT group_id FROM chunk_group")]
    problemes: list[str] = []

    for gid in groupes:
        v = gmod.verifier_membres(con, gid)
        if not v.get("ok"):
            problemes.append(f"groupe {gid} : membres incorrects {v}")

        seg = con.execute(
            "SELECT COUNT(*) FROM segment s JOIN group_member m"
            " ON m.chunk_id=s.chunk_id WHERE m.group_id=?", (gid,)).fetchone()[0]
        hors = con.execute(
            "SELECT COUNT(*) FROM segment s JOIN chunk c ON c.chunk_id=s.chunk_id"
            " JOIN group_member m ON m.chunk_id=s.chunk_id"
            " WHERE m.group_id=? AND (s.start_time < c.start_time-0.05"
            " OR s.end_time > c.end_time+0.05)", (gid,)).fetchone()[0]
        if hors:
            problemes.append(f"groupe {gid} : {hors} segments hors de leur chunk")

        tours = con.execute("SELECT COUNT(*) FROM turn WHERE group_id=?",
                            (gid,)).fetchone()[0]
        attribues = con.execute(
            "SELECT COUNT(*) FROM turn WHERE group_id=? AND speaker_id IS NOT NULL",
            (gid,)).fetchone()[0]
        if tours and attribues == 0:
            problemes.append(f"groupe {gid} : {tours} tours, aucun profil attribué")

        print(f"  groupe {gid:4} : {seg:4} segments, {tours:4} tours, "
              f"{attribues:4} tours avec profil"
              + ("" if v.get("ok") else "  <-- PROBLEME"), flush=True)

    print()
    if problemes:
        print(f"  {len(problemes)} PROBLEME(S) :")
        for p in problemes:
            print(f"    - {p}")
    else:
        print("  Aucun problème de cohérence détecté.")
    return {"problemes": problemes, "ok": not problemes}


def status(con) -> None:
    print("=== V2 pipeline ===")
    print(f"  base          : {config.DB_PATH}")
    print(f"  modèle ASR    : {config.MODELE_ASR_NOM} ({config.MODELE_ASR})")
    print(f"  diarisation   : {config.MODELE_DIARISATION}")
    print(f"  groupe        : {config.GROUPE_TAILLE} fichiers")
    print(f"  profil 0      : le propriétaire")
    print()
    for t in ("chunk", "chunk_group", "group_member", "segment", "turn",
              "speaker", "classification", "extraction"):
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception as e:
            n = f"erreur {e}"
        print(f"  {t:16} : {n}")
    print()
    st = con.execute(
        "SELECT statut, COUNT(*) c FROM chunk_group GROUP BY statut").fetchall()
    if st:
        print("  groupes par statut :")
        for r in st:
            print(f"    {r['statut']:12} {r['c']}")
    print()
    sp = con.execute("SELECT speaker_id, name, status FROM speaker"
                     " ORDER BY speaker_id").fetchall()
    if sp:
        print("  profils vocaux :")
        for r in sp:
            n = r["name"] or "(sans nom)"
            print(f"    {r['speaker_id']:3} {n:14} {r['status']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Pipeline MemoryBrain V2")
    ap.add_argument("--etape", choices=list(RUNNERS) + ["tout"],
                    help="une seule étape, ou toute la chaîne")
    ap.add_argument("--n", type=int, help="nombre de groupes")
    ap.add_argument("--groupes", help="liste d'ids, ex 1,2,3")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verifier", action="store_true")
    ap.add_argument("--verifier-etape", choices=list(RUNNERS) + ["tout"],
                    help="contrôle d'UNE étape (ou de toutes) avant la suite")
    ap.add_argument("--force", action="store_true",
                    help="continuer malgré un contrôle rouge (diagnostic)")
    args = ap.parse_args()

    config.ensure_dirs()
    con = db.connect(config.DB_PATH)

    if args.status:
        status(con)
        con.close()
        return 0

    if args.verifier:
        verifier(con)
        con.close()
        return 0

    if args.verifier_etape:
        from verification import controles
        etapes = (controles.ETAPES if args.verifier_etape == "tout"
                  else [args.verifier_etape])
        ok = True
        for e in etapes:
            c = controles.controler(con, e)
            controles.afficher(c)
            ok = ok and c["ok"]
        con.close()
        return 0 if ok else 1

    if args.etape == "tout":
        tout(con, n=args.n, forcer=args.force)
        verifier(con)
        con.close()
        return 0

    if args.etape == "groupement":
        print(etape_groupement(con, n=args.n))
    else:
        groupes = _groupes(con, n=args.n, ids=args.groupes)
        if not groupes:
            print("  aucun groupe : lance d'abord --etape groupement")
        else:
            print(RUNNERS[args.etape](con, groupes))

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
