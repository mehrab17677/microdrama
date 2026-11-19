#!/usr/bin/env bash
# Run ON the server (Ubuntu). Installs Docker, fetches the code, writes .env for a DRY-RUN staging system and starts everything.
#   curl -fsSL <raw url of this file> | bash        or     bash vm_bootstrap.sh
# Asks for your GitHub access token the first time (the repository is private). Safe to run again.
set -euo pipefail

REPO="${REPO:-https://github.com/mehrab17677/microdrama.git}"
BRANCH="${BRANCH:-claude/affectionate-goodall-r14vry}"
DIR="${DIR:-$HOME/microdrama}"

echo "== 1/6 Installing Docker =="
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker
sudo docker compose version >/dev/null

echo "== 2/6 Getting the code =="
sudo apt-get install -y -qq git >/dev/null
if [ ! -d "$DIR/.git" ]; then
  echo "When asked for a password, paste your GitHub token (it is not shown)."
  git clone --branch "$BRANCH" "$REPO" "$DIR"
else
  git -C "$DIR" fetch origin "$BRANCH" && git -C "$DIR" checkout "$BRANCH" && git -C "$DIR" pull --ff-only origin "$BRANCH"
fi
cd "$DIR"

echo "== 3/6 Writing .env (dry-run: nothing paid is ever called) =="
if [ ! -f .env ]; then
  IP="$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || curl -fsS https://api.ipify.org)"
  HOST="${PUBLIC_HOST:-$(echo "$IP" | tr . -).sslip.io}"
  cp .env.example .env
  sed -i "s#^DATABASE_URL=.*#DATABASE_URL=postgresql://postgres:postgres@db:5432/microdrama#" .env
  sed -i "s#^SESSION_SECRET=.*#SESSION_SECRET=$(openssl rand -hex 32)#" .env
  sed -i "s#^PUBLIC_BASE_URL=.*#PUBLIC_BASE_URL=https://$HOST#" .env
  sed -i "s#^PUBLIC_HOST=.*#PUBLIC_HOST=$HOST#" .env
  sed -i "s#^DRY_RUN=.*#DRY_RUN=1#" .env
  chmod 600 .env
  echo "Wrote .env for https://$HOST"
else
  echo ".env already exists, keeping it."
fi

echo "== 4/6 Building the image (a few minutes the first time) =="
sudo docker compose build

echo "== 5/6 Creating the database and the admin account =="
sudo docker compose run --rm migrate
if ! sudo docker compose run --rm -T web python - <<'PY' 2>/dev/null | grep -q admin
from app import db
print("admin" if db.fetch_one("select 1 from users where role = 'admin'") else "none")
PY
then
  read -rp "Admin email: " ADMIN_EMAIL
  read -rp "Admin name: " ADMIN_NAME
  sudo docker compose run --rm web python -m scripts.create_admin --email "$ADMIN_EMAIL" --name "$ADMIN_NAME"
fi

echo "== 6/6 Checking and starting =="
sudo docker compose run --rm web python scripts/preflight.py
sudo docker compose up -d
sleep 8
sudo docker compose ps
HOST="$(grep '^PUBLIC_HOST=' .env | cut -d= -f2)"
echo
echo "Open https://$HOST and sign in with the admin account."
echo "(The first visit can take up to a minute while Caddy gets the HTTPS certificate.)"
