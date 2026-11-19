#!/usr/bin/env bash
# Lance N workers V2 sur des groupes distincts.
#
# Adapter le nombre de workers au nombre de cœurs disponibles : chaque
# worker charge son propre modèle en mémoire.
#
# Usage :
#   ./lancer_2_workers.sh          # toute la chaîne, tous les groupes
#   ./lancer_2_workers.sh 10       # les 10 premiers groupes
#   ./lancer_2_workers.sh 10 3     # 10 groupes, 3 workers
#
# Chaque worker a son PROPRE base SQLite. C'est obligatoire : SQLite
# n'accepte qu'un seul écrivain à la fois, et deux workers sur la même
# base donnent « database is locked » au bout de quelques secondes.
# On a vu exactement ça pendant la mise au point.

set -u

V2="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-$(dirname "$0")/.venv/bin/python}"
N_GROUPES="${1:-9999}"
N_WORKERS="${2:-2}"
export HF_TOKEN="${HF_TOKEN:-}"   # jeton Hugging Face si le diariseur pyannote est utilisé

BASE="$V2/database/memorybrain.sqlite"
LOGS="$V2/work/logs"
mkdir -p "$LOGS"

echo "=== MemoryBrain V2 — $N_WORKERS workers, $N_GROUPES groupes ==="
echo "base   : $BASE"
echo "logs   : $LOGS"
echo

# Le regroupement est rapide (ffmpeg) : on le fait UNE FOIS dans le
# processus principal, pour que tous les workers voient les mêmes groupes.
echo "[préparation] regroupement par ${GROUPE_TAILLE:-5}..."
"$PY" - <<'PREP'
import sys
sys.path.insert(0, ".")
import config, db
from grouping import group as gmod
config.ensure_dirs()
con = db.connect(config.DB_PATH)
print("   groupes déjà faits :",
      con.execute("SELECT COUNT(*) FROM chunk_group").fetchone()[0])
PREP

PIDS=()
for i in $(seq 1 "$N_WORKERS"); do
    # ⚠ Répartition SANS COORDINATION, via le modulo — c'est ce qui évite
    # que deux workers prennent le même groupe. Un worker i ne traite que
    # les groupes dont group_id % N_WORKERS == i.
    #
    # ⚠ Chaque worker a sa PROPRE base SQLite. SQLite n'accepte qu'un
    # seul écrivain : deux workers sur la même base donnent « database is
    # locked » au bout de quelques secondes. La copie est faite depuis le
    # processus principal, AVANT le
    # démarrage des workers, donc les deux base sont identiques au départ.
    COPIE="$V2/database/worker_$i.sqlite"
    cp "$BASE" "$COPIE"

    echo "[worker $i] base $COPIE (groupes $i, $((i + N_WORKERS)), ...)"
    (
        cd "$V2" || exit 1
        MB_WORKER=$i \
        MB_TOTAL_WORKERS="$N_WORKERS" \
        MB_GROUPES="$N_GROUPES" \
        "$PY" -u worker_v2.py > "$LOGS/worker_$i.log" 2>&1
        echo "[worker $i] terminé (code $?)"
    ) &
    PIDS+=($!)
    # Petite pause : éviter deux chargements de modèle simultanés (mémoire).
    sleep 3
done

echo
echo "=== en cours : ${#PIDS[@]} workers ==="
echo "suivre avec :  tail -f $LOGS/worker_1.log"
echo

ATTENDRE=1
for p in "${PIDS[@]}"; do
    wait "$p" || ATTENDRE=0
done

echo
if [ "$ATTENDRE" = "1" ]; then
    echo "=== tous les workers ont fini ==="
else
    echo "=== au moins un worker a échoué — voir les logs ==="
fi

# Contrôle de cohérence final, une fois tout le monde terminé (base
# accessible sans conflit).
echo
echo "=== vérification ==="
cd "$V2" && "$PY" pipeline_v2.py --verifier