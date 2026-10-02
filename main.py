#!/usr/bin/env python3
"""
deobf bot - a Discord front end for a deobf server.

    .deobf <attachment, paste link, or a reply to either>

The deobfuscating happens on a deobf server (the `web/` half of
riftwarewtf/deobf): this process only takes the script off Discord, posts it to
that server's HTTP API, follows the job's progress stream, and edits one embed
until the finished Luau comes back. Nothing but discord.py and aiohttp is
needed here - no Luau runtime, no pipeline, nothing to compile.

    DISCORD_TOKEN=...  DEOBF_API=https://your-deobf-server  python main.py

See README.md for hosting, and .env.example for every setting.
"""
import asyncio
import io
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import time
from urllib.parse import urlparse, urlunparse

try:
    import aiohttp
    import discord
except ImportError as e:                             # a clearer message than the traceback
    raise SystemExit("missing dependency: %s\n  pip install -r requirements.txt" % e.name)

log = logging.getLogger("deobf.bot")
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- settings


def load_env(path=os.path.join(HERE, ".env")):
    """Read KEY=VALUE lines from .env into os.environ.

    Hand-rolled so the bot needs no extra dependency, and deliberately
    non-overriding: a variable already set by the host (Wispbyte's panel,
    Docker, systemd) wins over the file.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


def _ids(name):
    raw = os.environ.get(name, "") or ""
    return {int(p) for p in raw.replace(",", " ").split() if p.isdigit()}


class Settings:
    def __init__(self):
        load_env()
        self.token = (os.environ.get("DISCORD_TOKEN") or "").strip()
        self.api = (os.environ.get("DEOBF_API") or "http://127.0.0.1:8000").rstrip("/")
        # only useful if the server sits behind something that checks it
        self.api_token = (os.environ.get("DEOBF_API_TOKEN") or "").strip()
        self.prefix = os.environ.get("BOT_PREFIX", ".")

        self.allowed_guilds = _ids("ALLOWED_GUILDS")
        self.allowed_channels = _ids("ALLOWED_CHANNELS")
        self.owner_ids = _ids("OWNER_IDS")

        self.max_input_bytes = _int("MAX_INPUT_BYTES", 12 * 1024 * 1024)
        self.job_timeout = _int("JOB_TIMEOUT", 1800)
        self.cooldown = _int("USER_COOLDOWN", 10)
        self.max_inflight = _int("MAX_INFLIGHT", 4)

        self.run_timeout = _int("DEOB_TIMEOUT", 90)
        self.run_budget = _int("DEOB_BUDGET", 30)
        self.executor = os.environ.get("DEOB_EXECUTOR", "Wave")
        self.devirt = _bool("DEVIRT", True)

        self.max_upload_bytes = _int("MAX_UPLOAD_BYTES", 9 * 1024 * 1024)
        self.preview_lines = _int("PREVIEW_LINES", 18)
        self.attach_log = _bool("ATTACH_LOG", True)
        self.embed_colour = int(os.environ.get("EMBED_COLOUR", "0x5865F2"), 0)

    def allows(self, message):
        if self.owner_ids and message.author.id in self.owner_ids:
            return True
        gid = getattr(message.guild, "id", None)
        if self.allowed_guilds and gid not in self.allowed_guilds:
            return False
        if self.allowed_channels and message.channel.id not in self.allowed_channels:
            return False
        return True


# ---------------------------------------------------------------- the server

# The server's own stage names (jobs.STAGES), in the order a run goes through
# them, with what to call them in the embed.
STAGES = [("detect", "Detecting"), ("trace", "Running the script"),
          ("rerun", "Following the payload"), ("lift", "Lifting the bytecode"),
          ("polish", "Cleaning up"), ("done", "Writing the result")]
STAGE_LABELS = dict(STAGES)
STAGE_ORDER = [label for _, label in STAGES]

# traceout.header's first line. A result that starts with it is the behaviour
# trace, whatever was asked for: the server's plugin has no lifter for that VM,
# or lifting was tried and fell back.
TRACE_MARK = "(dynamic trace)"

API_TIMEOUT = 60            # per request, except the progress stream
SSE_IDLE = 120              # no event at all for this long means something broke


class ApiError(Exception):
    """Something the user should see, already worded for them."""


class Api:
    """The deobf server's HTTP API, one session for the bot's lifetime."""

    def __init__(self, settings):
        self.cfg = settings
        self.base = settings.api
        self._session = None
        self.limits = {}

    def headers(self):
        h = {"User-Agent": "deobf-bot (+https://github.com/riftwarewtf/tttt)"}
        if self.cfg.api_token:
            h["Authorization"] = "Bearer " + self.cfg.api_token
        return h

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers())
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def unreachable(self, exc):
        return ApiError(
            "The deobf server at `%s` is not answering (%s).\n"
            "Start one (`python web/server.py` in riftwarewtf/deobf, or its "
            "Dockerfile) and point `DEOBF_API` at it." % (self.base, type(exc).__name__))

    async def _json(self, method, path, payload=None, timeout=API_TIMEOUT):
        s = await self.session()
        try:
            async with s.request(method, self.base + path, json=payload,
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                body = await r.text()
                if r.status >= 400:
                    raise ApiError(self._why(r.status, body))
                return json.loads(body) if body else {}
        except ApiError:
            raise
        except asyncio.TimeoutError:
            raise ApiError("The deobf server took longer than %ds to answer." % timeout)
        except aiohttp.ClientError as e:
            raise self.unreachable(e)

    @staticmethod
    def _why(status, body):
        """The server's own reason for refusing, or the status."""
        try:
            detail = json.loads(body).get("detail")
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, list) and detail:            # FastAPI validation
            detail = detail[0].get("msg")
        if status == 429:
            return str(detail or "The deobf server is rate limiting this bot; try again in a minute.")
        return "The deobf server answered HTTP %d: %s" % (status, detail or body[:200] or "no reason given")

    async def health(self):
        h = await self._json("GET", "/api/health", timeout=20)
        self.limits = h.get("limits") or {}
        return h

    async def submit(self, text, name, options):
        return await self._json("POST", "/api/jobs",
                                {"source": text, "name": name, "options": options})

    async def job(self, jid):
        return await self._json("GET", "/api/jobs/" + jid)

    async def cancel(self, jid):
        try:
            await self._json("DELETE", "/api/jobs/" + jid, timeout=20)
        except ApiError:
            pass

    async def result(self, jid):
        s = await self.session()
        try:
            async with s.get("%s/api/jobs/%s/result" % (self.base, jid),
                             timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as r:
                body = await r.text()
                if r.status >= 400:
                    raise ApiError(self._why(r.status, body))
                return body
        except ApiError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            raise self.unreachable(e)

    async def log_tail(self, jid, lines=60):
        try:
            out = await self._json("GET", "/api/jobs/%s/log" % jid, timeout=20)
        except ApiError:
            return []
        return (out.get("lines") or [])[-lines:]

    async def follow(self, jid):
        """Yield the job's `state` dicts as the server reports them.

        The server streams server-sent events: `log` lines, `state` whenever
        the job's info changes, and one `end`. Only the states matter here -
        the log is fetched in one go if the run fails.
        """
        s = await self.session()
        url = "%s/api/jobs/%s/events" % (self.base, jid)
        timeout = aiohttp.ClientTimeout(total=None, sock_read=SSE_IDLE)
        try:
            async with s.get(url, timeout=timeout) as r:
                if r.status >= 400:
                    raise ApiError(self._why(r.status, await r.text()))
                event, data = None, []
                async for raw in r.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                    elif not line:                     # blank line ends an event
                        if event in ("state", "end") and data:
                            try:
                                yield json.loads("".join(data))
                            except ValueError:
                                pass
                        if event == "end":
                            return
                        event, data = None, []
        except ApiError:
            raise
        except asyncio.TimeoutError:
            raise ApiError("The deobf server stopped sending progress for %ds." % SSE_IDLE)
        except aiohttp.ClientError as e:
            raise self.unreachable(e)


# ---------------------------------------------------------------- the input

USER_AGENT = "deobf-bot (+https://github.com/riftwarewtf/tttt)"
FETCH_TIMEOUT = 30
CHUNK = 64 * 1024
URL_RE = re.compile(r"https?://\S+", re.I)


class FetchError(Exception):
    """A bad link, a file too big, a host that will not be fetched."""


def find_url(text):
    m = URL_RE.search(text or "")
    return m.group(0).strip("<>,;'\"") if m else None


def raw_url(url):
    """The raw-text form of a paste link, or the URL unchanged.

    Every rule only rewrites a path that is unmistakably a paste id on that
    host, so a link already pointing at raw text is left alone.
    """
    u = urlparse(url)
    host = u.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = u.path

    def repath(new_host, new_path):
        return urlunparse(("https", new_host, new_path, "", u.query, ""))

    if host == "pastebin.com" and re.fullmatch(r"/[A-Za-z0-9]{6,12}", path):
        return repath("pastebin.com", "/raw" + path)
    if host == "github.com" and "/blob/" in path:
        return repath("raw.githubusercontent.com", path.replace("/blob/", "/", 1))
    if host == "gist.github.com" and re.fullmatch(r"/[^/]+/[0-9a-f]{8,}", path):
        return repath("gist.githubusercontent.com", path + "/raw")
    if host == "rentry.co" and re.fullmatch(r"/[A-Za-z0-9_-]{2,}", path):
        return repath("rentry.co", path + "/raw")
    if host in ("hastebin.com", "hasteb.in", "paste.ee", "dpaste.org", "bin.disroot.org") \
            and re.fullmatch(r"/[A-Za-z0-9]{3,}", path):
        return repath(host, "/raw" + path)
    if host == "paste.mozilla.org" and re.fullmatch(r"/[A-Za-z0-9]{4,}", path):
        return repath(host, path + "/raw/")
    if host.endswith("pastefy.app") and re.fullmatch(r"/[A-Za-z0-9]{4,}", path):
        return repath(host, "/api/v2/paste" + path + "/raw")
    if host == "sourceb.in" and re.fullmatch(r"/[A-Za-z0-9]{4,}", path):
        return repath("sourceb.in", "/api/bins" + path)
    return url


async def _public_host(host):
    """Refuse a link that points back at the machine or the private network.

    Anyone who can type in the channel can hand the bot a URL, so it must not
    be usable to read things only this host can reach.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise FetchError("that host does not resolve: `%s`" % host[:100])
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise FetchError("that link points at a private address, which the bot will not fetch")


async def fetch(url, max_bytes):
    """Download a link as bytes, capped at `max_bytes`."""
    url = raw_url(url)
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise FetchError("only http(s) links are supported")
    if not u.netloc:
        raise FetchError("that does not look like a link")
    await _public_host(u.netloc.split(":")[0].strip("[]"))

    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/plain, */*"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(url, allow_redirects=True, max_redirects=5) as r:
                if r.status != 200:
                    raise FetchError("the link answered HTTP %d" % r.status)
                length = r.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise FetchError("that file is %s, the limit is %s"
                                     % (human(int(length)), human(max_bytes)))
                buf = bytearray()
                async for chunk in r.content.iter_chunked(CHUNK):
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise FetchError("that file is larger than %s" % human(max_bytes))
                return bytes(buf), name_from(url)
    except asyncio.TimeoutError:
        raise FetchError("the link timed out after %ds" % FETCH_TIMEOUT)
    except aiohttp.ClientError as e:
        raise FetchError("could not fetch that link (%s)" % type(e).__name__)


def name_from(url):
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "paste"
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", tail)[:60] or "paste"
    return tail if tail.lower().endswith((".lua", ".luau", ".txt")) else tail + ".lua"


def safe_name(name):
    """A file name safe to show and to send back, keeping the extension."""
    name = os.path.basename(name or "script.lua").replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip(".") or "script.lua"
    if not re.search(r"\.(lua|luau|txt)$", name, re.I):
        name += ".lua"
    return name[-80:]


# ---------------------------------------------------------------- the embed

OK_COLOUR = 0x2ECC71
BAD_COLOUR = 0xED4245
WARN_COLOUR = 0xFEE75C

DONE_MARK = "\N{WHITE HEAVY CHECK MARK}"
NOW_MARK = "\N{HOURGLASS WITH FLOWING SAND}"
TODO_MARK = "\N{WHITE SMALL SQUARE}"
FAIL_MARK = "\N{CROSS MARK}"

EDIT_INTERVAL = 2.0          # seconds between edits of one message
TICK = 5.0                   # how often the elapsed clock is refreshed
PREVIEW_CHARS = 1000


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def took(seconds):
    if seconds < 60:
        return "%.1fs" % seconds
    return "%dm %02ds" % (int(seconds) // 60, int(seconds) % 60)


def checklist(stage, failed=False):
    """The stage list, with everything before the current one ticked off."""
    here = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
    out = []
    for i, name in enumerate(STAGE_ORDER):
        if i < here:
            out.append("%s %s" % (DONE_MARK, name))
        elif i == here:
            out.append("%s **%s**" % (FAIL_MARK if failed else NOW_MARK, name))
        else:
            out.append("%s %s" % (TODO_MARK, name))
    return "\n".join(out)


class Job:
    """One request: what was asked, and the latest the server has said."""

    def __init__(self, data, name, devirt, requester):
        self.data = data
        self.name = safe_name(name)
        self.devirt = devirt
        self.requester = requester
        self.id = None
        self.stage = "Queued"
        self.detected = None            # the server's label, with the version
        self.confidence = None
        self.queued_behind = 0
        self.result = None
        self.lifted = None
        self.error = None
        self.started = time.monotonic()
        self.server_elapsed = None

    @property
    def elapsed(self):
        if self.server_elapsed is not None:
            return self.server_elapsed
        return time.monotonic() - self.started

    def update(self, state):
        """Take in one `state` event from the server."""
        self.id = state.get("id", self.id)
        self.detected = state.get("detected") or self.detected
        if state.get("confidence") is not None:
            self.confidence = state["confidence"]
        self.queued_behind = state.get("queued_behind") or 0
        if state.get("elapsed") is not None:
            self.server_elapsed = state["elapsed"]
        status = state.get("status")
        stage = state.get("stage")
        if status == "queued":
            self.stage = "Queued"
        elif stage in STAGE_LABELS:
            self.stage = STAGE_LABELS[stage]
        elif stage in ("starting", "queued"):
            self.stage = "Queued" if stage == "queued" else "Starting"
        if state.get("error"):
            self.error = state["error"]


def base_embed(job, colour):
    e = discord.Embed(colour=colour, timestamp=discord.utils.utcnow())
    e.title = "Deobfuscating `%s`" % job.name
    e.set_footer(text="requested by %s" % job.requester.display_name,
                 icon_url=getattr(job.requester.display_avatar, "url", None))
    return e


def source_fields(e, job):
    lines = job.data.count(b"\n") + 1
    e.add_field(name="Input", value="%s\n%s lines" % (human(len(job.data)), f"{lines:,}"))
    if job.detected:
        conf = "" if job.confidence is None else "\n%d%% match" % round(job.confidence * 100)
        e.add_field(name="Obfuscator", value="%s%s" % (job.detected, conf))
    else:
        e.add_field(name="Obfuscator", value="looking\N{HORIZONTAL ELLIPSIS}")


def progress_embed(job, settings):
    e = base_embed(job, settings.embed_colour)
    if job.stage == "Queued":
        e.description = ("Waiting for a free slot on the server%s."
                         % (" - %d ahead" % job.queued_behind if job.queued_behind else ""))
    else:
        e.description = checklist(job.stage)
    source_fields(e, job)
    e.add_field(name="Mode", value="Devirtualize" if job.devirt else "Trace")
    e.add_field(name="Elapsed", value=took(job.elapsed), inline=False)
    return e


def finished_embed(job, settings, preview, truncated):
    e = base_embed(job, OK_COLOUR)
    e.title = "Deobfuscated `%s`" % job.name
    source_fields(e, job)
    e.add_field(name="Mode", value="Devirtualized" if job.lifted else "Behaviour trace")
    out_lines = job.result.count("\n") + 1
    e.add_field(name="Output", value="%s\n%s lines"
                % (human(len(job.result.encode("utf-8", "replace"))), f"{out_lines:,}"))
    e.add_field(name="Took", value=took(job.elapsed))
    e.add_field(name="\N{ZERO WIDTH SPACE}", value="\N{ZERO WIDTH SPACE}")
    if preview:
        e.description = "```lua\n%s\n```" % preview
    if truncated:
        e.description = (e.description or "") + \
            "\n-# The attachment is truncated: the full result is over the upload limit."
    if job.devirt and not job.lifted:
        # asked for lifting and got a trace: say so, the two read very
        # differently - a trace has only the branches that actually ran
        e.description = (e.description or "") + \
            "\n-# Lifting did not apply to this script, so this is the behaviour trace: " \
            "only the branches that ran."
    return e


def failed_embed(job, settings, reason):
    e = base_embed(job, BAD_COLOUR)
    e.title = "Could not deobfuscate `%s`" % job.name
    e.description = checklist(job.stage, failed=True)
    source_fields(e, job)
    e.add_field(name="Mode", value="Devirtualize" if job.devirt else "Trace")
    e.add_field(name="Why", value="```\n%s\n```" % str(reason)[:1000], inline=False)
    e.add_field(name="Took", value=took(job.elapsed))
    return e


def notice(text, colour=WARN_COLOUR, title=None):
    e = discord.Embed(colour=colour, description=text)
    if title:
        e.title = title
    return e


class Editor:
    """Edits one message, no faster than Discord is happy to be edited.

    An edit landing inside the interval is remembered and sent when the
    interval is up, so the embed never falls behind and the bot never gets
    rate limited for it either.
    """

    def __init__(self, message, interval=EDIT_INTERVAL):
        self.message = message
        self.interval = interval
        self.last = 0.0
        self.pending = None

    async def show(self, embed, force=False, **kw):
        now = time.monotonic()
        if not force and now - self.last < self.interval:
            self.pending = (embed, kw)
            return
        self.pending = None
        self.last = now
        try:
            await self.message.edit(embed=embed, **kw)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------- the bot

FLAG_TRACE = re.compile(r"(?:^|\s)--(?:trace|no-?devirt)(?=\s|$)", re.I)
FLAG_OBF = re.compile(r"(?:^|\s)--(?:obf|obfuscator)[= ]([a-z0-9_]+)", re.I)
LOG_TAIL = 60

HELP = """\
**`{p}deobf`** - deobfuscate a protected Roblox Luau script.

Give it the script in any of these ways:
* attach the file to the message
* paste a link: `{p}deobf https://pastebin.com/AbCd1234`
* reply to a message that has the file or the link, and just say `{p}deobf`

The obfuscator is detected automatically, Luraph down to the version it says it
is. Luraph v14/v15 and IronBrew 1 are lifted back to real Luau (control flow,
locals, closures, untaken branches); Luraph up to v13 and everything else get a
behaviour trace: what the script does, written back as Luau.

**Options**
`--trace` - skip lifting and only trace, which is much faster
`--obf NAME` - force a plugin instead of detecting (`luraph`, `ironbrew1`, `generic`)

Limit: {mb} per script. The work runs on a deobf server, which never fetches
anything the script asks for - a `HttpGet` is recorded, not followed.
"""


class Bot(discord.Client):
    def __init__(self, settings):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg = settings
        self.api = Api(settings)
        self.cooldowns = {}
        self.inflight = 0

    async def setup_hook(self):
        """Say up front whether the server is there, rather than per request."""
        try:
            h = await self.api.health()
        except ApiError as e:
            log.warning("%s", e)
            return
        limit = (h.get("limits") or {}).get("max_upload")
        if limit:
            # the server decides what it accepts; do not promise more
            self.cfg.max_input_bytes = min(self.cfg.max_input_bytes, int(limit))
        names = ", ".join(p.get("label", p.get("name", "?")) for p in h.get("obfuscators") or [])
        log.info("deobf server %s: luau=%s, plugins: %s", self.cfg.api, h.get("luau"), names)
        if not h.get("luau"):
            log.warning("the server has no Luau runtime: every job there will fail "
                        "(python deobf/build_luau.py --portable)")

    async def close(self):
        await self.api.close()
        await super().close()

    async def on_ready(self):
        log.info("logged in as %s (%d guild(s)), prefix %r",
                 self.user, len(self.guilds), self.cfg.prefix)
        await self.change_presence(activity=discord.Game(
            name="%sdeobf a script" % self.cfg.prefix))

    async def on_message(self, message):
        if message.author.bot or not message.content:
            return
        p = self.cfg.prefix
        if not message.content.startswith(p):
            return
        word, _, rest = message.content[len(p):].partition(" ")
        word = word.lower()
        if word in ("deobf", "deobfuscate", "deob"):
            if self.cfg.allows(message):
                await self.handle(message, rest.strip())
        elif word in ("help", "deobfhelp", "commands"):
            if self.cfg.allows(message):
                await message.reply(embed=notice(
                    HELP.format(p=p, mb=human(self.cfg.max_input_bytes)),
                    colour=self.cfg.embed_colour, title="deobf"), mention_author=False)
        elif word == "status":
            if self.cfg.allows(message):
                await self.status(message)

    # -- the command ---------------------------------------------------

    def _cooling(self, user_id):
        if not self.cfg.cooldown or user_id in self.cfg.owner_ids:
            return 0
        left = self.cooldowns.get(user_id, 0) - time.monotonic()
        return max(0, int(left + 0.999))

    async def status(self, message):
        try:
            h = await self.api.health()
        except ApiError as e:
            await message.reply(embed=notice(str(e), BAD_COLOUR), mention_author=False)
            return
        q = h.get("queue") or {}
        # The server has no authentication of its own, so its address is only
        # shown to whoever runs the bot, not to the channel.
        owner = message.author.id in self.cfg.owner_ids
        where = "`%s`" % self.cfg.api if owner else "(hidden)"
        await message.reply(embed=notice(
            "**Server** %s\nLuau runtime: %s\nQueue: %s running, %s waiting "
            "(capacity %s)\nUpload limit: %s\nIn flight from this bot: %d"
            % (where, "yes" if h.get("luau") else "**no**",
               q.get("running", "?"), q.get("queued", "?"), q.get("capacity", "?"),
               human(int((h.get("limits") or {}).get("max_upload") or 0)), self.inflight),
            self.cfg.embed_colour, title="deobf server"), mention_author=False)

    async def handle(self, message, args):
        wait = self._cooling(message.author.id)
        if wait:
            await message.reply(embed=notice(
                "Give it %ds - one script at a time per person." % wait),
                mention_author=False, delete_after=10)
            return
        if self.inflight >= self.cfg.max_inflight:
            await message.reply(embed=notice(
                "This bot already has %d jobs on the server. Try again in a minute."
                % self.inflight), mention_author=False)
            return

        devirt = self.cfg.devirt and not FLAG_TRACE.search(args)
        forced = FLAG_OBF.search(args)
        args = FLAG_OBF.sub("", FLAG_TRACE.sub("", args)).strip()

        try:
            data, name = await self.collect(message, args)
        except FetchError as e:
            await message.reply(embed=notice(str(e), BAD_COLOUR), mention_author=False)
            return
        if data is None:
            await message.reply(embed=notice(
                "Attach the script, or give me a link to it. `%shelp` for the details."
                % self.cfg.prefix), mention_author=False)
            return
        if not data.strip():
            await message.reply(embed=notice("That file is empty.", BAD_COLOUR),
                                mention_author=False)
            return

        self.cooldowns[message.author.id] = time.monotonic() + self.cfg.cooldown
        job = Job(data, name, devirt, message.author)
        options = {"timeout": self.cfg.run_timeout, "budget": self.cfg.run_budget,
                   "executor": self.cfg.executor, "no_devirt": not devirt}
        if forced:
            options["obfuscator"] = forced.group(1).lower()
        self.inflight += 1
        try:
            await self.drive(message, job, options)
        finally:
            self.inflight -= 1

    async def collect(self, message, args):
        """The script to work on: (bytes, file name), or (None, None)."""
        limit = self.cfg.max_input_bytes
        replied = await self.replied_to(message)
        for m, text in ((message, args), (replied, replied.content if replied else "")):
            if m is None:
                continue
            for att in m.attachments:
                if att.size > limit:
                    raise FetchError("`%s` is %s, the limit is %s"
                                     % (att.filename, human(att.size), human(limit)))
                return await att.read(), att.filename
            url = find_url(text)
            if url:
                return await fetch(url, limit)
        return None, None

    async def replied_to(self, message):
        ref = message.reference
        if ref is None:
            return None
        if isinstance(ref.resolved, discord.Message):
            return ref.resolved
        try:
            return await message.channel.fetch_message(ref.message_id)
        except (discord.HTTPException, AttributeError):
            return None

    # -- one job, one embed --------------------------------------------

    async def drive(self, message, job, options):
        try:
            card = await message.reply(embed=progress_embed(job, self.cfg),
                                       mention_author=False)
        except discord.HTTPException:
            return
        editor = Editor(card)
        ticker = asyncio.create_task(self._tick(editor, job))
        try:
            await self._run(editor, job, options)
        except ApiError as e:
            job.error = str(e)
        except asyncio.CancelledError:
            job.error = "the bot was shutting down"
            if job.id:
                await self.api.cancel(job.id)
            raise
        except Exception as e:                       # noqa: BLE001 - shown in the embed
            log.exception("job failed")
            job.error = "%s: %s" % (type(e).__name__, e)
        finally:
            ticker.cancel()
        await self.deliver(editor, job)

    async def _run(self, editor, job, options):
        """Submit the script and follow the server until the job ends."""
        # latin-1 so every byte survives the JSON round trip: the server
        # re-encodes the string the same way deob.py reads a file
        text = job.data.decode("latin-1")
        state = await self.api.submit(text, job.name, options)
        job.update(state)
        await editor.show(progress_embed(job, self.cfg))

        # The server sends a state roughly three times a second while a job
        # runs, so the deadline below is checked often enough without a timer.
        deadline = time.monotonic() + self.cfg.job_timeout
        final, late = None, False
        events = self.api.follow(job.id)
        try:
            async for st in events:
                job.update(st)
                await editor.show(progress_embed(job, self.cfg))
                if st.get("status") in ("done", "failed", "cancelled"):
                    final = st
                    break
                if time.monotonic() > deadline:
                    late = True
                    break
        finally:
            # closes the streaming response; the generator is inside its
            # `async with`, so letting it be collected later would leak it
            await events.aclose()
        if late:
            await self.api.cancel(job.id)
            raise ApiError("the job passed this bot's %ds limit and was cancelled"
                           % self.cfg.job_timeout)
        if final is None:
            # the stream ended without a terminal state: ask outright
            final = await self.api.job(job.id)
            job.update(final)
        if final.get("status") != "done":
            raise ApiError(final.get("error") or "the server reported: %s"
                           % final.get("status", "no result"))
        job.result = await self.api.result(job.id)
        job.lifted = TRACE_MARK not in job.result.split("\n", 1)[0]

    async def _tick(self, editor, job):
        """Refresh the embed while nothing else happens, for the clock."""
        try:
            while True:
                await asyncio.sleep(TICK)
                await editor.show(progress_embed(job, self.cfg))
        except asyncio.CancelledError:
            pass

    async def deliver(self, editor, job):
        if job.result is None:
            files = []
            if self.cfg.attach_log and job.id:
                tail = "\n".join(await self.api.log_tail(job.id, LOG_TAIL))
                if tail:
                    files.append(discord.File(io.BytesIO(tail.encode("utf-8", "replace")),
                                              filename="pipeline.log"))
            await self._final(editor, failed_embed(job, self.cfg,
                                                   job.error or "unknown failure"), files)
            return

        blob = job.result.encode("utf-8", "replace")
        truncated = False
        if len(blob) > self.cfg.max_upload_bytes:
            blob = blob[:self.cfg.max_upload_bytes].rsplit(b"\n", 1)[0] + \
                b"\n-- [truncated: the full result is larger than Discord allows]\n"
            truncated = True
        out_name = re.sub(r"(\.luau?|\.txt)?$", ".deobf.luau", job.name, count=1)
        files = [discord.File(io.BytesIO(blob), filename=out_name)]
        await self._final(editor, finished_embed(job, self.cfg,
                                                 self.preview(job.result), truncated), files)

    def preview(self, text):
        """The first few lines, if they are short enough to read in an embed."""
        n = self.cfg.preview_lines
        if n <= 0:
            return None
        lines = text.splitlines()
        head = lines[:n]
        body = "\n".join(head)
        if len(body) > PREVIEW_CHARS:
            body = body[:PREVIEW_CHARS].rsplit("\n", 1)[0]
            head = body.splitlines()
        if not body.strip():
            return None
        if len(lines) > len(head):
            body += "\n-- ... %s more lines in the file" % f"{len(lines) - len(head):,}"
        return body

    async def _final(self, editor, embed, files):
        """Last edit: attachments can only go on the message, so replace them."""
        try:
            await editor.message.edit(embed=embed, attachments=files)
            return
        except discord.HTTPException:
            pass
        # an edit carrying attachments can be refused on its own (an upload
        # limit, a permission), so the embed and the file go separately
        for f in files:
            f.reset(seek=True)
        try:
            await editor.message.edit(embed=embed)
            if files:
                await editor.message.channel.send(files=files)
        except discord.HTTPException:
            log.warning("could not deliver the result")


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    if not settings.token:
        raise SystemExit(
            "No bot token.\n"
            "  Put DISCORD_TOKEN=... in a .env file next to main.py (copy .env.example),\n"
            "  or set it as an environment variable in your host's panel.")
    log.info("deobf server: %s", settings.api)
    Bot(settings).run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
