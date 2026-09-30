# deobf bot

A Discord bot that deobfuscates protected Roblox Luau scripts. Attach a file
or paste a link, and it detects the obfuscator, runs the script in a real Luau
VM against a fake Roblox environment, and sends readable Luau back — editing
one embed the whole way, so a request is a single line in the channel.

```
.deobf <attachment>
.deobf https://pastebin.com/AbCd1234
.deobf            (as a reply to a message that has the file or the link)
```

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
* `--obf NAME` — force a plugin instead of detecting it (`luraph`,
  `ironbrew1`, `generic`).

`.help` prints the same thing in the channel.

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

3. **Make the server on Wispbyte.** Create a server with the **Python** egg
   (any recent 3.x; 3.10+ is what this is tested on).

4. **Pull this repository in.** In the panel, either
   * set the **Git repository** / *auto-clone* field to
     `https://github.com/riftwarewtf/tttt`, or
   * open the **Console** and run
     `git clone https://github.com/riftwarewtf/tttt .`
     in an empty `/home/container`.

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
   (If your egg already installs `requirements.txt` by itself, `python3 bot.py`
   works just as well.)

7. **Start it.** The console prints `logged in as <name>`, and `.deobf` works
   in any channel the bot can see.

### If the free plan is small

Lifting a big script wants a few hundred MB for a minute or two. On a plan
that cannot spare that, set

```
DEVIRT=0
MAX_CONCURRENCY=1
```

in the panel or in `.env`. Every request then returns the behaviour trace,
which is fast and cheap. Detection still runs, so the embed still says what
the script was protected with.

## Hosting it anywhere else

Any machine with Python 3.10+ and ~1 GB of RAM:

```bash
git clone https://github.com/riftwarewtf/tttt
cd tttt
cp .env.example .env          # put your token in it
pip install -r requirements.txt
python bot.py
```

The Luau runtime for x86-64 Linux is committed in `deobf/bin/`, statically
linked, so nothing has to be compiled. On another architecture or on Windows,
build it once with

```bash
python deobf/build_luau.py --portable
```

(needs git, cmake and a C++ compiler; it takes a few minutes).

## Settings

All of them are environment variables, and all of them have a default — see
[`.env.example`](.env.example) for the annotated list. The ones worth knowing:

| Variable | Default | What it does |
|---|---|---|
| `DISCORD_TOKEN` | — | the only required one |
| `BOT_PREFIX` | `.` | `.deobf`, `.help` |
| `ALLOWED_GUILDS` / `ALLOWED_CHANNELS` | empty | ids the bot answers in; empty means everywhere |
| `MAX_CONCURRENCY` | `2` | scripts deobfuscated at the same time |
| `MAX_INPUT_BYTES` | 12 MB | largest script accepted |
| `JOB_TIMEOUT` | `900` | seconds one request may take |
| `USER_COOLDOWN` | `10` | seconds between one person's requests |
| `DEVIRT` | `1` | `0` makes every request trace-only |
| `PREVIEW_LINES` | `18` | lines of the result shown in the embed |

## What is in here

```
bot.py            start the bot
bot/
  client.py       the Discord side: the command, the embed, the reply
  runner.py       one request = one deob.py subprocess, watched line by line
  sources.py      attachments and paste links (pastebin, gist, rentry, ...)
  embeds.py       the progress embed and its stage checklist
  config.py       settings, from the environment and from .env
deobf/            the deobfuscator itself, a copy of riftwarewtf/deobf
  bin/luau        the Luau runtime the protected script runs in (static x86-64)
start.sh          install dependencies, then run the bot
```

Nothing is sent anywhere. The protected script runs locally, against a fake
Roblox/executor environment, and the network is never touched on its behalf —
a `HttpGet` in the script is recorded, not fetched.

The deobfuscator itself lives in [riftwarewtf/deobf](https://github.com/riftwarewtf/deobf)
(which also has the web front end); `deobf/` here is a copy of that pipeline, so
changes to the deobfuscation belong there and get copied over.
