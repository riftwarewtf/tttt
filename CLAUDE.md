# tttt: the deobf Discord bot

> Reference, not a changelog: record how things work and facts that are hard
> to rediscover, and replace outdated text instead of appending to it.
> **Hard limit: 200 lines.**

One command, `.deobf`, takes a protected Roblox Luau script (attachment, paste
link, or a reply to a message with either) and sends readable Luau back, editing
one embed from "queued" to the finished file. `README.md` is the user-facing
documentation, including the Wispbyte steps.

**The bot does no deobfuscating.** It posts the script to a **deobf server** -
the `web/` half of [riftwarewtf/deobf](https://github.com/riftwarewtf/deobf) -
and follows that job. So this repository is one file plus its packaging: no
pipeline, no Luau binaries, two pip dependencies.

## Layout

- `main.py` - everything, in five sections: settings (`.env` + environment),
  the API client (`Api`), the input (paste links, the SSRF guard), the embed
  (`Job`, the `*_embed` builders, `Editor`), and the Discord client (`Bot`).
- `start.sh` - the same thing for a host that runs one command; installs the
  dependencies on first boot.
- `.env.example` - every setting, annotated. `requirements.txt` - discord.py,
  aiohttp.

## The server's API (what `main.py` depends on)

From `web/server.py` upstream. Anything here changing upstream breaks the bot:

- `GET /api/health` -> `{ok, luau, obfuscators:[{name,label,aliases}], advanced,
  limits:{max_upload, job_timeout, result_ttl, rate_limit}, queue:{...}}`.
  Read once at startup (`setup_hook`): it lowers `MAX_INPUT_BYTES` to the
  server's own limit and warns when `luau` is false.
- `POST /api/jobs` with JSON `{source, name, options}` -> 202 + the job's info.
  **`source` is the script decoded as latin-1**: the server re-encodes it the
  same way, so every byte survives the JSON round trip. Sending UTF-8-decoded
  text would corrupt any byte over 127.
  `options` are `jobs.clean_options`' keys - `timeout`, `budget`, `executor`,
  `no_devirt`, `obfuscator`, ... - and an unknown `obfuscator` is a 400.
- `GET /api/jobs/{id}/events` - server-sent events: `log` (a list of lines),
  `state` (the job's info, whenever it changes), one `end`. The bot only uses
  the states; `info()["elapsed"]` changes every pass, so a state arrives about
  three times a second, which is what the job deadline is checked against.
- `GET /api/jobs/{id}` (one info), `/log?since=` (lines, attached on a
  failure), `/result` (the Luau as text, 409 before it exists),
  `DELETE /api/jobs/{id}` (cancel).
- `detected` in the job's info is the plugin's label **with the Luraph
  version** ("Luraph v14.4.2"): that is what the embed shows, so there is no
  need to call `/api/detect` separately.

## Rules that matter

- **The GitHub Pages site is not an API.** It runs the pipeline inside the
  visitor's browser; `/api/health` there is a 404. `DEOBF_API` has to be a real
  `web/server.py`. When it is unreachable the bot says so in the channel and at
  startup - never let that look like a deobfuscation failure.
- **The bot needs MESSAGE CONTENT INTENT.** Without it `on_message` sees empty
  content and the bot silently never answers.
- Whether the result came from the lifter is read off its first line
  (`TRACE_MARK`), not from what was asked for: the pipeline falls back to a
  trace on its own (a Luraph VM older than v14, or a lift that failed), and the
  embed has to say which one it is.
- The deobf server has no authentication and runs untrusted scripts, so its
  address is only shown to `OWNER_IDS` (`.status`), and `DEOBF_API_TOKEN` exists
  for when it sits behind a proxy that checks one.
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

No test suite, and `python main.py` needs a token. Check the parts that do not:

- a real job end to end, against a server started from the upstream repo
  (`python web/server.py`): build a `Job`, call `Bot._run` with stand-in
  Discord objects, and assert the result matches `samples/output/` upstream.
- the embeds: build `progress_embed`, `finished_embed` and `failed_embed` and
  assert each `to_dict()` stays under Discord's 6000-character limit.
- `raw_url` against the paste hosts it rewrites, and
  `fetch("http://127.0.0.1/...")` to confirm it is refused.
- the unreachable-server path: point `DEOBF_API` at a closed port and confirm
  the command answers with the "not answering" notice.
