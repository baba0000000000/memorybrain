"""ÉTAPE 8 — EXTRACTION : que reste-t-il à retenir ?

Un transcript de 25 minutes n'est pas une mémoire. Il faut en tirer :
  - une IDÉE   : une pensée, une décision, un projet ;
  - une TODO   : quelque chose à faire ;
  - une NOTE   : un fait, un nom, une date, un lieu.

Le modèle est défini dans `config.py` et se change en une ligne. Il est servi
localement (`chocolatine-2-4b`), sans coût d'API ; s'il n'est pas là,
l'extraction est SKIPPÉE et notée comme telle — elle ne bloque rien et ne fait
rien rater (règle 3). On n'invente jamais une idée à la place du LLM.
"""
import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone

import config

try:                                   # module frère, selon le point d'entrée
    from extraction import marqueurs
except ImportError:                    # lancé depuis le dossier extraction/
    import marqueurs

# Le prompt est court et fermé : on ne demande pas un roman, on demande
# une liste. Un LLM 4B demandé trop-large produit de la prose inutile.
#
# ⚠ Le prompt ne contient QUE l'instruction et le transcript : aucun élément
# de contexte sur la personne enregistrée. Un modèle à qui on décrit
# quelqu'un recopie la description mot pour mot dans les notes, sans qu'elle
# ait le moindre rôle dans l'extraction. Tout fait personnel doit donc venir
# du transcript, jamais du prompt.
PROMPT = """Tu analyses un extrait de la vie quotidienne du propriétaire.

Voici un transcript avec les interlocuteurs. Produis UNIQUEMENT un JSON, sans aucun texte autour, avec cette forme :

{"idees": ["..."], "todos": ["..."], "notes": ["..."]}

Règles :
- "idees" : les pensées, décisions ou projets du propriétaire (1 phrase max chacune)
- "todos" : ce qu'il doit faire, avec le verbe d'action
- "notes" : les faits à retenir (noms, dates, lieux, montants, numéros)
- Si une catégorie n'a rien, mets une liste vide.
- N'invente JAMAIS : n'écris aucun fait qui ne soit pas dit dans le
  transcript ci-dessous. Ce qui n'apparaît pas dans le transcript n'existe
  pas pour ce travail.
- Cite le début du passage dans chaque item, entre guillemets, quand c'est possible.

TRANSCRIPT :
"""

# Prompt des MARQUEURS (étage 1). Les passages sont déjà délimités à la voix
# (« début de l'idée » … « fin de l'idée », ou « rajoute ça à la to-do »).
# Le LLM ne choisit donc plus quoi garder : il VÉRIFIE que le passage est bien
# une idée ou une tâche, et le REFORMULE.
PROMPT_MARQUEURS = """Le propriétaire a DÉLIMITÉ lui-même les passages ci-dessous, en disant « début de
l'idée » / « fin de l'idée », ou en demandant de le mettre dans sa to-do.

Vérifie que chaque passage est bien ce que le propriétaire annonce, et reformule-le
en UNE phrase claire.

Réponds UNIQUEMENT un JSON, sans aucun texte autour :

{"items": [{"type": "idea", "texte": "..."}]}

Règles :
- "idea" pour un passage délimité par « début de l'idée » et « fin de l'idée ».
- "todo" pour une demande d'ajout à la to-do : reformule avec le verbe d'action.
- Si un passage n'est PAS une idée ni une tâche (le propriétaire se ravise, il lit un
  texte, c'est du bruit), NE LE METS PAS dans la réponse.
- ATTENTION : si le propriétaire DÉCRIT un mécanisme ou parle DU système (par exemple
  « s'il y a une nouvelle tâche, je crée la commande »), ce n'est PAS une
  tâche. Une tâche est quelque chose qu'il demande de FAIRE.
- N'invente RIEN : le contenu doit venir du passage.
- Si aucun passage n'est valable : {"items": []}

PASSAGES :
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _modeles_servis(corps: str) -> set:
    """Noms de modèles exposés par une réponse /v1/models ou /api/tags."""
    try:
        d = json.loads(corps)
    except Exception:
        return set()
    noms = set()
    for entree in (d.get("data") or []) + (d.get("models") or []):
        if isinstance(entree, dict):
            for cle in ("id", "name", "model"):
                v = entree.get(cle)
                if isinstance(v, str):
                    noms.add(v)
    return noms


def serveur_disponible(port: int | None = None, timeout: float = 3.0,
                       alias: str | None = None) -> bool:
    """Le serveur LLM répond-il, ET sert-il bien le modèle attendu ?

    ⚠ `/api/tags` est un endpoint **ollama**. llama-server (notre
    chocolatine, sur `config.PORT_LLM`) expose `/health` et `/v1/models`,
    et renvoie 404 sur `/api/tags` : le test disait donc « serveur absent »
    alors qu'il répondait parfaitement. L'extraction retombait alors
    silencieusement sur le modèle de repli, et le modèle attendu ne servait
    jamais.

    ⚠ Deuxième piège : `GET /v1/models` répond 200
    pour N'IMPORTE QUEL serveur llama.cpp. Un autre modèle resté sur le
    même port faisait donc passer le test, et l'extraction décodait avec
    le mauvais modèle SANS le signaler. Quand `alias` est fourni, on
    n'accepte que le serveur qui expose réellement ce nom de modèle
    (vérifié dans `data[].id` / `models[].name`). Sans alias, on retombe
    sur la simple joignabilité (`/health`).
    """
    import urllib.error
    import urllib.request
    port = port or config.PORT_LLM
    for chemin in ("/v1/models", "/api/tags"):
        try:
            with urllib.request.urlopen(f"http://localhost:{port}{chemin}",
                                        timeout=timeout) as r:
                if r.status != 200:
                    continue
                noms = _modeles_servis(r.read().decode())
                if alias is None or alias in noms:
                    return True
        except urllib.error.HTTPError:
            continue      # 404 = mauvais endpoint, on essaie le suivant
        except Exception:
            continue
    if alias is None:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health",
                                        timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False
    return False


def appeler_llm(prompt: str, port: int | None = None,
                max_tokens: int = 1200) -> dict:
    """Appelle le LLM local. Ne lève jamais (règle 3).

    Tente chaque entrée de config.LLM_REPLI dans l'ordre. Chocolatine
    d'abord : c'est le modèle demandé par le propriétaire, servi par le LLM
    local, et il est nettement meilleur en français que les alternatives.
    qwen3 reste en repli si le serveur est éteint.
    """
    erreurs = []
    for modele, p, api in config.LLM_REPLI:
        if not serveur_disponible(p, alias=modele):
            erreurs.append(f"{modele}:{p} absent")
            continue
        r = (_post_ollama if api == "ollama" else _post_openai)(p, modele,
                                                                  prompt,
                                                                  max_tokens)
        if "texte" in r and r["texte"].strip():
            r["modele"] = modele
            return r
        erreurs.append(f"{modele}:{p} {r.get('erreur', 'réponse vide')}")
    return {"erreur": " | ".join(erreurs)[:300]}


def _post_ollama(port: int, modele: str, prompt: str,
                 max_tokens: int) -> dict:
    """API native d'ollama (`/api/chat`).

    ⚠ L'endpoint `/v1/chat/completions` d'ollama ne répond pas ici (timeout
    sous charge CPU). Seule l'API native marche, et c'est elle qui
    accepte `"think": false` — indispensable pour qwen3, qui sinon
    consomme 1200 tokens à raisonner et n'émet jamais le JSON.
    """
    corps = json.dumps({
        "model": modele,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1, "num_predict": max_tokens},
    }).encode()

    req = urllib.request.Request(
        f"http://localhost:{port}/api/chat", data=corps,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode())
        return {"texte": data.get("message", {}).get("content") or "",
                "raisonnement": False,
                "finish": data.get("done_reason")}
    except urllib.error.HTTPError as e:
        return {"erreur": f"HTTP {e.code}"}
    except Exception as e:
        return {"erreur": str(e)[:200]}


def _post_openai(port: int, modele: str, prompt: str, max_tokens: int) -> dict:
    """Format OpenAI, pour litellm ou un vLLM local éventuel."""
    corps = json.dumps({
        "model": modele,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()

    req = urllib.request.Request(
        f"http://localhost:{port}/v1/chat/completions",
        data=corps, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode())
        msg = data["choices"][0]["message"]
        texte = msg.get("content") or ""
        raisonnement = msg.get("reasoning_content") or ""
        if not texte and raisonnement:
            texte = raisonnement
        return {"texte": texte,
                "raisonnement": bool(raisonnement),
                "finish": data["choices"][0].get("finish_reason")}
    except urllib.error.HTTPError as e:
        return {"erreur": f"HTTP {e.code}"}
    except Exception as e:
        return {"erreur": str(e)[:200]}


def parse_json(texte: str) -> dict:
    """Extrait le JSON d'une réponse de LLM, même mal formée.

    Un modèle 4B produit souvent du JSON entouré de texte, ou avec des
    virgules traînantes. On tente plusieurs reprises, de la plus stricte
    à la plus tolérante. Si rien ne marche, on renvoie du vide : une
    extraction ratée est un groupe sans idée, pas une panne.
    """
    texte = (texte or "").strip()
    if not texte:
        return {}

    # 1. le bloc entre les premières et dernières accolades
    i, j = texte.find("{"), texte.rfind("}")
    candidats = []
    if i >= 0 and j > i:
        candidats.append(texte[i:j + 1])

    # 2. le JSON entier
    candidats.append(texte)

    for c in candidats:
        for essai in (c, c.replace(",}", "}").replace(",]", "]")):
            try:
                d = json.loads(essai)
                if isinstance(d, dict):
                    return d
            except Exception:
                continue
    return {}


def transcript_texte(con: sqlite3.Connection, group_id: int,
                     limite: int = 4000) -> str:
    """Le transcript du groupe, en texte, prêt pour le LLM."""
    from speakers import align
    r = align.aligner_groupe(con, group_id)
    noms = {r["speaker_id"]: r["name"] for r in con.execute(
        "SELECT speaker_id, name FROM speaker WHERE name IS NOT NULL")}
    if not noms:
        noms = {0: "le propriétaire"}
    md = align.en_markdown(r["interventions"], noms)
    return md[:limite]


def controler_ancrage(texte: str, item: str) -> bool:
    """L'idée est-elle vraiment dans le transcript ?

    On compare les mots longs (> 5 lettres) de l'idée avec ceux du
    transcript. Si moins de la moitié sont présents, l'idée est probablement
    une invention du modèle et on la refuse.
    """
    mots_idee = {m.lower() for m in item.split()
                 if len(m) > 5 and m.isalpha()}
    if not mots_idee:
        return True          # idée trop courte pour être ancrable
    mots_texte = {m.lower() for m in texte.split() if m.isalpha()}
    presents = mots_idee & mots_texte
    return len(presents) >= len(mots_idee) * 0.5


def _charge_marqueurs(blocs: dict) -> str:
    """Met les passages délimités par le propriétaire en forme pour le LLM."""
    lignes = []
    for b in blocs["idees"]:
        lignes.append("[passage délimité comme une idée]\\n" + b["texte"])
    for b in blocs["todos"]:
        lignes.append("[passage délimité comme une tâche à ajouter]\\n" + b["texte"])
    return "\n\n".join(lignes)


def _kind(type_llm) -> str | None:
    """Traduit le type renvoyé par le LLM en ENUM de la base."""
    t = (type_llm or "").strip().lower()
    if t in ("idea", "idée", "idee", "thought"):
        return "IDEA"
    if t in ("todo", "tâche", "tache", "task"):
        return "TODO"
    return None


def _vider_items(con: sqlite3.Connection, group_id: int) -> None:
    """Un groupe qui ne doit PAS produire d'items ne doit pas en garder.

    Sans ça, changer une règle puis relancer laisse les anciens items en
    base : un faux « TODO » survivait après le retrait du marqueur fautif. Un contrôle lit la base, pas
    l'intention : il faut vraiment nettoyer.
    """
    con.execute("DELETE FROM extraction WHERE group_id=?", (group_id,))
    con.commit()


def extraire_groupe(con: sqlite3.Connection, group_id: int,
                    verifier_ancrage: bool = True) -> dict:
    """Extrait idées / todos / notes d'un groupe.

    Refuse de produire quoi que ce soit pour un groupe MEDIA ou UNKNOWN :
    c'est le garde-fou n°1 et le plus important.
    """
    classe = con.execute(
        "SELECT classe FROM classification WHERE group_id=?",
        (group_id,)).fetchone()
    if classe is None:
        _vider_items(con, group_id)
        return {"statut": "non_classé", "group_id": group_id}

    texte = transcript_texte(con, group_id)
    if not texte.strip():
        _vider_items(con, group_id)
        return {"statut": "vide", "group_id": group_id}

    # ÉTAGE 1 — les marqueurs oraux (« début de l'idée », to-do).
    # ⚠ Règle : le marqueur PRIME sur la classification. Un passage
    #   est UNKNOWN. Sinon ses idées se perdent dès que le vocabulaire sort
    #   des catégories prévues : une vraie tâche dictée passait à la trappe
    #   parce qu'aucun mot-clé ne la rattachait à une catégorie.
    blocs = marqueurs.extraire(texte)
    a_marqueurs = bool(blocs["idees"] or blocs["todos"])

    if not a_marqueurs and classe["classe"] not in ("PERSONNEL", "BUSINESS"):
        _vider_items(con, group_id)
        return {"statut": "ignoré", "group_id": group_id,
                "classe": classe["classe"],
                "detail": "seuls PERSONNEL et BUSINESS produisent des idées"}

    if not serveur_disponible(alias=config.MODELE_LLM):
        return {"statut": "llm_indisponible", "group_id": group_id,
                "port": config.PORT_LLM}

    # Le LLM reprend parfois nos marqueurs de locuteur dans sa réponse
    # (« Profil_3 parle de son travail »). C'est un défaut de notre mise en
    # forme, pas une idée du locuteur : on retire ces étiquettes du texte
    # AVANT l'ancrage, sinon l'item passe le contrôle en citant notre
    # propre sortie au lieu de la source. Sans ce nettoyage, une partie des
    # items sort avec l'étiquette de profil en tête.
    from speakers.align import _sans_profil
    texte_propre = _sans_profil(texte)

    if a_marqueurs:
        # délimité : le LLM vérifie le contenu et le reformule.
        r = appeler_llm(PROMPT_MARQUEURS + _charge_marqueurs(blocs))
        origine = "marqueur"
    else:
        r = appeler_llm(PROMPT + texte)
        origine = None
    if r.get("erreur"):
        return {"statut": "échec_llm", "group_id": group_id,
                "erreur": r["erreur"]}

    data = parse_json(r.get("texte"))
    if not data:
        return {"statut": "json_illisible", "group_id": group_id,
                "brut": (r.get("texte") or "")[:300]}

    if a_marqueurs:
        items = [(_kind(it.get("type")), it.get("texte"))
                 for it in (data.get("items") or [])
                 if isinstance(it, dict)]
    else:
        items = []
        for kind, cle in (("IDEA", "idees"), ("TODO", "todos"),
                          ("NOTE", "notes")):
            for it in (data.get(cle) or []):
                items.append((kind, it))

    # Idempotence (corrigé) : relancer l'étape ne doit pas
    # DUPLIQUER les items — c'est exactement ce qui arrivait avec --force,
    # et ça faussait tout comptage. On repart de zéro pour ce groupe.
    con.execute("DELETE FROM extraction WHERE group_id=?", (group_id,))

    refuses = 0
    ecrits = 0
    for kind, item in items:
        if kind is None:
            continue
        item = _sans_profil((item or "").strip())
        if len(item) < 8:
            continue
        if verifier_ancrage and not controler_ancrage(texte_propre, item):
            refuses += 1
            continue
        con.execute(
            "INSERT INTO extraction(group_id, kind, texte, chemin,"
            " speaker_id, llm, created_at) VALUES(?,?,?,?,?,?,?)",
            (group_id, kind, item, origine, config.PROFIL_PROPRIETAIRE,
             r.get("modele"), now_iso()))
        ecrits += 1
    con.commit()
    return {"statut": "ok", "group_id": group_id, "ecrits": ecrits,
            "refuses_invention": refuses, "classe": classe["classe"],
            "origine": origine, "modele": r.get("modele")}


def extraire_tout(con: sqlite3.Connection, limite: int | None = None) -> dict:
    """Extraction sur tous les groupes éligibles."""
    q = ("SELECT DISTINCT g.group_id FROM chunk_group g"
         " JOIN classification c ON c.group_id = g.group_id"
         " WHERE c.classe IN ('PERSONNEL','BUSINESS') ORDER BY g.group_id")
    if limite:
        q += f" LIMIT {int(limite)}"
    r = con.execute(q).fetchall()
    compte: dict = {}
    total = 0
    for row in r:
        res = extraire_groupe(con, row["group_id"])
        compte[res["statut"]] = compte.get(res["statut"], 0) + 1
        total += res.get("ecrits", 0)
    return {"par_statut": compte, "items_ecrits": total, "groupes": len(r)}
