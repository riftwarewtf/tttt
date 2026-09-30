"""Running the pipeline for one message.

One request = one `deob.py` subprocess in its own temp folder. The pipeline
keeps global state (the Path2D cache and the last raw run), so it must never
be imported into this long-lived process - see CLAUDE.md, "Usage".

Everything here is asyncio: the subprocess is read line by line, each line is
matched against the pipeline's own progress messages, and a callback is told
whenever the stage changes so the embed can be edited.
"""
import asyncio
import os
import re
import shutil
import signal
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEOB = os.path.join(ROOT, "deobf", "deob.py")
BIN = os.path.join(ROOT, "deobf", "bin")

# The pipeline prints `[*] ...` as it goes; these map those lines to the
# stages shown in the embed. First match wins, so the order is the order of
# the run.
STAGES = [
    ("Running the script", re.compile(r"^\[\*\] (tracing|replaying|\d+ Path2D)")),
    ("Following the payload", re.compile(r"^\[\*\] (script loadstring'd|anti-tamper|the script never finished)")),
    ("Lifting the bytecode", re.compile(r"^\[\*\] devirtualiz")),
    ("Lifting the bytecode", re.compile(r"^\[\*\] (constant|devirt round|  )")),
    ("Cleaning up", re.compile(r"^\[\*\] (patched|\d+ flattened)")),
    ("Writing the result", re.compile(r"^\[\+\] result:")),
]

STAGE_ORDER = ["Detecting", "Running the script", "Following the payload",
               "Lifting the bytecode", "Cleaning up", "Writing the result"]

MAX_LOG_LINES = 2000

# traceout.header's first line. A result that starts with it is the behaviour
# trace, whatever was asked for: the plugin has no lifter for that VM, or
# lifting was tried and fell back. The output itself is the only honest answer,
# so the embed reads it rather than repeating the request.
TRACE_MARK = "(dynamic trace)"


class PipelineError(Exception):
    """A run that produced no output, with the reason to show the user."""


def luau_ready():
    exe = ".exe" if os.name == "nt" else ""
    return all(os.path.exists(os.path.join(BIN, n + exe)) for n in ("luau", "luau-ast"))


def safe_name(name):
    """A file name safe to write into a temp folder, keeping the extension."""
    name = os.path.basename(name or "script.lua").replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip(".") or "script.lua"
    if not re.search(r"\.(lua|luau|txt)$", name, re.I):
        name += ".lua"
    return name[-80:]


async def _run(argv, cwd=ROOT, timeout=60):
    """Run a command, return (exit code, merged output)."""
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=env, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        await proc.wait()
        raise PipelineError("detection timed out")
    return proc.returncode, out.decode("utf-8", "replace")


def _kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:                                # noqa: BLE001 - already gone
        try:
            proc.kill()
        except Exception:                            # noqa: BLE001
            pass


async def detect(data, name="script.lua"):
    """`deob.py --detect`: {obfuscator, confidence, label}."""
    tmp = tempfile.mkdtemp(prefix="deobdet_")
    try:
        path = os.path.join(tmp, safe_name(name))
        with open(path, "wb") as f:
            f.write(data)
        code, out = await _run([sys.executable, DEOB, path, "--detect"], timeout=90)
        if code != 0:
            raise PipelineError((out or "detection failed").strip()[-300:])
        parts = out.strip().splitlines()[-1].split("\t")
        if len(parts) != 3:
            raise PipelineError("unexpected detector output: " + out.strip()[:200])
        return {"obfuscator": parts[0],
                "confidence": None if parts[1] == "forced" else float(parts[1]),
                "label": parts[2]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class Run:
    """One deobfuscation, from bytes in to Luau text out."""

    def __init__(self, data, name, settings, on_progress=None, forced=None):
        self.data = data
        self.forced = forced
        self.name = safe_name(name)
        self.cfg = settings
        self.on_progress = on_progress
        self.stage = "Queued"
        self.detected = None            # human label
        self.obfuscator = None          # plugin name
        self.confidence = None
        self.result = None              # deobfuscated text
        self.lifted = None              # did the result come from the lifter?
        self.log = []
        self.error = None
        self.exit_code = None
        self.started = None
        self.finished = None
        self.devirt = settings.devirt
        self._proc = None
        self._workdir = None

    @property
    def elapsed(self):
        if self.started is None:
            return 0.0
        return (self.finished or time.monotonic()) - self.started

    async def _set_stage(self, stage):
        if stage == self.stage:
            return
        self.stage = stage
        if self.on_progress:
            await self.on_progress(self)

    def _append(self, line):
        if self._workdir:
            line = line.replace(os.path.join(self._workdir, "result"), "output")
            line = line.replace(self._workdir + os.sep, "").replace(self._workdir, "")
        if len(self.log) < MAX_LOG_LINES:
            self.log.append(line)
        elif len(self.log) == MAX_LOG_LINES:
            self.log.append("[!] log truncated")

    async def go(self):
        self.started = time.monotonic()
        try:
            if self.forced:
                self.obfuscator = self.forced
                self.detected = self.forced + " (forced)"
            else:
                await self._set_stage("Detecting")
                info = await detect(self.data, self.name)
                self.obfuscator = info["obfuscator"]
                self.detected = info["label"]
                self.confidence = info["confidence"]
            # The generic plugin has no lifter, so devirtualizing is not a
            # choice there: it would spend the whole timeout to end up with
            # the same trace.
            if self.obfuscator == "generic":
                self.devirt = False
            await self._set_stage("Starting")
            await self._pipeline()
        except PipelineError as e:
            self.error = str(e)
        except asyncio.CancelledError:
            self.error = "cancelled"
            if self._proc:
                _kill(self._proc)
            raise
        except Exception as e:                       # noqa: BLE001 - shown to the user
            self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            self.finished = time.monotonic()
        return self

    async def _pipeline(self):
        workdir = tempfile.mkdtemp(prefix="deobbot_")
        self._workdir = workdir
        try:
            in_path = os.path.join(workdir, self.name)
            out_path = os.path.join(workdir, "result", self.name)
            with open(in_path, "wb") as f:
                f.write(self.data)
            argv = [sys.executable, DEOB, in_path, "-o", out_path,
                    "--obfuscator", self.obfuscator,
                    "--timeout", str(self.cfg.run_timeout),
                    "--budget", str(self.cfg.run_budget),
                    "--executor", self.cfg.executor]
            if not self.devirt:
                argv.append("--no-devirt")
            await self._stream(argv)
            if os.path.exists(out_path):
                with open(out_path, encoding="utf-8", errors="replace") as f:
                    self.result = f.read()
                self.lifted = TRACE_MARK not in self.result.split("\n", 1)[0]
            else:
                raise PipelineError(self._guess_error())
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            self._workdir = None

    async def _stream(self, argv):
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self._proc = await asyncio.create_subprocess_exec(
            *argv, cwd=ROOT, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env, start_new_session=True, limit=1024 * 1024)
        deadline = time.monotonic() + self.cfg.job_timeout
        timed_out = False
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), left)
            except asyncio.TimeoutError:
                timed_out = True
                break
            except ValueError:
                # a single line longer than the stream limit: keep going
                self._append("[!] a very long output line was dropped")
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            self._append(line)
            await self._note(line)
        if timed_out:
            _kill(self._proc)
            await self._proc.wait()
            raise PipelineError("the run hit the %ds limit" % self.cfg.job_timeout)
        self.exit_code = await self._proc.wait()

    async def _note(self, line):
        for stage, rx in STAGES:
            if rx.match(line):
                await self._set_stage(stage)
                return

    def _guess_error(self):
        for line in reversed(self.log):
            if line.startswith("[!]"):
                return line[4:].strip()
        return "the pipeline produced no output (exit %s)" % self.exit_code


class Queue:
    """A bounded work queue: a few runs at a time, the rest wait their turn."""

    def __init__(self, settings):
        self.cfg = settings
        self.sem = asyncio.Semaphore(settings.max_concurrency)
        self.waiting = 0
        self.running = 0

    @property
    def depth(self):
        return self.waiting + self.running

    def full(self):
        return self.depth >= self.cfg.max_queue

    async def submit(self, run):
        self.waiting += 1
        try:
            await self.sem.acquire()
        finally:
            # whether the wait finished or was cancelled, it is over
            self.waiting -= 1
        self.running += 1
        try:
            return await run.go()
        finally:
            self.running -= 1
            self.sem.release()
