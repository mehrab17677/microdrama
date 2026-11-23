#!/usr/bin/env bash
# Run ON the server. Adds or replaces one secret in ~/microdrama/.env without showing it (hidden prompt; never on a command line or in chat).
#   bash ~/microdrama/scripts/set_secret.sh FAL_KEY            (also: HIGGSFIELD_API_KEY as KEY_ID:KEY_SECRET, ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, OPENAI_API_KEY)
# The file stays readable by you only (mode 600). Afterwards the app is restarted so it reads the new value.
set -euo pipefail
NAME="${1:-}"
case "$NAME" in
  FAL_KEY|HIGGSFIELD_API_KEY|ANTHROPIC_API_KEY|ELEVENLABS_API_KEY|OPENAI_API_KEY) ;;
  *) echo "Usage: bash scripts/set_secret.sh FAL_KEY|HIGGSFIELD_API_KEY|ANTHROPIC_API_KEY|ELEVENLABS_API_KEY|OPENAI_API_KEY"; exit 1 ;;
esac
cd "${DIR:-$HOME/microdrama}"
[ -f .env ] || { echo "No .env here. Run scripts/vm_bootstrap.sh first."; exit 1; }
HINT=""; [ "$NAME" = "HIGGSFIELD_API_KEY" ] && HINT=" as KEY_ID:KEY_SECRET (the two parts joined by a colon)"
read -rsp "Paste the $NAME$HINT (nothing shows as you type), then press Enter: " VALUE; echo
[ -n "$VALUE" ] || { echo "Nothing entered. Nothing changed."; exit 1; }
NAME="$NAME" VALUE="$VALUE" python3 - <<'PY'
import os, re, pathlib
p = pathlib.Path(".env"); text = p.read_text(); name, value = os.environ["NAME"], os.environ["VALUE"].strip()
line = f"{name}={value}"
if re.search(rf"^{re.escape(name)}=", text, flags=re.M):
    text = re.sub(rf"^{re.escape(name)}=.*$", lambda m: line, text, flags=re.M)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
p.write_text(text)
PY
unset VALUE
chmod 600 .env
echo "$NAME saved in .env. Restarting the app so it reads it..."
sudo docker compose up -d --force-recreate web worker runner poller retention caddy >/dev/null
sleep 6
sudo docker compose ps --format 'table {{.Service}}\t{{.Status}}'
echo "Done. The server is still a dry run (DRY_RUN=1), so nothing is charged until you run the smoke test or set DRY_RUN=0 (docs/IMAGE_MODELS.md, docs/PILOT_RUNBOOK.md)."
