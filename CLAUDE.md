# tttt: the deobf Discord bot

> Reference, not a changelog: record how things work and facts that are hard
> to rediscover, and replace outdated text instead of appending to it.
> **Hard limit: 200 lines.**

One command, `.deobf`, takes a protected Roblox Luau script (attachment, paste
link, or a reply to a message with either) and sends readable Luau back, editing
one embed from "queued" to the finished file. `README.md` is the user-facing
documentation, including the Wispbyte steps.

**A token is the only thing that has to be set.** That is the point of the
layout: the pipeline ships with the bot and runs on the same host by default.

## Layout

- `main.py` - the whole bot, in five sections: settings (`.env` +
  environment), the engines (`Api`, `Local`), the input (paste links, the SSRF
  guard), the embed (`Job`, the `*_embed` builders, `Editor`), and the Discord
  client (`Bot`).
- `deobf/` - **a copy of the pipeline from
  [riftwarewtf/deobf](https://github.com/riftwarewtf/deobf)**. Deobfuscation
  changes belong there and are copied over; nothing in `deobf/` is edited here.
  That repo's `CLAUDE.md`, `LURAPH.md` and `IRONBREW1.md` are the reference for
  how the pipeline works.
- `start.sh` - for a host that runs one command; restores the executable bit on
  the Luau binaries and installs the dependencies on first boot.

## Two engines, one interface

`pick_engine()` returns `Local` when `DEOBF_API` is empty (the default) and
`Api` when it is set. Both offer exactly three calls, so neither the command
nor the embed knows which is behind them:

- `start()` -> `{where, luau, max_input, queue, plugins}`, for the startup log
  and `.status`. Raises `EngineError` when the engine is not usable.
- `run(job, options, progress)` - fills in `job.result` or raises
  `EngineError`; `await progress(job)` whenever something changed.
- `log_tail(job)` - lines attached when a request fails.

`Local` runs `deobf/deob.py` as a subprocess per request (plus one
`--detect` first, for the label). `Api` drives the server's HTTP API.

### The server's API (what `Api` depends on)

From `web/server.py` upstream; a change there breaks this half.

- `GET /api/health` -> `{ok, luau, obfuscators:[{name,label,aliases}], limits:
  {max_upload, ...}, queue:{...}}`.
- `POST /api/jobs` with JSON `{source, name, options}` -> 202 + the job's info.
  **`source` is the script decoded as latin-1**: the server re-encodes it the
  same way, so every byte survives the JSON round trip. UTF-8-decoded text
  would corrupt any byte over 127.
- `GET /api/jobs/{id}/events` - server-sent events: `log`, `state` (the job's
  info, whenever it changes), one `end`. `info()["elapsed"]` changes every
  pass, so a state arrives about three times a second, which is what the job
  deadline is checked against.
- `/api/jobs/{id}`, `/log?since=`, `/result` (409 before it exists),
  `DELETE /api/jobs/{id}`.
- `detected` in the job's info is the plugin's label **with the Luraph
  version** ("Luraph v14.4.2"), so there is no need to call `/api/detect`.

## Rules that matter

- **Never import the pipeline into this process.** `deob.py` keeps global state
  (the Path2D cache, the last raw run), so `Local` spawns a subprocess per
  request in its own temp folder. See the upstream `CLAUDE.md`, "Usage".
- **`deobf/bin/luau` and `deobf/bin/luau-ast` are committed here** (they are
  git-ignored upstream). Wispbyte has no compiler, so the binaries must ship.
  They are built with `python deobf/build_luau.py --portable` - **`--portable`
  matters**: the default build uses `-march=native` and would crash on a
  different host's CPU. Statically linked, stripped, x86-64 Linux.
- **The bot needs MESSAGE CONTENT INTENT.** Without it `on_message` sees empty
  content and the bot silently never answers.
- **The GitHub Pages site is not an API.** It runs the pipeline inside the
  visitor's browser; `/api/health` there is a 404. `DEOBF_API`, when set, has to
  be a real `web/server.py`. When it is unreachable the bot says so in the
  channel and at startup - never let that look like a deobfuscation failure.
- Whether the result came from the lifter is read off its first line
  (`TRACE_MARK`, via `Job.note_lifted`), not from what was asked for: the
  pipeline falls back to a trace on its own (a Luraph VM older than v14, or a
  lift that failed), and the embed has to say which one it is.
- `Local` holds a semaphore of `MAX_CONCURRENCY` (2) because each lift can want
  a few hundred MB; waiting requests show "Queued" with how many are ahead.
  `_kill` ends the whole process group - the pipeline starts children.
- A deobf server has no authentication and runs untrusted scripts, so its
  address only goes to `OWNER_IDS` (`.status`), and `DEOBF_API_TOKEN` exists for
  when it sits behind a proxy that checks one.
- A link anyone can type is fetched by this host, so `_public_host` refuses
  loopback, private, link-local and reserved addresses.
- Discord edits are rate limited per channel; `Editor` never edits faster than
  `EDIT_INTERVAL` (2 s) and keeps the last state to send when the interval is up.
- Attachments cannot be added to a message by editing its embed alone: the final
  edit passes `attachments=[...]`, and falls back to a follow-up message.
- `Api.follow` is an async generator holding a streaming response open, so it is
  closed with `aclose()` in a `finally` - not left to the garbage collector, and
  never wrapped in `asyncio.wait_for` (cancelling it there leaks the response).

## Checking a change

No test suite, and `python main.py` needs a token. Check the parts that do not,
**against both engines** - run the check once with `DEOBF_API` empty and once
with it pointed at a `python web/server.py` from the upstream repo:

- a real request end to end with stand-in Message/Attachment/Channel objects
  driving `Bot.on_message`: the finished embed must carry the version, and the
  attachment must equal the upstream `samples/output/` file byte for byte.
- the legacy-Luraph sample: labelled with its version, reported as a
  **Behaviour trace**, with the "lifting did not apply" note.
- the embeds: `to_dict()` under Discord's 6000-character limit.
- `raw_url` against the paste hosts it rewrites, and
  `fetch("http://127.0.0.1/...")` to confirm it is refused.
- `DEOBF_API` at a closed port: the command must answer with the "not
  answering" notice, and `setup_hook` must not raise.
- `MAX_CONCURRENCY=1` with two requests at once: only one subprocess runs.
