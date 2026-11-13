# Deployment Guide — Test vs Stable

This project supports two fully separate deployments:

| | **Stable** | **Test** |
|---|---|---|
| Git branch | `main` | `test` |
| Discord bot | Official bot application | Separate test bot application |
| Token | `DISCORD_TOKEN` (stable) | `DISCORD_TOKEN` (test) |
| Database | Production Turso DB | Separate test Turso DB |
| Hosting | `dummydreams-stable` | `dummydreams-test` |
| `BOT_ENV` | `stable` | `test` |
| `EXPECTED_GUILD_ID` | Official server ID | Test server ID |
| `DEBUG_MODE` | `0` | `1` (optional) |

Never share a token, database, or hosting process between the two.

---

## 1. Configuration matrix

Every variable the code reads. Fill each column with the value for that
deployment.

| Variable | Stable | Test | Required | Notes |
|---|---|---|---|---|
| `DISCORD_TOKEN` | stable bot token | test bot token | yes | Different Discord applications |
| `TURSO_DATABASE_URL` | `libsql://dummydreams-stable-...` | `libsql://dummydreams-test-...` | yes (prod) | Different databases |
| `TURSO_AUTH_TOKEN` | stable DB token | test DB token | yes (prod) | Rotate if leaked |
| `BOT_ENV` | `stable` | `test` | recommended | `test` labels the presence with `TEST` |
| `APP_ENV` | — | — | no | Alias for `BOT_ENV`; `BOT_ENV` wins |
| `EXPECTED_GUILD_ID` | official guild ID | test guild ID | recommended | Logs a warning if the bot joins the wrong guild |
| `ADMIN_USERNAME` | strong username | strong username | recommended | Enables HTTP Basic Auth on `/admin` |
| `ADMIN_PASSWORD` | strong password | strong password | recommended | Must be set together with the username |
| `ANNOUNCEMENT_CHANNEL_ID` | official channel | test channel | optional | Fallback only; admin panel setting wins |
| `VERIFIED_ROLE_ID` | official role | test role | optional | Fallback only; admin panel setting wins |
| `API_SECRET_KEY` | random string | random string | optional | Protects `POST /api/announcement` |
| `SIGN_API_KEY` / `TIKTOK_SIGN_API_KEY` | provider key | provider key | optional | TikTokLive signing key |
| `PORT` | set by host | set by host | no | Render/Replit inject it |
| `DEBUG_MODE` | `0` | `1` | no | Adds `DEBUG <code>` to presence |
| `PUSH_CODE` / `BOT_VERSION` | — | — | no | Overrides the version label |
| `LOCAL_DB_PATH` | — | `database-test.db` | no | Local SQLite only, when Turso is unset |

Validate any environment before starting the bot:

```bash
python scripts/preflight.py
```

It exits non-zero when something blocking is wrong (missing token,
unreachable database, missing `libsql_experimental`) and warns about
non-blocking gaps (no admin auth, no `EXPECTED_GUILD_ID`, local SQLite on
stable).

---

## 2. Turso databases

Create one database per environment and keep the tokens separate.

```bash
# one-time login
turso auth login

# create both databases
turso db create dummydreams-stable
turso db create dummydreams-test

# connection URLs
turso db show dummydreams-stable --url
turso db show dummydreams-test   --url

# auth tokens (per database)
turso db tokens create dummydreams-stable
turso db tokens create dummydreams-test
```

The schema is created automatically by `init_db()` on the first start, so
a brand-new empty database is fine — no migration step is needed.

Verify the token quickly:

```bash
turso db shell dummydreams-test "SELECT COUNT(*) FROM sqlite_master;"
```

Optional: copy production data into the test database for realistic
testing.

```bash
turso db dump dummydreams-stable > stable-backup.sql
turso db shell dummydreams-test < stable-backup.sql
```

Never point the test bot at the stable database. `scripts/preflight.py`
prints the environment and database host so you can confirm.

---

## 3. Render deployment (blueprint)

`render.yaml` in the repo root defines both services. Push it to `main`,
then in Render: **New + → Blueprint → select this repo → Apply**.

What the blueprint creates:

| Service | Branch | Plan | Health check |
|---|---|---|---|
| `dummydreams-stable` | `main` | starter | `/health` |
| `dummydreams-test` | `test` | starter | `/health` |

Render asks for the `sync: false` secrets at creation time. Fill the
stable service with stable values and the test service with test values.

Important:

- **Use an always-on plan.** Free instances sleep after inactivity, which
  stops the Discord gateway and the TikTok observer. `plan: starter` is
  set for that reason.
- Python version comes from `.python-version` (`3.11.9`).
- Do not set `PORT` manually; Render injects it.
- `libsql-experimental` may need build tools on some images. If the build
  fails, add `cmake`/`build-essential` to the build command or pin a
  version with a prebuilt wheel.
- Auto-deploy is on: pushing to `test` updates only the test service,
  pushing/merging to `main` updates only stable.

Health check:

```bash
curl -s https://dummydreams-stable.onrender.com/health
# {"db":true,"live_tracked":0,"ok":true}
```

---

## 4. Replit deployment

The current `.replit` uses `deploymentTarget = "autoscale"`. Autoscale
sleeps when there is no HTTP traffic, which stops the Discord connection
and the live observer — use a **Reserved VM** deployment for a bot that
must stay online:

```toml
[deployment]
deploymentTarget = "vm"
```

For two Repls (stable + test), give each Repl its own:

- Secrets: `DISCORD_TOKEN`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`,
  `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `EXPECTED_GUILD_ID`.
- `BOT_ENV` (`stable` / `test`).
- Run command: `python main.py`.

Keep the stable Repl on `main` and the test Repl on `test`; pull and
restart the matching Repl after each push.

---

## 5. Local two-process development

Two copies of the repo, or one repo with two env files:

```bash
# terminal 1 — stable-like
cp .env.example .env.stable
# fill .env.stable, then:
set -a; . ./.env.stable; set +a
BOT_ENV=stable LOCAL_DB_PATH=database-stable.db PORT=8080 python main.py

# terminal 2 — test
cp .env.example .env.test
# fill .env.test, then:
set -a; . ./.env.test; set +a
BOT_ENV=test LOCAL_DB_PATH=database-test.db PORT=8081 python main.py
```

Always set `LOCAL_DB_PATH` when running two instances locally, otherwise
both share `database.db`.

---

## 6. Stable deployment (official server)

1. Checkout and update the stable branch:

   ```bash
   git checkout main
   git pull origin main
   ```

2. Configure environment variables for the **stable** bot, database and
   server. Set `BOT_ENV=stable`.
3. Run the preflight check:

   ```bash
   python scripts/preflight.py
   ```

4. Start the bot:

   ```bash
   python main.py
   ```

5. In the official Discord server, as an admin:
   - `/set_verified_role @Verified` — set the verification role.
   - `/setup_verification` — post the TikTok verification panel.
   - `/welcome-setup` — post the welcome + rules message in `#rules`.
6. Verify in the admin panel that settings point to the official
   channels/roles (Bot Settings, announcement channels, role tiers).

---

## 7. Test deployment (test server)

1. Checkout the test branch:

   ```bash
   git checkout test
   git pull origin test
   ```

2. Configure environment variables for the **test** bot, database and
   server. Set `BOT_ENV=test`.
3. Run `python scripts/preflight.py` and confirm it reports `test`.
4. Start the bot in a separate process/instance:

   ```bash
   python main.py
   ```

5. In the test server, run the same admin commands to validate changes.

Because the token, database, and server are separate, changes made while
testing cannot affect the stable deployment.

---

## 8. Development workflow

1. Switch to the test branch:

   ```bash
   git checkout test
   ```

2. Make and commit changes.
3. Push and deploy to the test instance.
4. Validate end-to-end:
   - `/welcome-setup` posts the rules message.
   - Verification (bio / live code / live greeting) succeeds.
   - The verified role is granted immediately.
   - Announcements, leaderboard, sessions, and analytics work.
5. Promote to stable:

   ```bash
   git checkout main
   git merge --no-ff test
   git push origin main
   ```

6. Deploy `main` to the stable instance.

Optional: tag stable releases.

```bash
git tag -a v1.0.0 -m "Stable release v1.0.0"
git push origin v1.0.0
```

---

## 9. Health and operations

- `GET /health` returns JSON with the database status and tracked live
  count. Returns HTTP 503 if the database check fails.
- `python scripts/preflight.py` validates configuration without touching
  data.
- The admin panel is served with `waitress` when installed, otherwise with
  Flask's development server.
- Watch logs for:
  - `[Discord] Bot logged in as ... (env=...)` — confirms which deployment
  - `[Role Error]` — role permission/hierarchy problems
  - `[Verification]` — verification and auto-grant outcomes
  - `[Analytics] ... failed` — analytics query failures
  - `WARNING: connected guilds ...` — wrong `EXPECTED_GUILD_ID`

---

## 10. Checklist before going live

- [ ] Separate Discord bot and token for stable.
- [ ] Separate Turso database (no shared data with test).
- [ ] `BOT_ENV=stable` and `EXPECTED_GUILD_ID` set to the official server.
- [ ] `python scripts/preflight.py` passes on both deployments.
- [ ] Hosting plan always-on (not autoscale/free-sleep).
- [ ] `ADMIN_USERNAME`/`ADMIN_PASSWORD` enabled; panel behind HTTPS/VPN.
- [ ] Verified role configured and bot role above it, with **Manage Roles**.
- [ ] `/setup_verification` panel posted in the verification channel.
- [ ] `/welcome-setup` posted in `#rules`.
- [ ] Announcement/leaderboard channels configured.
- [ ] End-to-end verification tested with a real account.
- [ ] Backups of the production database configured.
- [ ] Secrets rotated if they were ever shared.
