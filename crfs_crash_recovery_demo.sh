
#!/usr/bin/env bash
set -u

IMAGE="${1:-demo.img}"

section() {
    echo
    echo "=========================================="
    echo "$1"
    echo "=========================================="
}

section "CRFS CRASH RECOVERY DEMO"
echo "Disk image: $IMAGE"
echo "WARNING: Formatting erases the selected image."

read -r -p 'Type YES to continue: ' answer
if [[ "$answer" != "YES" ]]; then
    echo "Demo cancelled."
    exit 1
fi

section "1. Prepare OLD DATA"

python -m src.cli "$IMAGE" --format --size 8M || exit 1
python -m src.cli "$IMAGE" -c "mkdir /lab" || exit 1
python -m src.cli "$IMAGE" -c "create /lab/data.txt" || exit 1
python -m src.cli "$IMAGE" -c "write /lab/data.txt OLD DATA" || exit 1
python -m src.cli "$IMAGE" -c "read /lab/data.txt" || exit 1

section "2. Crash after commit flush"

echo "Attempting the configured crash scenario..."
set +e
python -m src.crash_worker \
'{"phase":"crash","image":"'"$IMAGE"'","operation":"write_file","point":"AFTER_COMMIT_FLUSH"}'
status=$?
set -e

echo "Worker exit code: $status"

section "3. Inspect without recovery"

python -m src.cli "$IMAGE" --no-recovery \
    -c "read /lab/data.txt" || exit 1

python -m src.cli "$IMAGE" --no-recovery \
    -c "journal" || exit 1

section "4. Recover normally"

python -m src.cli "$IMAGE" \
    -c "read /lab/data.txt" || exit 1

python -m src.cli "$IMAGE" \
    -c "check" || exit 1

section "DEMO FINISHED"
echo "Explain how journal replay restores committed metadata."
