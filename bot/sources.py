"""Getting the script to deobfuscate: a Discord attachment or a link.

Links to the usual paste sites are rewritten to their raw form, so
`.deobf https://pastebin.com/AbCd1234` works as well as the /raw/ URL.
"""
import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse, urlunparse

import aiohttp

USER_AGENT = "deobf-bot (+https://github.com/riftwarewtf/tttt)"
FETCH_TIMEOUT = 30
CHUNK = 64 * 1024

URL_RE = re.compile(r"https?://\S+", re.I)


class FetchError(Exception):
    """Something the user should see: a bad link, too big, host unreachable."""


def find_url(text):
    m = URL_RE.search(text or "")
    return m.group(0).strip("<>,;'\"") if m else None


def raw_url(url):
    """The raw-text form of a paste link, or the URL unchanged.

    Every rule here only rewrites a path that is unmistakably a paste id on
    that host, so a link that already points at raw text is left alone.
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
    """Reject a link that points back at the machine or the private network.

    Anyone who can type in the channel can hand the bot a URL, so it must not
    be usable to read things only the host can reach.
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
    """A file name for a downloaded paste."""
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "paste"
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", tail)[:60] or "paste"
    return tail if tail.lower().endswith((".lua", ".luau", ".txt")) else tail + ".lua"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
