# deobf bot

A Discord bot that deobfuscates protected Roblox Luau scripts. Attach a file or
paste a link, and it detects the obfuscator, runs the script in a real Luau VM
against a fake Roblox environment, and sends readable Luau back — editing one
embed the whole way, so a request is a single line in the channel.

```
.deobf <attachment>
.deobf https://pastebin.com/AbCd1234
.deobf            (as a reply to a message that has the file or the link)
```

**One file, `main.py`.** The deobfuscating itself happens on a **deobf server**
that this bot talks to over HTTP, so nothing heavy lives here: no Luau runtime,
no pipeline, nothing to compile. Two dependencies, both pure pip.

| Obfuscator | What comes back |
|---|---|
| Luraph v14, v15 and newer | **Devirtualized**: the VM's bytecode lifted back to Luau, with control flow, locals, closures and the branches that never ran |
| IronBrew 1 | **Devirtualized**, same |
| Luraph up to v13 | **Behaviour trace** — that generation is a different VM (a Lua 5.1 interpreter) and has no lifter yet |
| anything else | **Behaviour trace**: everything the script does to its environment, written back as Luau — only the branches that actually ran |

Luraph stamps its version into a comment at the top of every script it makes, so
the embed names it exactly: `Luraph v14.4.2`.

Options, anywhere in the command:

* `--trace` — skip lifting and only trace. Much faster, much less memory.
* `--obf NAME` — force a plugin instead of detecting it (`luraph`, `ironbrew1`,
  `generic`).

`.help` prints that in the channel. `.status` reports what the server says about
itself — its queue, its upload limit, whether it has a Luau runtime.

---

## You need a deobf server

This bot is only the Discord side. The pipeline lives in
[riftwarewtf/deobf](https://github.com/riftwarewtf/deobf), and its `web/` half
is an HTTP API this bot drives: submit a script, follow the job's progress
stream, fetch the result.

> **The GitHub Pages site is not that API.** <https://riftwarewtf.github.io/deobf/>
> runs the whole pipeline *inside the visitor's browser* — there is no server
> behind it, and `/api/health` there is a 404. A bot cannot use it.

Start one wherever this bot can reach it over HTTP:

```bash
git clone https://github.com/riftwarewtf/deobf
cd deobf
python deobf/build_luau.py --portable     # once: builds the Luau runtime
pip install -r web/requirements.txt
DEOB_HOST=0.0.0.0 python web/server.py    # http://<that machine>:8000
```

or with the Dockerfile in that repo, which builds Luau for you:

```bash
docker compose up -d                      # http://<that machine>:8000
```

Then point the bot at it with `DEOBF_API`. Check it first — the bot logs a
warning at startup if the server is unreachable, and says so in the channel
rather than failing silently:

```bash
curl http://<that machine>:8000/api/health
```

**Keep it off the open internet.** The deobf server has no authentication: it
runs untrusted scripts for anyone who can reach it. Put it on a private network
with the bot, or behind a reverse proxy or Cloudflare Access that checks a token
— and then set `DEOBF_API_TOKEN`, which the bot sends as
`Authorization: Bearer <token>`.

## Hosting the bot on Wispbyte

1. **Make the bot application.**
   [Discord Developer Portal](https://discord.com/developers/applications) →
   *New Application* → **Bot** → *Reset Token* and copy it.
   On that same page turn on **MESSAGE CONTENT INTENT** — without it the bot
   cannot read `.deobf` and will simply never answer.

2. **Invite it.** *OAuth2 → URL Generator*, scope `bot`, permissions
   **Send Messages**, **Embed Links**, **Attach Files**, **Read Message
   History**. Open the URL it builds and add the bot to your server.

3. **Make the server on Wispbyte** with the **Python** egg (3.10+).

4. **Pull this repository in.** Set the panel's **Git repository** field to
   `https://github.com/riftwarewtf/tttt`, or run
   `git clone https://github.com/riftwarewtf/tttt .` in an empty
   `/home/container` from the console.

5. **Give it the token and the server.** Either add `DISCORD_TOKEN` and
   `DEOBF_API` as variables in the panel's **Startup** tab, or copy
   `.env.example` to `.env` in the file manager and fill in the first two.

6. **Set the startup command** to

   ```
   bash start.sh
   ```

   That installs the two dependencies on first boot and then runs the bot.
   (If your egg already installs `requirements.txt` by itself, `python3 main.py`
   works just as well.)

7. **Start it.** The console prints the server it is pointed at, then
   `logged in as <name>`.

## Anywhere else

```bash
git clone https://github.com/riftwarewtf/tttt
cd tttt
cp .env.example .env          # token + DEOBF_API
pip install -r requirements.txt
python main.py
```

Any Python 3.10+ with ~60 MB of RAM. All the weight is on the deobf server.

## Settings

All of them are environment variables, and all but the first two have a
default — see [`.env.example`](.env.example) for the annotated list.

| Variable | Default | What it does |
|---|---|---|
| `DISCORD_TOKEN` | — | required |
| `DEOBF_API` | `http://127.0.0.1:8000` | the deobf server |
| `DEOBF_API_TOKEN` | empty | sent as `Authorization: Bearer …` |
| `BOT_PREFIX` | `.` | `.deobf`, `.help`, `.status` |
| `ALLOWED_GUILDS` / `ALLOWED_CHANNELS` | empty | ids the bot answers in; empty means everywhere |
| `OWNER_IDS` | empty | skip the cooldown; the only ones `.status` shows the server address to |
| `MAX_INFLIGHT` | `4` | jobs this bot keeps on the server at once |
| `MAX_INPUT_BYTES` | 12 MB | largest script accepted (lowered to the server's own limit) |
| `JOB_TIMEOUT` | `1800` | seconds before the bot cancels a job |
| `USER_COOLDOWN` | `10` | seconds between one person's requests |
| `DEVIRT` | `1` | `0` makes every request trace-only |
| `PREVIEW_LINES` | `18` | lines of the result shown in the embed |

## What is in here

```
main.py           the whole bot: settings, the deobf API client, paste links,
                  the progress embed and the Discord command
requirements.txt  discord.py and aiohttp
start.sh          install dependencies, then run the bot
.env.example      every setting, annotated
```

Nothing is sent anywhere except the deobf server you point it at. That server
runs the protected script locally against a fake Roblox/executor environment,
and never touches the network on the script's behalf — a `HttpGet` in the script
is recorded, not followed.
