#!/usr/bin/env python3
"""Tell the AI topic when a newer stable Hermes release is out (runs on the ENTRY host).

Hermes, the /ai backend, ran v0.18.2 from July until 2026-11-16 because
nothing said that a newer release existed. This oneshot (systemd timer,
daily) says so, once per version. Updating stays a manual procedure with
a stand run first: docs/runbooks/hermes_update.md.

  1) RUNNING VERSION. The binary hermes-api.service runs (the path= of its
     ExecStart) is asked for --version. The first line of its answer reads
     "Hermes Agent vX.Y.Z (...) · stable · local <sha>". The CLI's own
     "Up to date" line is not used: on 2026-11-16 it still said so
     12 minutes after v0.23.2 was published.
  2) NEWEST RELEASE. GET api.github.com/repos/NousResearch/hermes-agent/
     releases/latest, directly: entry reaches GitHub without the proxy.
     GitHub never answers there with a draft or a pre-release (the daily
     canaries are pre-releases). The version comes from the release NAME
     ("Hermes Agent v0.21.5 (v2026.9.24)"), the tag only when the name has
     none. From August to September 2026 the tags were dates, and
     v2026.9.24 read as a version is newer than anything, so a version
     whose first number looks like a year is refused.
  3) NOTICE. When the release is newer than the running version and than
     the last version announced, one message goes to TOPIC_AI. It carries
     the first line of the release notes, because the number alone says
     little: v0.23.2 calls itself a patch release and rolls up ~2,000 PRs.
     The path is hermes_model_guard.py's, whose helpers this file loads
     from its own directory: BOT_TOKEN from /opt/vpn-bot/.env, or the token
     the dashboard set, through that file's HTTPS_PROXY; never a PM. A
     version counts as announced only once Telegram took the message, so
     an undelivered notice goes out on the next run.

A run that cannot read one of the two versions decides nothing.
FAIL_NOTIFY_AFTER such runs in a row (a week of daily runs) post one
warning, once per streak; a good run ends the streak.

Usage:
    hermes_release_notice.py [--dry-run] [--state-dir DIR] [--bot-env F]
                             [--latest-url URL]

Exit codes: 0 = checked, 1 = a notice was due and Telegram did not take
it (the next run sends it), 2 = could not check.
"""

import argparse
import html
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple


def _load_guard():
    """hermes_model_guard.py from this file's directory (/usr/local/bin on
    entry, scripts/ in the repo), by path: sys.path is left alone."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermes_model_guard.py")
    spec = importlib.util.spec_from_file_location("hermes_model_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()

logger = logging.getLogger("hermes_release_notice")

REPO = "NousResearch/hermes-agent"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
USER_AGENT = "nekovpn-hermes-release-notice"
HERMES_UNIT = guard.HERMES_UNIT
BOT_ENV = guard.BOT_ENV
STATE_DIR = "/var/lib/hermes-release-notice"
RUNBOOK = "docs/runbooks/hermes_update.md"
HTTP_TIMEOUT = guard.HTTP_TIMEOUT
VERSION_TIMEOUT = 60
FAIL_NOTIFY_AFTER = 7
SUMMARY_MAX = 240
# A "version" whose first number is this big is a date (v2026.9.24).
CALENDAR_MAJOR = 1000

Version = Tuple[int, int, int]

_VERSION_IN_NAME = re.compile(r"\bv(\d+)\.(\d+)\.(\d+)\b")
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_RUNNING = re.compile(r"^Hermes Agent v(\d+)\.(\d+)\.(\d+)\b", re.M)
_EXEC_PATH = re.compile(r"\bpath=(/[^ ;]+)")


# --------------------------------------------------------------------------
# Pure core
# --------------------------------------------------------------------------

def _version(match) -> Optional[Version]:
    if match is None:
        return None
    version = tuple(int(x) for x in match.groups())
    return version if version[0] < CALENDAR_MAJOR else None


def tag_of(version: Version) -> str:
    return "v%d.%d.%d" % version


def parse_announced(value) -> Optional[Version]:
    return _version(_TAG.match(value)) if isinstance(value, str) else None


def running_from_output(output) -> Optional[Version]:
    """The version in `hermes --version`, from its "Hermes Agent vX.Y.Z" line."""
    return _version(_RUNNING.search(output or ""))


def exec_path(show_value) -> Optional[str]:
    """The binary in `systemctl show -p ExecStart --value` ("{ path=/… ; argv[]=… }")."""
    match = _EXEC_PATH.search(show_value or "")
    return match.group(1) if match else None


def published_text(value) -> str:
    try:
        ts = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""
    return ts.strftime("%d.%m.%Y %H:%M UTC")


def release_summary(body, limit: int = SUMMARY_MAX) -> str:
    """The first line of prose in the release notes, without markdown marks.

    Headings, HTML comments and the "Release Date" line are skipped; a
    longer line is cut at a word and ends with "…".
    """
    if not isinstance(body, str):
        return ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue
        line = re.sub(r"\*\*|__|`", "", line.lstrip("> ")).strip()
        if not line or line.lower().startswith("release date"):
            continue
        if len(line) > limit:
            line = line[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
        return line
    return ""


@dataclass(frozen=True)
class Release:
    version: Version
    url: str
    published: str
    summary: str

    @property
    def tag(self) -> str:
        return tag_of(self.version)


def release_from(payload) -> Tuple[Optional[Release], str]:
    """A Release from GitHub's /releases/latest answer, or (None, why)."""
    if not isinstance(payload, dict):
        return None, "GitHub ответил не объектом релиза"
    if payload.get("draft") or payload.get("prerelease"):
        return None, "GitHub отдал как последний релиз черновик или pre-release"
    name, tag = payload.get("name"), payload.get("tag_name")
    version = None
    if isinstance(name, str):
        for match in _VERSION_IN_NAME.finditer(name):
            version = _version(match)
            if version is not None:
                break
    if version is None and isinstance(tag, str):
        version = _version(_TAG.match(tag))
    if version is None:
        return None, "в последнем релизе нет версии vX.Y.Z (имя %r, тег %r)" % (
            str(name)[:80], str(tag)[:40])
    url = payload.get("html_url")
    if not (isinstance(url, str) and url.startswith("https://github.com/")):
        url = RELEASES_PAGE
    return Release(version, url, published_text(payload.get("published_at")),
                   release_summary(payload.get("body"))), ""


def format_notice(running: Version, release: Release) -> str:
    title = f"🆕 <b>Вышел Hermes {release.tag}</b>"
    if release.published:
        title += f" ({release.published})"
    lines = [title, f"На entry работает {tag_of(running)}."]
    if release.summary:
        lines.append(f"«{html.escape(release.summary)}»")
    lines.append(f'Заметки: <a href="{html.escape(release.url)}">{release.tag} на GitHub</a>')
    lines.append(f"Обновлять вручную, по <code>{RUNBOOK}</code>: сначала стенд.")
    return "\n".join(lines)


def format_failure(failures: int, why: str) -> str:
    return (f"⚠️ <b>Проверка релизов Hermes не сработала {failures} раз подряд</b>\n"
            f"Последняя причина: {html.escape(why)}\n"
            f"Лог: <code>journalctl -u hermes-release-notice</code>")


def decide(running: Version, release: Release, announced: Optional[Version]) -> Optional[str]:
    """The notice to post, or None: not newer than what runs, or announced already."""
    if release.version <= running:
        return None
    if announced is not None and release.version <= announced:
        return None
    return format_notice(running, release)


# --------------------------------------------------------------------------
# Thin I/O
# --------------------------------------------------------------------------

def load_state(state_dir: str) -> dict:
    path = os.path.join(state_dir, "state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            raise ValueError("state is not a dict")
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        logger.warning("state.json unreadable (%s) — starting fresh", type(exc).__name__)
        state = {}
    state.setdefault("announced", None)          # the last version announced, "vX.Y.Z"
    state.setdefault("failures", 0)              # runs in a row that could not check
    state.setdefault("failure_warned", False)    # this streak's warning went out
    if not isinstance(state["failures"], int) or isinstance(state["failures"], bool):
        state["failures"] = 0
    return state


class NoticeIO:
    """Everything that touches systemd, the Hermes binary or the network.

    Tests swap in a fake; the pure core never sees this class.
    """

    def __init__(self, gio, latest_url: str = LATEST_URL):
        self.gio = gio                  # hermes_model_guard.GuardIO: session + Telegram
        self.latest_url = latest_url

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def running_version(self) -> Tuple[Optional[Version], str]:
        try:
            show = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", HERMES_UNIT],
                                  capture_output=True, encoding="utf-8", errors="replace",
                                  timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"systemctl show {HERMES_UNIT}: {type(exc).__name__}"
        binary = exec_path(show.stdout) if show.returncode == 0 else None
        if not binary:
            return None, f"в ExecStart {HERMES_UNIT} нет пути к бинарю"
        try:
            proc = subprocess.run([binary, "--version"], capture_output=True, encoding="utf-8",
                                  errors="replace", timeout=VERSION_TIMEOUT,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"{binary} --version: {type(exc).__name__}"
        version = running_from_output(proc.stdout)
        if version is None:
            return None, (f"{binary} --version (код {proc.returncode}) "
                          f"без строки «Hermes Agent vX.Y.Z»")
        return version, ""

    def latest_release(self) -> Tuple[Optional[Release], str]:
        try:
            r = self.gio.session.get(self.latest_url, timeout=HTTP_TIMEOUT, headers={
                "Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:  # noqa: BLE001 — any failure = unknown
            return None, f"GitHub: {type(exc).__name__}: {guard.redact(str(exc))[:200]}"
        return release_from(payload)

    def send_telegram(self, text: str) -> bool:
        return self.gio.send_telegram(text)


def build_io(bot_env: str, latest_url: str = LATEST_URL,
             bot_token_override: Optional[str] = None) -> NoticeIO:
    """The guard's Telegram path, without its OpenRouter key: GitHub needs none."""
    b = guard.parse_env_file(bot_env, ("BOT_TOKEN", "FORUM_GROUP_ID", "TOPIC_AI", "HTTPS_PROXY"))
    token = guard.effective_bot_token(b.get("BOT_TOKEN"), bot_token_override)
    for value in (b.get("BOT_TOKEN"), token, b.get("HTTPS_PROXY")):
        guard.register_secret(value)
    if not token or not b.get("FORUM_GROUP_ID"):
        logger.warning("BOT_TOKEN/FORUM_GROUP_ID missing in %s — notices disabled", bot_env)
    gio = guard.GuardIO("", None, token, b.get("FORUM_GROUP_ID"), b.get("TOPIC_AI"),
                        b.get("HTTPS_PROXY") or None)
    return NoticeIO(gio, latest_url)


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------

def run_once(io, state_dir: str, dry_run: bool) -> int:
    state = load_state(state_dir)
    running, why_running = io.running_version()
    release, why_release = io.latest_release()

    if running is None or release is None:
        why = why_running if running is None else why_release
        state["failures"] += 1
        logger.warning("could not check (%d in a row): %s", state["failures"], why)
        if state["failures"] >= FAIL_NOTIFY_AFTER and not state["failure_warned"]:
            text = format_failure(state["failures"], why)
            if dry_run:
                print("would send:\n" + text)
            elif io.send_telegram(text):
                state["failure_warned"] = True
        if not dry_run:
            guard.save_state(state_dir, state)
        return 2

    state["failures"] = 0
    state["failure_warned"] = False
    announced = parse_announced(state.get("announced"))
    text = decide(running, release, announced)
    logger.info("running %s, newest stable %s (%s), announced %s", tag_of(running), release.tag,
                release.published or "?", tag_of(announced) if announced else "nothing")

    if text is None:
        logger.info("nothing to announce")
        if not dry_run:
            guard.save_state(state_dir, state)
        return 0
    if dry_run:
        print("would send:\n" + text)
        return 0
    if io.send_telegram(text):
        state["announced"] = release.tag
        guard.save_state(state_dir, state)
        logger.info("announced %s", release.tag)
        return 0
    guard.save_state(state_dir, state)
    logger.error("notice for %s not delivered — the next run sends it", release.tag)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent; send nothing, write nothing")
    ap.add_argument("--state-dir", default=STATE_DIR)
    ap.add_argument("--bot-env", default=BOT_ENV)
    ap.add_argument("--latest-url", default=LATEST_URL,
                    help="where the newest release is read (tests point it at a local server)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    # Configured here, not at import time: tests import the module.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        return run_once(build_io(args.bot_env, args.latest_url), args.state_dir, args.dry_run)
    except OSError as exc:
        logger.error("cannot check: %s: %s", type(exc).__name__, guard.redact(str(exc)))
        return 2
    except Exception as exc:  # noqa: BLE001 — a crash is "could not check"
        logger.error("release notice crashed: %s: %s", type(exc).__name__,
                     guard.redact(str(exc)), exc_info=logger.isEnabledFor(logging.DEBUG))
        return 2


if __name__ == "__main__":
    sys.exit(main())
