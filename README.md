# Conserve Mass Ops

Self-hosted ops app for the **Conserve Mass** corporation (EVE Online).
It tracks the corp's courier contracts, keeps a record of repeat customers, and sends you a Discord DM when work arrives.

- Single container: FastAPI with server-rendered pages, APScheduler and a discord.py bot.
- SQLite in `/config` (WAL mode, nightly online backups).
- Unraid-native: `PUID=99`/`PGID=100`, all state in `/mnt/user/appdata/conserve-mass`, host port **8484**.

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
   Also select the industry ones now so you don't have to edit the app later: `esi-industry.read_character_jobs.v1`, `esi-industry.read_corporation_jobs.v1`, `esi-skills.read_skills.v1`, `esi-skills.read_skillqueue.v1`, `esi-assets.read_assets.v1`, `esi-assets.read_corporation_assets.v1`, `esi-characters.read_blueprints.v1`, `esi-corporations.read_blueprints.v1`.
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
On Unraid you only need two files in `/mnt/user/appdata/conserve-mass-src`: `docker-compose.yml` and `update.sh`.

```bash
mkdir -p /mnt/user/appdata/conserve-mass-src && cd /mnt/user/appdata/conserve-mass-src
curl -fsSLO https://raw.githubusercontent.com/cmasiah/conserve-mass/main/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/cmasiah/conserve-mass/main/update.sh
bash update.sh          # pulls the image, starts it on the "connectfour" network
```
Using the Unraid Docker tab instead? Copy `unraid/conserve-mass.xml` to `/boot/config/plugins/dockerMan/templates-user/`.

### Nginx Proxy Manager
Add a Proxy Host with:
- Forward Hostname: `conserve-mass`
- Port: `8000`
- Scheme: `http`
- SSL: your cert, Force SSL on

NPM must be on the same `connectfour` network. Port 8484 on the host is only for direct LAN access.
If `BASE_URL` is `https://…`, the session cookie is HTTPS-only, so log in through the NPM hostname, not `http://ip:8484`.

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

### Logs tab
**Logs** in the top menu shows a live view of the app's logs: level filter, text search, expandable tracebacks and an error/warning count.
It holds the last 5,000 lines in memory. Full history is written to `/config/logs/conserve-mass.log` (rotated, 5 × 5 MB) and can be downloaded from the page.

## 5. Backups & updates
- A nightly backup runs at 04:17 to `/config/backups/` and is kept for `BACKUP_KEEP_DAYS`. You can also trigger one from the System page. CA Appdata Backup will pick these up too.
- To update: `bash /mnt/user/appdata/conserve-mass-src/update.sh`. It pulls the newest image and restarts. Database migrations run automatically on start.
- To roll back: set `image: ghcr.io/cmasiah/conserve-mass:<version>` in `docker-compose.yml`, then run `docker compose up -d`.

## 6. Development (optional, on a PC — not needed on Unraid)
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
CONFIG_DIR=./config pytest -q
mkdir -p config && CONFIG_DIR=./config alembic upgrade head
CONFIG_DIR=./config BASE_URL=http://localhost:8000 uvicorn app.main:app --reload
```

## 7. Roadmap: industry module (v0.2+)
Each feature plugs into the existing `characters`, `esi_tokens` and scheduler. Each is one new Alembic migration plus one sync service.

| Feature | ESI source | New tables |
|---|---|---|
| Industry job monitor (manufacturing, reactions, research, copying, invention) with "job finished" DMs | `/characters/{id}/industry/jobs`, `/corporations/{id}/industry/jobs` | `industry_jobs` |
| Slot availability per character (used vs max manufacturing, reaction and science slots) | `/characters/{id}/skills`: Mass Production / Adv. Mass Production, Mass Reactions / Adv. Mass Reactions, Laboratory Operation / Adv. Laboratory Operation | `character_skills` |
| Skill training status | `/characters/{id}/skillqueue` | `skill_queue` |
| Stockpile tracking with minimum-level targets and low-stock DMs | `/characters/{id}/assets`, `/corporations/{id}/assets` | `asset_snapshots`, `stockpile_targets` |
| Blueprint library | `/characters/{id}/blueprints`, `/corporations/{id}/blueprints` | `blueprints` |
| Item names, groups, materials | CCP static data export (SDE), imported into SQLite | `sde_types`, `sde_groups`, `sde_blueprint_materials` |

SQLite still fits this workload: one writer, and asset snapshots in the tens of thousands of rows.
