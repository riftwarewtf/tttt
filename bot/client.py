"""The Discord side: one command, one embed, edited until the script is out.

    .deobf <attachment or paste link> [--trace] [--obf NAME]

Everything heavy happens in runner.py; this file only decides what to accept,
keeps the embed in step with the run, and sends the result back.
"""
import asyncio
import io
import logging
import os
import re
import time

import discord

from . import embeds, sources
from .config import SETTINGS
from .runner import PipelineError, Queue, Run, luau_ready

log = logging.getLogger("deobf.bot")

TICK = 5.0                      # how often the elapsed time is refreshed
PREVIEW_CHARS = 1000
LOG_TAIL = 60                   # lines of pipeline log attached on a failure

FLAG_TRACE = re.compile(r"(?:^|\s)--(?:trace|no-?devirt)(?=\s|$)", re.I)
FLAG_OBF = re.compile(r"(?:^|\s)--(?:obf|obfuscator)[= ]([a-z0-9_]+)", re.I)

HELP = """\
**`{p}deobf`** - deobfuscate a protected Roblox Luau script.

Give it the script in any of these ways:
* attach the file to the message
* paste a link: `{p}deobf https://pastebin.com/AbCd1234`
* reply to a message that has the file or the link, and just say `{p}deobf`

The obfuscator is detected automatically. Luraph v15 and IronBrew 1 are lifted
back to real Luau (control flow, locals, closures, untaken branches); anything
else gets a behaviour trace: everything the script does, written back as Luau.

**Options**
`--trace` - skip lifting and only trace, which is much faster
`--obf NAME` - force a plugin instead of detecting (`luraph_v15`, `ironbrew1`, `generic`)

Limit: {mb} per script. Nothing is uploaded anywhere - the script runs locally \
against a fake Roblox environment.
"""


class Bot(discord.Client):
    def __init__(self, settings=SETTINGS):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg = settings
        self.queue = Queue(settings)
        self.cooldowns = {}

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
        body = message.content[len(p):]
        word, _, rest = body.partition(" ")
        word = word.lower()
        if word in ("deobf", "deobfuscate", "deob"):
            if not self.cfg.allows(message):
                return
            await self.handle(message, rest.strip())
        elif word in ("help", "deobfhelp", "commands"):
            if not self.cfg.allows(message):
                return
            await message.reply(embed=embeds.notice(
                HELP.format(p=p, mb=embeds.human(self.cfg.max_input_bytes)),
                colour=self.cfg.embed_colour, title="deobf"),
                mention_author=False)

    # -- the command -------------------------------------------------------

    def _cooling(self, user_id):
        """Seconds the user still has to wait, 0 if they may go ahead."""
        if not self.cfg.cooldown or user_id in self.cfg.owner_ids:
            return 0
        left = self.cooldowns.get(user_id, 0) - time.monotonic()
        return max(0, int(left + 0.999))

    async def handle(self, message, args):
        wait = self._cooling(message.author.id)
        if wait:
            await message.reply(embed=embeds.notice(
                "Give it %ds - one script at a time per person." % wait),
                mention_author=False, delete_after=10)
            return
        if self.queue.full():
            await message.reply(embed=embeds.notice(
                "The queue is full (%d waiting). Try again in a minute."
                % self.queue.depth), mention_author=False)
            return

        devirt = self.cfg.devirt and not FLAG_TRACE.search(args)
        forced = FLAG_OBF.search(args)
        args = FLAG_OBF.sub("", FLAG_TRACE.sub("", args)).strip()

        try:
            data, name = await self.collect(message, args)
        except sources.FetchError as e:
            await message.reply(embed=embeds.notice(str(e), embeds.BAD),
                                mention_author=False)
            return
        if data is None:
            await message.reply(embed=embeds.notice(
                "Attach the script, or give me a link to it. `%shelp` for the details."
                % self.cfg.prefix), mention_author=False)
            return
        if not data.strip():
            await message.reply(embed=embeds.notice("That file is empty.", embeds.BAD),
                                mention_author=False)
            return
        if not luau_ready():
            await message.reply(embed=embeds.notice(
                "The Luau runtime is missing from `deobf/bin/`. Build it with "
                "`python deobf/build_luau.py --portable`.", embeds.BAD), mention_author=False)
            return

        self.cooldowns[message.author.id] = time.monotonic() + self.cfg.cooldown
        run = Run(data, name, self.cfg, forced=forced.group(1).lower() if forced else None)
        run.devirt = devirt
        await self.drive(message, run)

    async def collect(self, message, args):
        """The script to work on: (bytes, file name), or (None, None)."""
        limit = self.cfg.max_input_bytes
        replied = await self.replied_to(message)
        for m, text in ((message, args), (replied, replied.content if replied else "")):
            if m is None:
                continue
            for att in m.attachments:
                if att.size > limit:
                    raise sources.FetchError(
                        "`%s` is %s, the limit is %s"
                        % (att.filename, embeds.human(att.size), embeds.human(limit)))
                return await att.read(), att.filename
            url = sources.find_url(text)
            if url:
                return await sources.fetch(url, limit)
        return None, None

    async def replied_to(self, message):
        ref = message.reference
        if ref is None:
            return None
        if ref.resolved is not None and isinstance(ref.resolved, discord.Message):
            return ref.resolved
        try:
            return await message.channel.fetch_message(ref.message_id)
        except (discord.HTTPException, AttributeError):
            return None

    # -- the embed ---------------------------------------------------------

    async def drive(self, message, run):
        """Post the embed, run the job, keep the embed in step, send the result."""
        first = embeds.progress(run, message.author, self.cfg, ahead=self.queue.depth)
        try:
            card = await message.reply(embed=first, mention_author=False)
        except discord.HTTPException:
            return
        editor = embeds.Editor(card)

        async def on_progress(r):
            await editor.show(embeds.progress(r, message.author, self.cfg))

        run.on_progress = on_progress
        ticker = asyncio.create_task(self._tick(editor, run, message.author))
        try:
            await self.queue.submit(run)
        except PipelineError as e:
            run.error = str(e)
        except Exception as e:                       # noqa: BLE001 - reported in the embed
            log.exception("run failed")
            run.error = "%s: %s" % (type(e).__name__, e)
        finally:
            ticker.cancel()
        await self.deliver(editor, run, message.author)

    async def _tick(self, editor, run, requester):
        """Refresh the embed while nothing else is happening, for the clock."""
        try:
            while True:
                await asyncio.sleep(TICK)
                await editor.show(embeds.progress(run, requester, self.cfg,
                                                  ahead=self.queue.waiting))
        except asyncio.CancelledError:
            pass

    async def deliver(self, editor, run, requester):
        if run.result is None:
            embed = embeds.failed(run, requester, self.cfg,
                                  run.error or "unknown failure")
            files = []
            if self.cfg.attach_log and run.log:
                tail = "\n".join(run.log[-LOG_TAIL:])
                files.append(discord.File(io.BytesIO(tail.encode("utf-8", "replace")),
                                          filename="pipeline.log"))
            await self._final(editor, embed, files)
            return

        blob = run.result.encode("utf-8", "replace")
        truncated = False
        if len(blob) > self.cfg.max_upload_bytes:
            blob = blob[:self.cfg.max_upload_bytes].rsplit(b"\n", 1)[0] + \
                b"\n-- [truncated: the full result is larger than Discord allows]\n"
            truncated = True
        out_name = re.sub(r"(\.luau?|\.txt)?$", ".deobf.luau", run.name, count=1)
        files = [discord.File(io.BytesIO(blob), filename=out_name)]
        embed = embeds.finished(run, requester, self.cfg,
                                self.preview(run.result), truncated)
        await self._final(editor, embed, files)

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
        # An edit that carries attachments can be refused on its own (an
        # upload limit, a permission), so the embed and the file go separately.
        for f in files:
            f.reset(seek=True)
        try:
            await editor.message.edit(embed=embed)
            if files:
                await editor.message.channel.send(files=files)
        except discord.HTTPException:
            log.warning("could not deliver the result")


def run():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not SETTINGS.token:
        raise SystemExit(
            "No bot token.\n"
            "  Put DISCORD_TOKEN=... in a .env file next to bot.py (copy .env.example),\n"
            "  or set it as an environment variable in your host's panel.")
    if not luau_ready():
        log.warning("deobf/bin/luau and deobf/bin/luau-ast are missing: "
                    "every job will fail until they are built "
                    "(python deobf/build_luau.py --portable)")
    Bot().run(SETTINGS.token, log_handler=None)
