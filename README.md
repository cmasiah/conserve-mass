# Conserve Mass Ops

Self-hosted ops app for the **Conserve Mass** corporation (EVE Online).
It tracks the corp's courier contracts, keeps a record of repeat customers, and sends you a Discord DM when work arrives.

- Single container: FastAPI with server-rendered pages, APScheduler and a discord.py bot.
- SQLite in `/config` (WAL mode, nightly online backups).
- Unraid-native: `PUID=99`/`PGID=100`, all state in `/mnt/user/appdata/conserve-mass`, host port configurable (default **8000**).

---

## 1. One-time setup

### 1a. Unraid storage
Shares → `appdata` → **Primary storage: Cache**, **Secondary: None**.
Settings → Global Share Settings → **Permit exclusive shares: Yes**.
With that, `/mnt/user/appdata` bypasses the FUSE layer, which SQLite needs to be reliable.

### 1b. EVE developer application
1. Go to https://developers.eveonline.com/applications and create an application.
2. Set **Callback URL** to `https://<your-npm-host>/auth/callback`. It must match `BASE_URL` + `/auth/callback` exactly.
3. Select these scopes: `esi-contracts.read_corporation_contracts.v1` and `esi-universe.read_structures.v1`.
   Also select the industry ones now so you don't have to edit the app later: `esi-industry.read_character_jobs.v1`, `esi-industry.read_corporation_jobs.v1`, `esi-skills.read_skills.v1`, `esi-skills.read_skillqueue.v1`, `esi-assets.read_assets.v1`, `esi-assets.read_corporation_assets.v1`, `esi-characters.read_blueprints.v1`, `esi-corporations.read_blueprints.v1`, `esi-corporations.read_divisions.v1`.
4. Copy the Client ID and Secret.

> The app never needs to be reachable from the internet. SSO redirects your *browser* to the callback, so a LAN-only NPM hostname works.

### 1c. Discord bot
1. Go to https://discord.com/developers/applications → New Application → **Bot** → Reset Token and copy it.
2. In **General Information**, copy the **Application ID**. Then open this link with your ID filled in and add the bot to a private server you're in:
   `https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=0`
   The bot needs no server permissions; it only joins so it can DM you.
3. In Discord, turn on Settings → Advanced → **Developer Mode**. Right-click your name → **Copy User ID**.
4. Right-click the server icon → **Privacy Settings** → allow **Direct Messages**.

No privileged intents are required.

### 1d. Config (Unraid terminal, no Python needed)
```bash
mkdir -p /mnt/user/appdata/conserve-mass
curl -fsSL https://raw.githubusercontent.com/cmasiah/conserve-mass/main/.env.example \
  -o /mnt/user/appdata/conserve-mass/.env
chmod 600 /mnt/user/appdata/conserve-mass/.env

openssl rand -base64 48 | tr -d '\n'; echo                  # -> SESSION_SECRET
openssl rand -base64 32 | tr '+/' '-_'                        # -> TOKEN_ENCRYPTION_KEY (valid Fernet key)

nano /mnt/user/appdata/conserve-mass/.env                     # paste values, Ctrl+O, Enter, Ctrl+X
```

**Back up `TOKEN_ENCRYPTION_KEY`.** If it's lost, the stored ESI tokens can't be decrypted and every character has to log in again.

## 2. Run

The image is built by GitHub Actions and published as `ghcr.io/cmasiah/conserve-mass` (public; it contains code only, no secrets).
On Unraid you only need a few files in `/mnt/user/appdata/conserve-mass-src`. The public `docker-compose.yml` holds only generic defaults; your own values go in two **local** files that are never committed and never overwritten by `update.sh`:

| Local file | Holds | Template |
|---|---|---|
| `.env` (in this folder, not the app's `.env` in appdata) | `CMASS_PORT`, `TZ`, `CMASS_APPDATA`, `CMASS_IMAGE_TAG` (release channel) | `deploy.env.example` |
| `docker-compose.override.yml` | your reverse-proxy Docker network | `docker-compose.override.example.yml` |

```bash
mkdir -p /mnt/user/appdata/conserve-mass-src && cd /mnt/user/appdata/conserve-mass-src
RAW=https://raw.githubusercontent.com/cmasiah/conserve-mass/main
for f in docker-compose.yml update.sh deploy.env.example docker-compose.override.example.yml; do curl -fsSLO "$RAW/$f"; done
cp deploy.env.example .env                                         # set your port / timezone
cp docker-compose.override.example.yml docker-compose.override.yml # set your network name
nano .env; nano docker-compose.override.yml
bash update.sh
```
Using the Unraid Docker tab instead? Copy `unraid/conserve-mass.xml` to `/boot/config/plugins/dockerMan/templates-user/`.

The compose file sets Unraid labels, so the Docker tab shows the app's icon and a **WebUI** entry in the container's menu. To make WebUI open your proxy hostname instead of `IP:port`, set the `net.unraid.docker.webui` label in your `docker-compose.override.yml` (see the example file).

### Nginx Proxy Manager
Add a Proxy Host with:
- Forward Hostname: `conserve-mass`
- Port: `8000`
- Scheme: `http`
- SSL: your cert, Force SSL on

NPM must be on the network named in your `docker-compose.override.yml`. The host port (`CMASS_PORT`) is only for direct LAN access.
If `BASE_URL` is `https://…`, the session cookie is HTTPS-only, so log in through the NPM hostname, not `http://<server-ip>:<port>`.

## 3. First login
1. Open `BASE_URL` and click **Log in with EVE Online** as your CEO.
   With `ADMIN_CHARACTER_IDS` empty, the first character to log in becomes admin. After that, only admins (or the IDs you list) can log in.
2. Conserve Mass becomes the tracked corp, and your CEO becomes the contract poller.
3. Go to **System → Send test DM** to confirm Discord works.
4. Optional: **Characters → Link character** to add alts. Alts are stored for the upcoming character and industry features but can't log in.

## 4. How it works
- Every 60 seconds the scheduler checks whether ESI's cache for corp contracts (about 5 minutes) has expired. If it has, it fetches the list, using ETag and pagination.
- Only `courier` contracts **assigned to Conserve Mass** are stored. Every status change is written to `contract_events`.
- ESI only returns roughly the last 30 days of contracts, so this database is your permanent history.
- The Customers tab lists people with at least one **delivered** contract (toggle to show every issuer). Earnings and volume count delivered contracts only.
- Alerts go into an outbox table (`notifications`) with a unique constraint on (ref, kind). Restarts and Discord outages never lose an alert or send one twice.

| Alert | When |
|---|---|
| 🚚 New courier contract | A new, unexpired `outstanding` contract appears |
| ⏳ Expiring soon | An outstanding contract expires within `EXPIRY_WARNING_HOURS` |
| 📦 Accepted / ✅ Delivered | Status changes to `in_progress` / `finished` |
| ⏰ Delivery due | An accepted contract's deadline is within `EXPIRY_WARNING_HOURS` |
| ❌ Failed / 🚫 Cancelled / 🗑️ Deleted | Issuer or game state changed it |
| ⚠️ Token revoked | The refresh token was rejected, so you need to log in again |

Discord slash commands (owner only, work in DMs): `/contracts` and `/sync`.

### Spam & flood protection
- **Digests:** if more than `DIGEST_THRESHOLD` (3) alerts of one kind are waiting, you get one message ("🚚 37 new courier contracts", top 15 listed) instead of 37 pings. There are also at most 3 messages per 20 s and `DISCORD_MAX_MESSAGES_PER_HOUR` (30) per hour; extra alerts wait and get digested.
- **Auto-mute:** an issuer who posts more than `SPAM_CONTRACTS_PER_HOUR` (10) contracts within an hour (by EVE issue time) is muted automatically, their queued alerts are dropped, and you get **one** "possible spam" DM.
- **Mute / Block** on each customer's page. *Muted*: tracked, no pings. *Blocked*: no pings, hidden from the dashboard, no ESI lookups for their contracts. ESI can't reject contracts, so reject junk in-game.
- **ESI frugality:** the contract list is one call no matter how many contracts exist. Names are batched (one call per 1000 IDs); unknown locations are capped at `MAX_LOCATION_LOOKUPS_PER_SYNC` (25) per sync; IDs that can't be resolved are remembered for 24 h; the app pauses when a rate-limit bucket drops under 10%.

## Home
The landing page pulls together what needs a look right now, from data the app already has (it makes no extra ESI calls):
- **Needs attention:** auto-muted issuers, ESI logins to re-link, sync errors, Discord problems, available updates.
- **Hauling:** open and in-transit contracts, collateral at risk, ISK earned per week (last 8 weeks), next deadlines, top customers this month.
- **Industry:** free slots, jobs ready and finishing in the next 24 hours, characters with free slots (idle ones first), skill queues that are empty, paused or about to run out.

The previous dashboard is now **Hauling Services → Dashboard** (`/hauling`).

## Industry
The left menu's **Industry** section covers:

- **Slots & Skills.** Each character's manufacturing, science and reaction slots, how many are in use right now, and their Capital Ship Construction tier.
  - Slots = 1 base + basic skill level + advanced skill level, so both skills at V gives **11**.
  - Reactions also need the *Reactions* skill.
  - Capital Ship Construction tiers: I components · III most capitals · IV jump freighters & supercarriers · V titans.
- **Jobs.** Active and ready jobs, personal and corp, with progress and time remaining.
- **Blueprints.** Searchable library of personal blueprints and corp hangar blueprints, grouped by hangar division name. Shows original/copy, ME, TE and runs.

### Scopes and roles
Enable these scopes on your EVE developer application, then use **Characters → Link with industry scopes** for each character:

| Scope | Used for | Corp role needed |
|---|---|---|
| `esi-skills.read_skills.v1` | slot counts, capital tier | none |
| `esi-skills.read_skillqueue.v1` | skill queue status on Home | none |
| `esi-industry.read_character_jobs.v1` | personal jobs, slots in use | none |
| `esi-industry.read_corporation_jobs.v1` | corp jobs | **Factory Manager** |
| `esi-characters.read_blueprints.v1` | personal blueprints | none |
| `esi-corporations.read_blueprints.v1` | corp hangar blueprints | **Director** |
| `esi-corporations.read_divisions.v1` | hangar division names | **Director** |
| `esi-assets.read_assets.v1`, `esi-assets.read_corporation_assets.v1` | (reserved for stockpiles) | Director for corp |

Polling (in minutes, ESI's cache timers apply on top): skills `INDUSTRY_SKILLS_POLL_MINUTES` (60), jobs `INDUSTRY_JOBS_POLL_MINUTES` (5), blueprints `INDUSTRY_BLUEPRINTS_POLL_MINUTES` (60).

### Logs tab
**Logs** in the left menu (Admin) shows a live view of the app's logs: level filter, text search, expandable tracebacks and an error/warning count.
It holds the last 5,000 lines in memory. Full history is written to `/config/logs/conserve-mass.log` (rotated, 5 × 5 MB) and can be downloaded from the page.

## 5. Backups & updates
- A nightly backup runs at 04:17 to `/config/backups/` and is kept for `BACKUP_KEEP_DAYS`. You can also trigger one from the System page. CA Appdata Backup will pick these up too.
- To update: `bash /mnt/user/appdata/conserve-mass-src/update.sh`. It pulls the newest image and restarts. Database migrations run automatically on start.
- To roll back: set `CMASS_IMAGE_TAG=<version>` (e.g. `0.4.3`) in your local `.env`, then run `docker compose up -d`.

### Release channels: stable and beta
| Channel | `CMASS_IMAGE_TAG` | Built from | Gets |
|---|---|---|---|
| Stable | `latest` (default) | `main` branch | Tested releases |
| Beta | `beta` | `beta` branch | New features first, for live testing |

Switch by setting `CMASS_IMAGE_TAG` in your local `.env` (next to `docker-compose.yml`) and running `update.sh`. The app knows which channel it was built for: on beta, the System tab shows a **beta channel** badge, and every new commit on `beta` counts as an update, not just a version bump.

How changes flow:
1. New work goes to a `release-*` branch and a pull request into `beta`. GitHub runs the tests on the pull request; nothing is published.
2. Merging that pull request builds the `:beta` image. Beta installs offer the update.
3. Once it's been tested live, a pull request from `beta` into `main` ("Create a merge commit") publishes it as a stable release (`:latest` and `:<version>`).

### One-click updates from the app
The **System** tab shows the running version, the newest one on GitHub, recent changes, and an **Update now** button. An "update" badge appears in the menu when a new version is out, and you get a Discord DM once per new version.

The app never controls Docker itself (that would give a web page root access to the server). Instead, **Update now** drops a request file in `/config`, and a small host script picks it up:

1. Install **User Scripts** from Community Apps.
2. Settings → User Scripts → **Add New Script**, name it `conserve-mass-updater`.
3. Replace its contents with: `bash /mnt/user/appdata/conserve-mass-src/update-watcher.sh`
4. Schedule: **Custom** → `* * * * *` → Apply.

Within a minute the System tab shows the host updater as **active**. `update.sh` keeps `update-watcher.sh` up to date automatically.

## 6. Development (optional, on a PC — not needed on Unraid)
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
CONFIG_DIR=./config pytest -q
mkdir -p config && CONFIG_DIR=./config alembic upgrade head
CONFIG_DIR=./config BASE_URL=http://localhost:8000 uvicorn app.main:app --reload
```

## 7. Roadmap
Done: industry job tracker, slot availability, skill queue status, blueprint library, watched-job DMs.

| Next | ESI source | New tables |
|---|---|---|
| Stockpile tracking with minimum-level targets, low-stock DMs and shortfalls on Home | `/characters/{id}/assets`, `/corporations/{id}/assets` | `asset_snapshots`, `stockpile_targets` |
| Item names, groups, materials offline | CCP static data export (SDE), imported into SQLite | `sde_types`, `sde_groups`, `sde_blueprint_materials` |

SQLite still fits this workload: one writer, and asset snapshots in the tens of thousands of rows.
