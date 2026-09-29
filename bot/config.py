"""Settings, read from the environment (and from a .env file next to bot.py).

Everything has a working default except the token, so a fresh clone runs as
soon as DISCORD_TOKEN is set. See .env.example for the full list.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(ROOT, ".env")


def load_env(path=ENV_FILE):
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
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


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
    """A set of snowflakes from a comma/space separated variable."""
    raw = os.environ.get(name, "") or ""
    return {int(p) for p in raw.replace(",", " ").split() if p.isdigit()}


class Settings:
    def __init__(self):
        load_env()
        self.token = (os.environ.get("DISCORD_TOKEN") or "").strip()
        self.prefix = os.environ.get("BOT_PREFIX", ".")

        # who may use it: empty means everyone, everywhere
        self.allowed_guilds = _ids("ALLOWED_GUILDS")
        self.allowed_channels = _ids("ALLOWED_CHANNELS")
        self.owner_ids = _ids("OWNER_IDS")

        # how much work is accepted
        self.max_concurrency = _int("MAX_CONCURRENCY", 2)
        self.max_queue = _int("MAX_QUEUE", 8)
        self.max_input_bytes = _int("MAX_INPUT_BYTES", 12 * 1024 * 1024)
        self.job_timeout = _int("JOB_TIMEOUT", 900)
        self.cooldown = _int("USER_COOLDOWN", 10)

        # what deob.py gets
        self.run_timeout = _int("DEOB_TIMEOUT", 90)
        self.run_budget = _int("DEOB_BUDGET", 30)
        self.executor = os.environ.get("DEOB_EXECUTOR", "Wave")
        self.devirt = _bool("DEVIRT", True)

        # what comes back
        self.max_upload_bytes = _int("MAX_UPLOAD_BYTES", 9 * 1024 * 1024)
        self.preview_lines = _int("PREVIEW_LINES", 18)
        self.attach_log = _bool("ATTACH_LOG", True)
        self.embed_colour = int(os.environ.get("EMBED_COLOUR", "0x5865F2"), 0)

    def allows(self, message):
        """Is this message somewhere the bot should answer?"""
        if self.owner_ids and message.author.id in self.owner_ids:
            return True
        gid = getattr(message.guild, "id", None)
        if self.allowed_guilds and gid not in self.allowed_guilds:
            return False
        if self.allowed_channels and message.channel.id not in self.allowed_channels:
            return False
        return True


SETTINGS = Settings()
