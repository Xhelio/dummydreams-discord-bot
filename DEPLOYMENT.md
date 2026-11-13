# Deployment Guide — Test vs Stable

This project supports two fully separate deployments:

| | **Stable** | **Test** |
|---|---|---|
| Git branch | `main` | `test` |
| Discord bot | Official bot application | Separate test bot application |
| Token | `DISCORD_TOKEN` (stable) | `DISCORD_TOKEN` (test) |
| Database | Production Turso DB | Separate test Turso DB |
| Hosting | Stable service/instance | Test service/instance |
| `BOT_ENV` | `stable` | `test` |
| `EXPECTED_GUILD_ID` | Official server ID | Test server ID |

Never share a token, database, or hosting process between the two.

---

## 1. Prerequisites

- Python 3.12+
- A Discord application/bot per environment
- A Turso (libSQL) database per environment
- One hosting service/instance per environment (or two local processes)

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## 2. Environment variables

Copy `.env.example` to `.env` and fill it in, or set the values in your
hosting dashboard.

Minimum required for each environment:

```env
DISCORD_TOKEN=...
TURSO_DATABASE_URL=libsql://...
TURSO_AUTH_TOKEN=...
BOT_ENV=stable        # or: test
```

Recommended:

```env
EXPECTED_GUILD_ID=123456789012345678
ADMIN_USERNAME=admin
ADMIN_PASSWORD=a-strong-password
DEBUG_MODE=0
```

Notes:

- `BOT_ENV=test` prefixes the bot presence with `TEST` and prints a test
  warning at startup, so it is easy to tell which bot you are looking at.
- `EXPECTED_GUILD_ID` makes the bot log a warning if it is connected to a
  different server than expected.
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` enable HTTP Basic Auth on `/admin`.
  Leave them empty only if the panel is not publicly reachable.
- Without `TURSO_DATABASE_URL`, the bot falls back to a local SQLite file.

---

## 3. Stable deployment (official server)

1. Checkout and update the stable branch:

   ```bash
   git checkout main
   git pull origin main
   ```

2. Configure environment variables for the **stable** bot/database/server.
3. Set `BOT_ENV=stable`.
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

## 4. Test deployment (test server)

1. Checkout the test branch:

   ```bash
   git checkout test
   git pull origin test
   ```

2. Configure environment variables for the **test** bot/database/server.
3. Set `BOT_ENV=test`.
4. Start the bot in a separate process/instance:

   ```bash
   python main.py
   ```

5. In the test server, run the same admin commands to validate changes.

Because the token, database, and server are separate, changes made while
testing cannot affect the stable deployment.

---

## 5. Development workflow

1. Create/switch to the test branch:

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

## 6. Health and operations

- `GET /health` returns JSON with the database status and tracked live
  count. Returns HTTP 503 if the database check fails.
- The admin panel is served with `waitress` when installed, otherwise with
  Flask's development server.
- Watch logs for:
  - `[Role Error]` — role permission/hierarchy problems
  - `[Verification]` — verification and auto-grant outcomes
  - `[Analytics] ... failed` — analytics query failures
  - `WARNING: connected guilds ...` — wrong `EXPECTED_GUILD_ID`

---

## 7. Checklist before going live

- [ ] Separate Discord bot and token for stable.
- [ ] Separate database (no shared data with test).
- [ ] `BOT_ENV=stable` and `EXPECTED_GUILD_ID` set to the official server.
- [ ] `ADMIN_USERNAME`/`ADMIN_PASSWORD` enabled; panel behind HTTPS/VPN.
- [ ] Verified role configured and bot role above it, with **Manage Roles**.
- [ ] `/setup_verification` panel posted in the verification channel.
- [ ] `/welcome-setup` posted in `#rules`.
- [ ] Announcement/leaderboard channels configured.
- [ ] End-to-end verification tested with a real account.
- [ ] Backups of the production database configured.
- [ ] Secrets rotated if they were ever shared.
