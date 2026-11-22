# Set up a staging server on Google Cloud (first-timer guide)

**What you get:** the whole app running on a small Google Cloud server at a web address like `https://34-12-56-78.sslip.io`, with HTTPS, that you can share with stakeholders.
**What it costs:** it uses your $300 free credit (about $50 a month for the server). Nothing is charged to your card unless you upgrade the account.
**What it shows:** the app runs in **dry-run mode**. Every screen and every approval works exactly like the real thing, but the story text, pictures, voices and videos are built-in samples, and nothing is sent to Anthropic, fal or ElevenLabs. So stakeholders can click through the workflow; they will not see real AI output until you get credit and follow `docs/PILOT_RUNBOOK.md`.

You need: a Google account, a credit or debit card (for sign-up verification only), a GitHub account that can read the repository, and about 40 minutes. You do **not** need to install anything on your computer: everything runs in the browser.

---

## Step 1. Create the Google Cloud account and project
1. Go to **https://console.cloud.google.com** and sign in with your Google account.
2. Click **Activate** (or "Try for free") at the top. Choose your country, accept the terms, add your card. Google checks the card but does **not** charge it. You get the $300 credit (valid 90 days).
3. At the top left, click the project drop-down → **New project**. Name it `microdrama-staging` → **Create**. Make sure it is the selected project (its name shows at the top).

## Step 2. Set a spending alarm (2 minutes, worth doing)
1. Menu (three lines, top left) → **Billing** → **Budgets & alerts** → **Create budget**.
2. Name `staging`, amount `$50`, keep the defaults for the alert levels, and make sure email notifications are on → **Finish**.

## Step 3. Open Cloud Shell
Click the **terminal icon** (`>_`, "Activate Cloud Shell") at the top right of the console. A terminal opens at the bottom of the page. Click **Authorize** if it asks. Everything below is typed into this terminal.

## Step 4. Create a GitHub access token (so the server can read the code)
1. On GitHub: your photo (top right) → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Name `microdrama-staging`, expiry 30 or 60 days, **Repository access: Only select repositories** → `microdrama`.
3. Under **Permissions → Repository permissions**, set **Contents: Read-only**. Click **Generate token** and **copy it now** (it is shown once). Keep it somewhere private. Never paste it into a chat or a file in the repository.

## Step 5. Create the server
In Cloud Shell, paste these three lines (it asks for the token as the password: paste it; nothing shows as you type):
```bash
git clone --branch claude/affectionate-goodall-r14vry https://github.com/mehrab17677/microdrama.git
cd microdrama
bash scripts/gcp_create_vm.sh
```
If you see `destination path 'microdrama' already exists`, you ran this before. Use `cd ~/microdrama && git pull` instead of the clone line. The script is safe to run again: anything it already created ("already exists") is reused, not an error.
If it says the Compute Engine API needs enabling, wait a minute and run the last line again. It creates a fixed IP address, a firewall rule for web traffic, and the server, then prints **your web address**, for example `https://34-12-56-78.sslip.io`. Write it down.
(To use another region or a bigger machine: `ZONE=europe-west1-b MACHINE=e2-standard-4 bash scripts/gcp_create_vm.sh`.)

## Step 6. Connect to the server and install the app
1. Still in Cloud Shell:
   ```bash
   gcloud compute ssh microdrama-staging --zone us-central1-a
   ```
   The first time it asks to create an SSH key: press Enter for no passphrase (twice). You are now typing on the server (the prompt changes to `yourname@microdrama-staging`).
2. On the server, run:
   ```bash
   sudo apt-get update && sudo apt-get install -y git
   git clone --branch claude/affectionate-goodall-r14vry https://github.com/mehrab17677/microdrama.git
   bash microdrama/scripts/vm_bootstrap.sh
   ```
   Paste the GitHub token when asked for the password. The script then installs Docker, builds the app (5 to 10 minutes the first time), creates the database, **asks you for the admin email, name and password** (choose a strong password and keep it), checks everything and starts the app. At the end it prints `preflight passed` and the web address.

## Step 7. Open the app
Visit the address from Step 5. The first visit can take up to a minute while the HTTPS certificate is issued. Sign in with the admin email and password. You should see the dashboard.

## Step 8. Invite stakeholders
1. Send them the address. They click **Register**, enter their email, name and password.
2. You (admin) open **Team** in the top bar and click **Approve** next to each person. The app allows 5 people in total including you.
3. To show the workflow: **New production** → choose a category → fill the brief (a photo of the male lead and one of the female lead, and a few look pictures, are needed) → start. In dry-run the story, look, voices and episodes appear within minutes; approve the gates as the dashboard's "Waiting for you" strip prompts. For an ad drama, upload a logo, a real product photo (at least 800 px) and fill the brand details.

---

## Everyday commands (run on the server after `gcloud compute ssh ...`)
| Want to | Command |
|---|---|
| See what is running | `cd ~/microdrama && sudo docker compose ps` |
| Read the logs | `sudo docker compose logs -f --tail=100 worker` (also `web`, `runner`, `caddy`) |
| Update to the latest code | `git pull && sudo docker compose build && sudo docker compose run --rm migrate && sudo docker compose up -d` |
| Restart everything | `sudo docker compose restart` |

**Save credit between demos:** in Cloud Shell, `gcloud compute instances stop microdrama-staging --zone us-central1-a`. A stopped server charges only for its disk and the reserved IP (a few dollars a month). Start it again with `gcloud compute instances start microdrama-staging --zone us-central1-a`; the app comes back by itself and keeps its data.

**Remove everything when you are done** (stops all charges):
```bash
gcloud compute instances delete microdrama-staging --zone us-central1-a
gcloud compute addresses delete microdrama-staging-ip --region us-central1
gcloud compute firewall-rules delete microdrama-staging-web
```
(Or delete the whole project from the console: IAM & Admin → Settings → Shut down.)

## If something goes wrong
- **The page does not load / certificate error:** wait two minutes, then `sudo docker compose logs caddy`. Check the firewall rule exists (`gcloud compute firewall-rules list`) and that you used the `https://` address with dashes, not dots, in the IP part.
- **"Connection refused" right after step 6:** `sudo docker compose ps`; if `web` is restarting, read `sudo docker compose logs web`.
- **The build is slow or runs out of disk:** the first build downloads about 2 GB; wait. `df -h` shows disk space; `sudo docker system prune -af` frees old images.
- **"Wrong email or password" / forgot the password:** on the server run `cd ~/microdrama && sudo docker compose run --rm web python -m scripts.reset_password --email you@example.com`. It asks for a new password twice (at least 8 characters; nothing shows as you type) and also lifts the 15-minute lockout that follows 5 wrong tries.
- **Bills:** Billing → Reports shows what the credit has paid for.

## When you get credit and budget
Follow `docs/DEPLOYMENT.md` (set the vendor keys and `DRY_RUN=0` in `.env`, run preflight) and then `docs/PILOT_RUNBOOK.md`. The same server can be used.
