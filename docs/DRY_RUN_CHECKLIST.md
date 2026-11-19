# Dry-run checklist (staging server, one full rehearsal)

Address: `https://136-116-197-246.sslip.io`  ·  Admin: `mehrab17677@gmail.com`  ·  Dry-run = built-in sample media, nothing paid is called.

## 1. Gather these files (before you start)
- [ ] **One face photo**: one clear face, at least 512 px, you may use it (your own is safest). More than one face or no face is rejected.
- [ ] **Look pictures**: 3 or more images, or a description of 20+ words.
- [ ] **Library source**: any `.txt` file (the text does not matter in dry-run).
- [ ] **Ad demo**: a logo (PNG, at least 300 px) and a real product photo (at least 800 px).
- [ ] The admin password, and your GitHub token (for `git pull`).

## 2. Wake the server and shorten the run
In Cloud Shell (the `>_` icon):
- [ ] Start the server if it is stopped: `gcloud compute instances start microdrama-staging --zone us-central1-a`
- [ ] Connect: `gcloud compute ssh microdrama-staging --zone us-central1-a` (the prompt must say `microdrama-staging`)
- [ ] Update: `cd ~/microdrama && git pull && sudo docker compose build && sudo docker compose up -d`
- [ ] Make the run 5 episodes instead of 45:
  ```
  sudo docker compose exec db psql -U postgres -d microdrama -c "update config set value='5'::jsonb where key='dryrun_episode_count'; update config set value='[1,50]'::jsonb where key='episode_count_range';"
  ```
- [ ] `sudo docker compose ps` shows web, worker, runner, poller, db, caddy running.

## 3. Run it in the browser
- [ ] Sign in. The dashboard loads (the first visit can take a minute for the HTTPS certificate).
- [ ] **Library**: upload the `.txt` with genre "revenge romance". Wait for status *ready*.
- [ ] **New production → Standard drama**: pick the genre, 3 to 8 keywords, language English, face photo + role label (e.g. "female lead"), look pictures. Start. *(Try leaving one field empty first: every problem should be listed at once.)*
- [ ] **Gate 2, Story**: read the bible and beats. Optionally reject once with a note to see a redo, then approve.
- [ ] **Gate 3, Look**: check character sheets and voices. Approval must be refused while a voice is missing; then approve.
- [ ] **Gate 4, Pilot episodes** (3 episodes): play one, mark a range, send feedback such as `Episode 2 (00:05-00:10) the pacing is slow`; confirm it maps to shots; then approve.
- [ ] **Spend bar**: visible on the dashboard and the production page.
- [ ] **Gate 5, Final cut**: approve. **Delivery** page shows the master and the reports (CSV links work).
- [ ] **Ad placement drama**: New production → Ad placement. Fill brand, product, description (20+ words), printed text, placement types, upload logo (kind `logo`) and product photo (kind `product`, view `front`). Run the gates.
  - [ ] Final-cut approval is **refused** until each placement is approved on the page; approve them, then approve.
  - [ ] Placement report page and CSV open.
- [ ] **Team**: register a second email in a private window, approve it as admin, sign in as that person.

## 4. Afterwards
- [ ] Note anything confusing or broken (page, what you clicked, what you saw) and send it to me.
- [ ] Put the run back to the real settings if you will start a production for real:
  ```
  sudo docker compose exec db psql -U postgres -d microdrama -c "update config set value='45'::jsonb where key='dryrun_episode_count'; update config set value='[45,50]'::jsonb where key='episode_count_range';"
  ```
- [ ] Save credit: `gcloud compute instances stop microdrama-staging --zone us-central1-a`

## What to expect
Every episode shows the same sample clip and sample voice. You are checking the workflow, gates, checks and cost tracking, not the quality of the drama. Stuck? `sudo docker compose logs --tail=80 web worker runner`.
