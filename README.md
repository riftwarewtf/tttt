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

**Clone it, put your bot token in, start it.** That is the whole setup — the
deobfuscator ships with it and runs on the same host, with the Luau runtime
already built for x86-64 Linux. The bot itself is one file, `main.py`.

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

`.help` prints that in the channel. `.status` reports which engine is in use,
its queue, and the script size limit.

---

## Hosting it on Wispbyte

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

   Do not upload a zip: GitHub's zip drops the executable bit on the Luau
   runtime. (`start.sh` puts it back, but a clone saves the trouble.)

5. **Give it the token.** Either add a `DISCORD_TOKEN` variable in the panel's
   **Startup** tab, or copy `.env.example` to `.env` in the file manager and
   fill in the first line.

6. **Set the startup command** to

   ```
   bash start.sh
   ```

   That installs the two dependencies on first boot and then runs the bot.
   (If your egg already installs `requirements.txt` by itself, `python3 main.py`
   works just as well.)

7. **Start it.** The console prints `engine: local (…)`, then
   `logged in as <name>`, and `.deobf` works in any channel the bot can see.

### If the plan is a small one

Lifting a big script wants a few hundred MB for a minute or two. On a plan that
cannot spare that, either

```
DEVIRT=0
MAX_CONCURRENCY=1
```

— every request then returns the behaviour trace, which is fast and cheap and
still names the obfuscator — or move the work off this box entirely, below.

## Moving the work to a server

Set `DEOBF_API` and the bot stops deobfuscating anything itself: it posts each
script to that **deobf server**, follows the job's progress stream and sends the
result back. Nothing heavy then happens on the box running the bot.

The server is the `web/` half of
[riftwarewtf/deobf](https://github.com/riftwarewtf/deobf):

```bash
git clone https://github.com/riftwarewtf/deobf && cd deobf
docker compose up -d                      # http://<that machine>:8000
```

or without Docker:

```bash
python deobf/build_luau.py --portable     # once: builds the Luau runtime
pip install -r web/requirements.txt
DEOB_HOST=0.0.0.0 python web/server.py
```

Then `DEOBF_API=http://<that machine>:8000`. Check it with
`curl http://<that machine>:8000/api/health` first — the bot logs a warning at
startup if it cannot reach it, and says so in the channel rather than failing
silently.

> **The GitHub Pages site is not such a server.**
> <https://riftwarewtf.github.io/deobf/> runs the whole pipeline *inside the
> visitor's browser* — there is nothing behind it, and `/api/health` there is a
> 404. A bot cannot use it.

**Keep a deobf server off the open internet.** It has no authentication and it
runs untrusted scripts for anyone who can reach it. Put it on a private network
with the bot, or behind a reverse proxy or Cloudflare Access that checks a token
— and then set `DEOBF_API_TOKEN`, which the bot sends as
`Authorization: Bearer <token>`.

## Anywhere else

```bash
git clone https://github.com/riftwarewtf/tttt
cd tttt
cp .env.example .env          # put your token in it
pip install -r requirements.txt
python main.py
```

Any machine with Python 3.10+ and ~1 GB of RAM. The committed Luau runtime is
static x86-64 Linux; on another architecture or on Windows, build it once with
`python deobf/build_luau.py --portable` (needs git, cmake and a C++ compiler).

## Settings

All of them are environment variables, and all but the token have a default —
see [`.env.example`](.env.example) for the annotated list.

| Variable | Default | What it does |
|---|---|---|
| `DISCORD_TOKEN` | — | the only required one |
| `DEOBF_API` | empty | empty: deobfuscate here; set: use that deobf server |
| `DEOBF_API_TOKEN` | empty | sent as `Authorization: Bearer …` |
| `BOT_PREFIX` | `.` | `.deobf`, `.help`, `.status` |
| `ALLOWED_GUILDS` / `ALLOWED_CHANNELS` | empty | ids the bot answers in; empty means everywhere |
| `OWNER_IDS` | empty | skip the cooldown; the only ones `.status` shows a remote server's address to |
| `MAX_CONCURRENCY` | `2` | scripts deobfuscated at once on this machine |
| `MAX_INPUT_BYTES` | 12 MB | largest script accepted |
| `JOB_TIMEOUT` | `1800` | seconds before the bot gives up on a job |
| `USER_COOLDOWN` | `10` | seconds between one person's requests |
| `DEVIRT` | `1` | `0` makes every request trace-only |
| `PREVIEW_LINES` | `18` | lines of the result shown in the embed |

## What is in here

```
main.py           the whole bot: settings, both engines (local subprocess and
                  the deobf server's API), paste links, the progress embed and
                  the Discord command
deobf/            the deobfuscator, a copy of riftwarewtf/deobf
  bin/luau        the Luau runtime the protected script runs in (static x86-64)
requirements.txt  discord.py and aiohttp
start.sh          fix the executable bit, install dependencies, run the bot
.env.example      every setting, annotated
```

Nothing is sent anywhere. The protected script runs against a fake
Roblox/executor environment, and the network is never touched on its behalf — a
`HttpGet` in the script is recorded, not fetched.

The deobfuscator itself lives in
[riftwarewtf/deobf](https://github.com/riftwarewtf/deobf) (which also has the
web front end); `deobf/` here is a copy of that pipeline, so changes to the
deobfuscation belong there and get copied over.
