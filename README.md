# Telegram Project Bot

Discovery timing update: the default scan interval is now 10 seconds. Existing installations switch to 10 seconds once on restart; later `/interval` changes are preserved. `/check` can request a scan every 10 seconds. These timings supersede the older 30-second scan descriptions below. Server backoff still applies, and verification is fetched before the first alert where available. Freelancer API publication and network delays can still delay delivery relative to the website.

A Telegram bot that monitors new Freelancer.com projects and sends each subscriber alerts matching their own skills and filters. Python 3.10+; no pip packages required.

## Start on Windows

1. Install Python 3.10 or newer if needed: https://www.python.org/downloads/ (enable **Add Python to PATH**).
2. Extract this ZIP into a normal folder.
3. Open https://t.me/BotFather in Telegram, send `/newbot`, choose a name and username, and copy the bot token. Use a dedicated bot.
4. Double-click **start.bat**. Paste the token into the hidden local prompt, then send the displayed pairing command to your new bot in a private chat.
5. Once the application starts, send `/start` to your bot. Leave the terminal open to receive alerts.

Setup verifies the host owner's account using a random pairing code. You do not need to find your Telegram ID manually. Other people can subscribe by opening the same bot in a private chat and sending `/start`. Each person controls only their own alerts. Do not share `.env`; it contains your credentials.

## Let others subscribe and choose their skills

Restart the host bot once after installing this upgrade. Share the bot's Telegram username or link; subscribers do not install Python, run another bot process, or need your token. Each person sends:

```text
/start
/skills Python, React, Graphic Design
/filters
```

`/skills` and `/keywords` are aliases. Comma-separated terms match any project title, description, or listed skill. `/skills off` accepts all skills. New subscribers start with all skills, a USD 100 fixed upper-budget minimum, a USD 15 hourly upper-rate minimum, and a five-minute age window. They can independently change budgets, exclusions, countries, and other filters. `/pause`, `/resume`, `/stop`, `/status`, and `/latest` apply only to the person sending the command. `/stop` unsubscribes while keeping their filters; `/start` resumes their subscription. People whose earlier `/start` was ignored by the old version need to send it again after the upgrade.

The existing owner's settings and delivery history stay in the original database. A subscriber registry is saved there; each other person's settings, sent-project history, and message snapshots are stored in a separate SQLite file under `data/bot-subscribers/` (or `<database-stem>-subscribers/` beside a custom database). Back up this folder together with the main database. Delivery of a project to one person does not suppress it for another matching subscriber.

One shared source scan serves all active subscribers, using the largest requested age window and fastest requested scan interval. `/check` applies fresh cached data immediately and requests a source refresh no more often than once per 30 seconds; source backoff still applies. Each subscriber's filters and project age are rechecked before delivery. Up to four recipients can receive alerts concurrently, with at most one alert delivery in progress per subscriber. Slow requests in one chat do not hold up the others. Each chat retains a minimum 1.5-second gap after a send, with a shared 0.1-second gap between outgoing requests across chats and a shared Telegram rate-limit cooldown. A user who blocks the bot is deactivated without stopping delivery to other users. Group chats are not supported.

## macOS / Linux

From the extracted project folder:

```sh
python3 setup_bot.py
python3 -m projectbot.app
```

The setup requires your Telegram account to create the bot and retrieve its token. No bot has been created or activated on your account by this download.

## Starting settings

| Setting | Default |
|---|---|
| Source | Freelancer.com active projects |
| Keywords | Python, FastAPI, artificial intelligence, AI, LLM, React, Next.js, embedded, STM32, PLC |
| Matching | Any keyword in title, description, or skills; case insensitive, word boundaries |
| Exclusions | None |
| Fixed project minimum | Advertised **upper** budget at least USD 100 equivalent |
| Hourly minimum | Advertised **upper** hourly rate at least USD 15 equivalent |
| Countries | All |
| Payment verification | Not required |
| Bids | No limit |
| Project age | At most 5 minutes |
| Checks | Every 30 seconds |

A $50–$250 project passes `/budget 100` because its upper budget is $250. This is a lead filter; the posted range is not guaranteed compensation. Non-USD values use the API's approximate currency exchange rate. Unknown currency rates fail positive budget floors; setting a floor to zero disables that budget check. Original amounts and currencies appear in alerts. Every alert shows the project's exact Freelancer posting timestamp in Japan Standard Time, down to the second.

The bot checks every 30 seconds and suppresses already-confirmed deliveries across restarts. The cadence is measured from the start of a source request instead of adding another full interval after it completes. Projects older than five minutes are excluded by default, including old pending entries. Starting the bot does not replay sent projects. Source scanning and Telegram delivery run independently, so a delivery backlog or Telegram cooldown does not block discovery. Each send selects the newest pending project and rechecks its age; because client verification is the most important field, a live alert first attempts a quick public-page lookup (six-second bound) so verification arrives with the message, falling back to background enrichment only when that lookup is slow, unavailable, or rate-limited. Summaries wait while that subscriber has pending alerts; client verification updates continue independently of the alert backlog. `/latest` displays up to five cached previews within the age window. An outage longer than the age window can cause missed projects; temporarily enlarge `/age` to recover eligible recent listings. `/status` and the logs explain the last alert skipped because it expired or no longer matched filters.

On the first startup after this upgrade, the saved scan interval changes to 30 seconds and the maximum project age to five minutes. Later `/interval` and `/age` changes are preserved. Polling is not instant: normal discovery takes up to roughly one scan interval plus API response and delivery time; source publication delays, outages, and rate limits can extend this. Temporary connection/server failures retry from five seconds, with source backoff capped at 60 seconds unless the server asks for a longer delay. Explicit server retry delays are always honored. Reusing cached projects does not update the last successful source-scan time or clear a source error. `/status` shows the scan interval, last delivered project's age, the last public lookup result, and API coverage for identity, payment, deposit, email, profile, and phone states. Unknown client states stay hidden in project posts.

On Telegram HTTP 429, all Telegram requests respect a shared cooldown using the server's full retry delay. The cooldown is saved in SQLite so restarting does not bypass it. Startup also waits and retries temporary connection failures. Logs name the failed API method and show the retry delay in seconds. Keep only one bot process running; another copy has its own request pacing and can cause conflicts or further rate limits.

## Change filters inside Telegram

Send one command per message:

```text
/keywords python, react, telegram, AI
/skills Python, Graphic Design
/exclude wordpress, data entry
/budget 250
/hourly 20
/type fixed
/countries US, GB, AU
/verified on
/maxbids 30
/age 120
/interval 300
```

| Command | Meaning |
|---|---|
| `/start` | Activate the bot and show help; use `/resume` if paused |
| `/filters` | Show saved settings |
| `/keywords off` | Allow any keyword |
| `/exclude off` | Remove excluded terms |
| `/type any` | Allow fixed and hourly projects |
| `/countries off` | Allow all client countries |
| `/verified off` | Stop requiring verified payment |
| `/maxbids off` | Remove bid-count limit |
| `/check` | Request a fresh scan; rate-limit backoff still applies |
| `/latest` | Show up to five recent cached matches as previews |
| `/pause`, `/resume` | Stop or restart alerts |
| `/stop` | Unsubscribe; keep your filters for a later `/start` |
| `/status` | Last successful scan, match counts, queue, and API errors |
| `/stats` | Category counts for 30 minutes, 1 hour, 1 day, 3 days, and 1 week |
| `/stats on`, `/stats off` | Enable or disable automatic summaries every 30 minutes |
| `/help` | Command reference |

Lists are comma separated. Keywords use OR; separate filter categories use AND. Exclusions take precedence. All settings survive restart in SQLite. `config.example.json` supplies defaults only when creating a new database; subsequently edit settings through Telegram. `/interval` accepts 30–3600 seconds; `/age` accepts 5–1440 minutes.

## Project-count summaries

Each active subscriber receives a project-count table every 30 minutes. `/stats` shows it immediately, and its Refresh counts button updates that message. `/stats off` disables only automatic summaries; `/stats on` enables them again. Pausing or unsubscribing stops scheduled summaries. The next scheduled send and retry delay are saved, and restarting does not send a backlog of missed summaries.

Columns cover rolling 30-minute, 1-hour, 1-day, 3-day, and 1-week windows. Rows include design, embedded engineering, marketing, software development, AI/data, video/audio, writing/translation, engineering/architecture, business/finance, admin/support, and Other. Each project has one inferred primary category, using its listed skills first and title hints second, so category counts add up to the total. Classification is heuristic; interdisciplinary projects may fit several categories but are counted in only one.

Counts use each subscriber's **current** skills, budget, and other matching filters. The rolling windows replace the short alert-age limit for this report; a five-minute alert setting therefore still allows one-week statistics. These are discovered matching projects, regardless of whether an alert was delivered to that subscriber. New subscribers can use the shared history already collected by the host.

History collection begins with the first successful scan after installing this feature. Windows without enough history show `*` and the tracking start time in Japan Standard Time. No older Freelancer history is invented or backfilled, and counts cover only projects the running bot observed; outages, inactive subscriptions, source omissions, and API limits can leave gaps. Source posting timestamps determine the windows. Repeated IDs are deduplicated and keep their earliest observed posting timestamp. The shared history is stored in the host database and retained for 30 days; reports use at most the latest week. Counts do not require extra Freelancer requests or widen the live alert window.

## Freelancer access and client details

The public projects endpoint returned active project listings during a live check on September 7, 2026. It returned no client identities in that check. The bot requests user and country details, but access depends on Freelancer's API permissions and response.

Because client verification is the priority, each live alert and `/latest` preview first attempts a public-page lookup with a six-second socket timeout, so verification states, country, and any public-page avatar arrive with the first message instead of a later edit. If that lookup is slow, unavailable, or rate-limited, the alert still goes out immediately and background workers finish it; the slower secondary owner/avatar API lookup always remains in the background so it cannot hold verification back. This lookup needs no token. It reads explicit boolean values from the exact project's embedded JSON, never infers verification from labels, and preserves known API values during initial cached enrichment. Background refreshes replace older explicit values with the returned client states; unknown values never become unverified. Conflicting client IDs are rejected. Owner API results are also cached for one minute. Some pages omit identity or other states; those remain hidden. Successful lookups are cached for one minute, empty results for 30 seconds, and HTTP 429 pauses public lookups for the server's retry delay. Available public verification states update the original message before the secondary avatar lookup. Four dedicated workers fetch and display public verification states using a six-second public-page socket timeout. A separate single worker handles secondary owner/avatar API requests, so slow avatar requests cannot occupy verification workers. Subscribers share per-project public lookup locks and cached results, avoiding simultaneous duplicate successful lookups. Failed public lookups retain retries and can fall back to the owner API. There is no fixed pause between verification jobs. Telegram pacing and server retry delays still apply; unavailable source fields cannot be shown immediately. On restart, the bot checks the newest 100 saved posts from the last 24 hours and resumes unfinished client lookups without replaying alerts.

Country and payment-verification **filters still use the API data before delivery**. If enabled, unavailable client information is excluded, not assumed to qualify; background public-page enrichment does not change filter eligibility. Check `/status` for API coverage. If you have an authorized Freelancer API OAuth token, put it in `FREELANCER_OAUTH_TOKEN` in `.env` and restart. Obtain your own authorized access through https://developers.freelancer.com/. A token does not guarantee those fields will be available. No client-review filter is implemented in this version.

Project posts use a larger clickable h2 title and plain paragraphs. Details occupy two logical rows: price, Fixed/Hourly, and time; then country flag/name and bid count. Client and Skills have separate icon headings. Skills use normal text separated by middle dots, with no forced row breaks; Telegram wraps skills to the available message width and all skills are preserved. Thin native horizontal rules separate the four groups, matching the approved screenshot. Only Skills uses a native shaded quote block with the Telegram quote border and decoration; other sections remain plain. Yellow highlights and disabled buttons remain removed. Exact pixel sizes, custom section backgrounds, and reaction badges as skill labels are not supported by message formatting. Available client avatars and verification updates are unchanged. Telegram controls final typography and message spacing. Standard HTML uses the same content with a bold title and blank lines between sections.

Avatar lookup is automatic for every project's own client; no manual profile links are needed. The bot preserves the listing's owner ID and checks the exact project's public client record. In the background it resolves missing owner IDs through the project-details API, then requests that owner's avatar and states from the users API. Results are cached for 10 minutes, and late photos update the original post. Additional API lookups do not delay new-project delivery. Lookup errors and availability appear in `/status`; `python diagnose_alerts.py --public --avatar` checks the latest saved post without sending messages.

Bidder photos, country flags, and generic unknown-avatar placeholders are never used. If Freelancer withholds both the owner ID and avatar, the bot cannot retrieve that client's real photo and keeps the post text-only. This occurred in the live checks of recent projects. Authorized `FREELANCER_OAUTH_TOKEN` access may expose the missing client data, depending on permissions; the bot cannot guarantee a photo for every project.

The bot reads the official listings API and public project pages, then sends matching alerts to each subscribed private chat. It does not place bids or contact clients. It honors HTTP/API rate-limit retry delays. No login scraping or restriction bypass is included.

## Run continuously with Docker

First complete local pairing to create `.env`, then place the source folder and `.env` on your always-on server. Stop any local copy before starting the server; run exactly one instance per Telegram bot token.

```sh
docker compose up -d --build
docker compose logs -f
```

Send `/start` once to activate the server instance. A named volume stores configuration and alert history across container restarts. Docker runs the bot as a non-root user and rotates logs. No incoming ports are needed. The host requires outbound HTTPS to `api.telegram.org` and `www.freelancer.com`. Moving from a local database to Docker starts a new history unless you migrate `data/bot.sqlite3` while the bot is stopped. Do not run `docker compose down -v` unless you intend to delete the stored settings and history.

```sh
docker compose down
```

You may set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (a positive private chat ID), `FREELANCER_OAUTH_TOKEN`, and `BOT_DB_PATH` as environment variables instead of using `.env`. Environment variables take precedence. The default database is `data/bot.sqlite3`.

## Checks and limitations

```sh
python -m unittest discover -s tests -v
python demo.py
python check_source.py
```

`demo.py` uses a clearly labeled synthetic listing and sends nothing. `check_source.py` makes a read-only live API call and prints aggregate counts. It uses the shipped default filters, not the running database's customized settings.

Automated tests cover currency handling, keyword boundaries, exclusions, missing metadata, hourly/fixed thresholds, access control, pagination, persistence, and failed delivery retries. A real Telegram send and server deployment must be checked after pairing your bot; credentials are not bundled.

The queue records a delivery only after Telegram acknowledges it. A rare timeout after Telegram accepted a message, or a crash between acceptance and the SQLite write, can lead to a duplicate on retry: Telegram has no idempotency key for `sendMessage`. Sent history is retained for 30 days, longer than the maximum one-day lookback. Pending alerts are rechecked against current filters and age before sending. Source records are snapshots, so a project's status or bid count may change before you open it.

Each scan fixes its time window and paginates up to 2,000 records. The status warns if that cap is reached. The API listing can change during pagination, and completeness is not guaranteed. Source errors use backoff; failed alerts remain pending. No end-to-end delivery claim is made until tested with your own token.

If the bot stops responding: verify the host process is running, inspect terminal/Docker logs, send `/start` in a private chat, check `/status`, and ensure another bot instance or webhook is not consuming updates. The app refuses to replace an existing webhook automatically.

## Primary references

- Telegram bot creation and token setup: https://core.telegram.org/bots/tutorial
- Telegram Bot API (`getUpdates`, `sendMessage`, `getWebhookInfo`): https://core.telegram.org/bots/api
- Freelancer developer portal: https://developers.freelancer.com/
- Freelancer's official Python SDK, used to verify request parameters: https://github.com/freelancer/freelancer-sdk-python

The runtime uses Python's standard HTTPS and SQLite libraries directly; the SDK is not a dependency.
