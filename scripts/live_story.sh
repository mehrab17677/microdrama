#!/usr/bin/env bash
# Run ON the server, from anywhere:   bash ~/microdrama/scripts/live_story.sh on     |     bash ~/microdrama/scripts/live_story.sh off
#
# "on"  : real Claude for the writing and planning roles (story, scripts, originality, shot lists, line tags), on a server that is
#         otherwise a dry run: images, voices, music, video, lip-sync, the image-judging checks and embeddings stay as samples.
#         It asks for your Anthropic key (hidden; stored only in ~/microdrama/.env, mode 600), keeps Opus as the director (the guide's
#         choice), uses Haiku for the "copy" role (now also the default everywhere), and plans a 6 to 8 episode series.
# "off" : back to a pure dry run and the guide's settings (45 to 50 episodes). The key stays in .env but is unused.
set -euo pipefail
MODE="${1:-}"
cd "${DIR:-$HOME/microdrama}"
[ -f .env ] || { echo "No .env here. Run scripts/vm_bootstrap.sh first."; exit 1; }
LIVE_ROLES="story,writer,originality,director,copy,product_sheet"

setenv() {   # setenv NAME VALUE  (value taken from the environment, never shown on a command line)
  NAME="$1" VALUE="$2" python3 - <<'PY'
import os, re, pathlib
p = pathlib.Path(".env"); text = p.read_text(); name, value = os.environ["NAME"], os.environ["VALUE"]
line = f"{name}={value}"
text = re.sub(rf"^{re.escape(name)}=.*$", lambda m: line, text, flags=re.M) if re.search(rf"^{re.escape(name)}=", text, flags=re.M) else text.rstrip("\n") + "\n" + line + "\n"
p.write_text(text)
PY
  chmod 600 .env
}
sql() { sudo docker compose exec -T db psql -U postgres -d microdrama -v ON_ERROR_STOP=1 -c "$1" >/dev/null; }

if [ "$MODE" = "on" ]; then
  read -rsp "Paste your Anthropic API key (nothing shows as you type), then press Enter: " KEY; echo
  [ -n "$KEY" ] || { echo "No key entered. Nothing changed."; exit 1; }
  setenv ANTHROPIC_API_KEY "$KEY"; unset KEY
  setenv LIVE_LLM_ROLES "$LIVE_ROLES"
  sql "update config set value = value || '{\"director\":\"anthropic:claude-opus-5-5\",\"copy\":\"anthropic:claude-haiku-5-5\"}'::jsonb where key = 'llm_roles'"
  sql "update config set value = '[6, 8]'::jsonb where key = 'episode_count_range'"
  echo "Live roles: $LIVE_ROLES   Director: Opus   Copy: Haiku   Episodes: 6 to 8"
elif [ "$MODE" = "off" ]; then
  setenv LIVE_LLM_ROLES ""
  sql "update config set value = value || '{\"director\":\"anthropic:claude-opus-5-5\",\"copy\":\"anthropic:claude-haiku-5-5\"}'::jsonb where key = 'llm_roles'"
  sql "update config set value = '[45, 50]'::jsonb where key = 'episode_count_range'"
  echo "Back to a pure dry run with the guide's settings."
else
  echo "Usage: bash scripts/live_story.sh on|off"; exit 1
fi

echo "Restarting the app so it reads the new settings..."
sudo docker compose up -d --force-recreate web worker runner poller retention caddy >/dev/null
sleep 8
sudo docker compose run --rm web python scripts/preflight.py 2>/dev/null | grep -v WARN
sudo docker compose ps --format 'table {{.Service}}\t{{.Status}}'
