"""The one embed a request gets, from "queued" to the finished script.

Every stage edits the same message, so a request is one line in the channel
no matter how long it takes.
"""
import time

import discord

from .runner import STAGE_ORDER

OK = 0x2ECC71
BAD = 0xED4245
WARN = 0xFEE75C

DONE_MARK = "\N{WHITE HEAVY CHECK MARK}"
NOW_MARK = "\N{HOURGLASS WITH FLOWING SAND}"
TODO_MARK = "\N{WHITE SMALL SQUARE}"
FAIL_MARK = "\N{CROSS MARK}"

EDIT_INTERVAL = 2.0          # seconds between edits of the same message


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
    stages = list(STAGE_ORDER)
    if stage in ("Queued", "Starting"):
        here = -1
    elif stage in stages:
        here = stages.index(stage)
    else:
        here = 0
    lines = []
    for i, name in enumerate(stages):
        if i < here:
            lines.append("%s %s" % (DONE_MARK, name))
        elif i == here:
            lines.append("%s **%s**" % (FAIL_MARK if failed else NOW_MARK, name))
        else:
            lines.append("%s %s" % (TODO_MARK, name))
    return "\n".join(lines)


def _base(run, requester, colour):
    e = discord.Embed(colour=colour, timestamp=discord.utils.utcnow())
    e.title = "Deobfuscating `%s`" % run.name
    e.set_footer(text="requested by %s" % requester.display_name,
                 icon_url=getattr(requester.display_avatar, "url", None))
    return e


def _source_fields(e, run):
    lines = run.data.count(b"\n") + 1
    e.add_field(name="Input", value="%s\n%s lines" % (human(len(run.data)), f"{lines:,}"))
    if run.detected:
        conf = "" if run.confidence is None else "\n%d%% match" % round(run.confidence * 100)
        e.add_field(name="Obfuscator", value="%s%s" % (run.detected, conf))
    else:
        e.add_field(name="Obfuscator", value="looking\N{HORIZONTAL ELLIPSIS}")


def progress(run, requester, settings, ahead=0):
    e = _base(run, requester, settings.embed_colour)
    if run.stage == "Queued":
        e.description = ("Waiting for a free slot%s."
                         % (" - %d ahead" % ahead if ahead else ""))
    else:
        e.description = checklist(run.stage)
    _source_fields(e, run)
    e.add_field(name="Mode", value="Devirtualize" if run.devirt else "Trace")
    if run.started is not None:
        e.add_field(name="Elapsed", value=took(run.elapsed), inline=False)
    return e


def finished(run, requester, settings, preview, truncated):
    e = _base(run, requester, OK)
    e.title = "Deobfuscated `%s`" % run.name
    _source_fields(e, run)
    e.add_field(name="Mode",
                value="Devirtualized" if run.devirt else "Behaviour trace")
    out_lines = run.result.count("\n") + 1
    e.add_field(name="Output", value="%s\n%s lines"
                % (human(len(run.result.encode("utf-8", "replace"))), f"{out_lines:,}"))
    e.add_field(name="Took", value=took(run.elapsed))
    e.add_field(name="\N{ZERO WIDTH SPACE}", value="\N{ZERO WIDTH SPACE}")
    if preview:
        e.description = "```lua\n%s\n```" % preview
    if truncated:
        e.description = (e.description or "") + \
            "\n-# The attachment is truncated: the full result is over the upload limit."
    if not run.devirt and run.obfuscator != "generic":
        e.description = (e.description or "") + \
            "\n-# Lifting did not finish, so this is the behaviour trace: only the branches that ran."
    return e


def failed(run, requester, settings, reason):
    e = _base(run, requester, BAD)
    e.title = "Could not deobfuscate `%s`" % run.name
    e.description = checklist(run.stage, failed=True)
    _source_fields(e, run)
    e.add_field(name="Mode", value="Devirtualize" if run.devirt else "Trace")
    e.add_field(name="Why", value="```\n%s\n```" % reason[:1000], inline=False)
    if run.started is not None:
        e.add_field(name="Took", value=took(run.elapsed))
    return e


def notice(text, colour=WARN, title=None):
    e = discord.Embed(colour=colour, description=text)
    if title:
        e.title = title
    return e


class Editor:
    """Edits one message, no faster than Discord is happy to be edited.

    An edit that lands inside the interval is remembered and sent when the
    interval is up, so the embed never falls behind but the bot never gets
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
