# Truth or Dare Discord Bot

A single-server bot with daily prompts, slash commands, themes, intensity levels,
quiet hours, admin prompt management and durable history. It never tags members.
Participation is optional. Six general-audience starter prompts are included;
server administrators supply any additional prompt collection.

## Commands

Run commands in the configured server channel. Administrative commands require
Discord's **Manage Server** permission and are checked again by the bot.

| Command | Purpose |
| --- | --- |
| `/ask-now [mode] [theme] [intensity]` | Post now; mode is truth, dare or mixed; intensity is 1-5. |
| `/settings` | Show schedule, quiet hours, filters, pause state and prompt count. |
| `/pause`, `/resume` | Pause/resume automatic posts. Manual posts remain available outside quiet hours. |
| `/schedule time timezone` | Set HH:MM local time and an IANA timezone. Default: 19:00 America/Chicago. |
| `/quiet-hours [start] [end]` | Suppress all prompts during this interval, including overnight. Omit both to disable. |
| `/configure [mode] [theme] [intensity]` | Set filters for scheduled posts. Omitted theme/intensity means any. |
| `/add-prompt kind intensity text [theme]` | Add a prompt; kind is truth or dare. |
| `/import-prompts prompts` | Import one `truth/dare|1-5|theme|text` entry per line. Invalid batches make no changes. |

Prompts avoid the most recent 20 selections where the pool permits; otherwise
the least recently used matching prompt is chosen. Matching only one prompt
necessarily allows repeats. Quiet hours use the configured timezone, include the
start and exclude the end. A missed scheduled minute catches up later the same
local day after downtime or quiet hours. There is at most one automatic attempt
per local date. If delivery is interrupted after an attempt starts, inspect the
channel before using `/ask-now`; automatic retries are suppressed to avoid
posting twice. Changing timezone can change the meaning of a local date.

## Local setup

Python 3.11+ (deployment uses 3.12):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

Copy `.env.example` to `.env` and fill in `BOT_AUTH`, `GUILD_ID`, and `CHANNEL_ID`
locally. Never commit or share the Discord token. Invite the existing bot with
`bot` and `applications.commands` scopes. Grant View Channel, Send Messages and
Embed Links in the target channel. It does not need Administrator or privileged
Message Content/Server Members intents. Use a normal server text channel.

```powershell
.\.venv\Scripts\python.exe -m truth_or_dare_bot
.\scripts\verify.ps1
```

SQLite defaults to `data/truth_or_dare.sqlite3`; existing local data is preserved.
Set `DATABASE_PATH` to use another persistent local path. Back up SQLite only
while the bot is stopped, or use SQLite's backup API.

## Northflank deployment (free Sandbox)

Use the existing `truthordarebot` Build service, plus one Deployment service linked
to its `main` branch. Build with the root `Dockerfile`, context `/`. The image
installs pinned dependencies and runs the test suite before it can be deployed.
Run the default image command as a background worker with one instance and no
public port. Select the explicitly Free/Sandbox compute option. No paid upgrade,
standalone paid volume, external database endpoint or new subscription is needed.

Use the one free PostgreSQL addon for durable prompts, settings and history.
Keep it private. Link its connection URI into the worker as `DATABASE_URL`,
without copying it into code, logs or chat. Set `REQUIRE_POSTGRES=true` so a missing
connection cannot silently fall back to disposable container storage. Enter the
existing Discord token privately as `BOT_AUTH` in runtime variables, never build
arguments. Also set GUILD_ID, CHANNEL_ID, TIMEZONE=America/Chicago, POST_TIME=19:00.

Do not run the local bot and cloud bot simultaneously. After startup, logs should
show nine registered commands and `READY: Discord connected; configured channel
and posting permissions verified`. Verify `/settings`, then a single authorized
`/ask-now`. Changes to schedule/filters/prompts persist in PostgreSQL. Redeploying
or rolling back code must retain that database. Do not delete the addon to fix a
build failure. Before teardown or migration, export the database through the
hosting provider's backup tools.

Free hosting availability/limits are controlled by Northflank. Confirm the UI
shows zero-cost resources before creating them; do not accept paid substitutions.
See https://northflank.com/pricing and
https://northflank.com/docs/v1/application/release/manage-ci-cd.

## Verification limits

Local tests cover permission boundaries, atomic imports, invalid inputs, quiet
hours, schedule catch-up, failed/cancelled sends, durable daily claims and concurrent
ticks. Live cloud build, connection, command registration and persistence must
also be checked before calling the deployment complete. No model or AI API is
used by this bot.
