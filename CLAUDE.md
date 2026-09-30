# tttt: the deobf Discord bot

> Reference, not a changelog: record how things work and facts that are hard
> to rediscover, and replace outdated text instead of appending to it.
> **Hard limit: 200 lines.**

A Discord bot around the `deobf` pipeline. One command, `.deobf`, takes a
protected Roblox Luau script (attachment, paste link, or a reply to a message
with either), detects the obfuscator and sends readable Luau back, editing one
embed from "queued" to the finished file. `README.md` is the user-facing
documentation, including the Wispbyte steps.

## Layout

- `bot.py` — entry point (`python bot.py`); `start.sh` is the same thing for a
  host that runs one command, and also restores the executable bit on the
  Luau binaries (a GitHub zip download drops it).
- `bot/client.py` — the Discord side: the command, the embed lifecycle, the
  reply. `bot/runner.py` — the pipeline: detection, one `deob.py` subprocess
  per request, the stage callback, the bounded queue.
  `bot/sources.py` — attachments and links. `bot/embeds.py` — the embed and
  its throttled editor. `bot/config.py` — settings from the environment and
  from `.env`.
- `deobf/` — **a copy of the pipeline from
  [riftwarewtf/deobf](https://github.com/riftwarewtf/deobf)**. Deobfuscation
  changes belong there and are copied over; nothing in `deobf/` is edited
  here. That repo's `CLAUDE.md`, `LURAPH.md` and `IRONBREW1.md` are the
  reference for how the pipeline works.

## Rules that matter

- **Never import the pipeline into this process.** `deob.py` keeps global
  state (the Path2D cache, the last raw run), so every request is its own
  subprocess in its own temp folder. See the upstream `CLAUDE.md`, "Usage".
- **`deobf/bin/luau` and `deobf/bin/luau-ast` are committed here** (they are
  git-ignored upstream). Wispbyte has no compiler, so the binaries must ship.
  They are built with `python deobf/build_luau.py --portable` — **`--portable`
  matters**: the default build uses `-march=native` and would crash on a
  different host's CPU. They are statically linked, stripped, x86-64 Linux.
- **The bot needs MESSAGE CONTENT INTENT.** Without it `on_message` sees empty
  content and the bot silently never answers.
- The generic plugin has no lifter, so `runner.Run` turns devirtualization off
  for it: leaving it on would spend the whole timeout to produce the same
  trace. Whether the result actually came from the lifter is read back off the
  output's first line (`runner.TRACE_MARK`), not from what was asked for - the
  pipeline falls back to a trace on its own (a Luraph VM older than v14, or a
  lift that failed), and the embed has to say which one it is.
- A link anyone can type is fetched by the host, so `sources._public_host`
  refuses loopback, private, link-local and reserved addresses.
- Discord edits are rate limited per channel; `embeds.Editor` never edits
  faster than `EDIT_INTERVAL` (2 s) and keeps the last state to send when the
  interval is up.
- Attachments cannot be added to a message by editing its embed alone: the
  final edit passes `attachments=[...]`, and falls back to a follow-up message
  if that fails.

## Checking a change

There is no test suite. `python bot.py` needs a token, so check the parts that
do not:

- a real run end to end, without Discord: build a `runner.Run` with a sample's
  bytes and `await runner.Queue(...).submit(run)`, then build every embed from
  the result (`embeds.progress`, `finished`, `failed`) and assert each
  `to_dict()` stays under Discord's 6000-character limit. Samples live in the
  upstream repo (`samples/001_vm_like_dispatch-obfuscated.lua` for Luraph,
  `001_vm_like_dispatch-ib1.lua` for IronBrew).
- `sources.raw_url` against the paste hosts it rewrites, and
  `sources.fetch("http://127.0.0.1/...")` to confirm it is refused.
- `python deobf/deob.py <sample> --detect` and a plain full run, to confirm
  the committed binaries still work.
