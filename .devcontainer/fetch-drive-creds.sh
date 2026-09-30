#!/usr/bin/env bash
# Fetch Google Drive credentials from a short-lived public handoff URL and
# store them in .drive-creds.json (gitignored). The handoff file in Drive is
# deleted ("burned") immediately after it is read, so the public link only
# works for a few minutes during initial provisioning.
#
# Preferred long-term alternative: add a Codespaces secret named
# DRIVE_CREDS_JSON (repo → Settings → Secrets and variables → Codespaces) —
# the pipeline picks env credentials up first.

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CREDS_FILE="$REPO/.drive-creds.json"
URL_FILE="$REPO/pipeline/creds_url.txt"

if [ -n "${DRIVE_CREDS_JSON:-}" ]; then
  echo "[creds] DRIVE_CREDS_JSON env is set — nothing to fetch"
  exit 0
fi
if [ -s "$CREDS_FILE" ]; then
  echo "[creds] $CREDS_FILE already present"
  exit 0
fi
if [ ! -s "$URL_FILE" ]; then
  echo "[creds] no handoff URL configured (pipeline/creds_url.txt missing)"
  exit 1
fi

URL="$(head -n1 "$URL_FILE" | tr -d '[:space:]')"
echo "[creds] waiting for handoff file …"

for i in $(seq 1 90); do
  # re-read each attempt so a newly published handoff URL is picked up
  URL="$(head -n1 "$URL_FILE" | tr -d '[:space:]')"
  if curl -sfL --max-time 60 "$URL" -o "$CREDS_FILE.tmp" \
     && python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$CREDS_FILE.tmp" 2>/dev/null; then
    mv "$CREDS_FILE.tmp" "$CREDS_FILE"
    chmod 600 "$CREDS_FILE"
    echo "[creds] obtained ($CREDS_FILE, $(wc -c <"$CREDS_FILE") bytes)"
    # burn-after-reading: remove the handoff file from Drive
    python3 "$REPO/pipeline/burn_handoff.py" --creds "$CREDS_FILE" --url-file "$URL_FILE" \
      && echo "[creds] handoff file deleted from Drive" \
      || echo "[creds] WARNING: could not delete handoff file (will be removed manually)"
    exit 0
  fi
  sleep 20
done

echo "[creds] timed out after 30 min — no handoff file appeared"
exit 1
