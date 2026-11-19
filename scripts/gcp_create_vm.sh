#!/usr/bin/env bash
# Creates the staging VM on Google Cloud: a static IP, a firewall rule for web traffic, and an Ubuntu VM.
# Run it in Google Cloud Shell (the terminal icon at the top right of console.cloud.google.com), with your project selected.
#   bash gcp_create_vm.sh            (optional: ZONE=europe-west1-b MACHINE=e2-standard-4 NAME=my-name bash gcp_create_vm.sh)
set -euo pipefail

NAME="${NAME:-microdrama-staging}"
ZONE="${ZONE:-us-central1-a}"
MACHINE="${MACHINE:-e2-standard-2}"
REGION="${ZONE%-*}"
PROJECT="$(gcloud config get-value project 2>/dev/null)"
[ -n "$PROJECT" ] || { echo "No project is selected. Run: gcloud config set project YOUR_PROJECT_ID"; exit 1; }

echo "Project: $PROJECT   Zone: $ZONE   Machine: $MACHINE"
gcloud services enable compute.googleapis.com

# Safe to run again: each piece is created only if a list query does not already show it.
exists() { [ -n "$("$@" --format='value(name)' 2>/dev/null)" ]; }

if exists gcloud compute addresses list --filter="name=$NAME-ip AND region:$REGION"; then echo "IP address $NAME-ip already exists, reusing it"
else gcloud compute addresses create "$NAME-ip" --region "$REGION"; fi
IP="$(gcloud compute addresses describe "$NAME-ip" --region "$REGION" --format='get(address)')"

if exists gcloud compute firewall-rules list --filter="name=$NAME-web"; then echo "Firewall rule $NAME-web already exists, reusing it"
else gcloud compute firewall-rules create "$NAME-web" --direction=INGRESS --allow=tcp:80,tcp:443 --target-tags="$NAME" --description="Web traffic for $NAME"; fi

if exists gcloud compute instances list --filter="name=$NAME AND zone:$ZONE"; then echo "Server $NAME already exists, reusing it"
else gcloud compute instances create "$NAME" --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
    --boot-disk-size=40GB --boot-disk-type=pd-balanced --address="$IP" --tags="$NAME"; fi

HOST="$(echo "$IP" | tr . -).sslip.io"
cat <<MSG

Done. Your server:
  IP address : $IP
  Web address: https://$HOST     (sslip.io gives every IP address a free name, so you need no domain)

Next: connect to it with
  gcloud compute ssh $NAME --zone $ZONE
and, on the server, run the bootstrap script (see docs/GCP_STAGING_SETUP.md, step 6).
To stop paying for compute between demos:  gcloud compute instances stop $NAME --zone $ZONE
To start it again:                          gcloud compute instances start $NAME --zone $ZONE
MSG
