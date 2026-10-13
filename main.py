"""Watch an Obsidian vault and generate LLM answers for `#ticket-<name>` blocks.

Usage:
    uv run main.py                  # watch mode
    uv run main.py --once note.md   # process a single file and exit
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import truststore
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

DEBOUNCE_SECONDS = 2.0
TICKET_RE = re.compile(r"^#ticket-([\w-]+)\s*$")
IGNORED_DIRS = {".obsidian", ".trash", ".git"}

log = logging.getLogger("ticket-watch")

# Use the OS certificate store (needed behind TLS-inspecting proxies/antivirus)
truststore.inject_into_ssl()


@dataclass
class Config:
    vault: Path
    answers_dir: Path
    state_file: Path
    provider: str
    model: str
    system_prompt: str
    max_tokens: int
    ollama_host: str

    @classmethod
    def load(cls, path: Path) -> Config:
        with path.open("rb") as f:
            raw = tomllib.load(f)

        vault = Path(raw["vault_path"]).expanduser().resolve()
        answers = Path(raw.get("answers_dir", "ticket-answers")).expanduser()
        if not answers.is_absolute():
            answers = vault / answers
        state = Path(raw.get("state_file", ".ticket-hashes.json")).expanduser()
        if not state.is_absolute():
            state = path.parent / state

        provider = raw.get("provider", "anthropic").lower()
        if provider not in ("anthropic", "ollama", "mock"):
            raise ValueError(f"unsupported provider: {provider!r}")

        return cls(
            vault=vault,
            answers_dir=answers.resolve(),
            state_file=state.resolve(),
            provider=provider,
            model=raw.get("model", "mock"),
            system_prompt=raw.get("system_prompt", "").strip(),
            max_tokens=int(raw.get("max_tokens", 4096)),
            ollama_host=raw.get("ollama_host", "http://localhost:11434").rstrip("/"),
        )


@dataclass
class Ticket:
    name: str
    text: str

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def parse_tickets(content: str, source: Path) -> list[Ticket]:
    """Extract `#ticket-<name>` blocks, from the header line up to the first blank line."""
    tickets: dict[str, Ticket] = {}
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        match = TICKET_RE.match(lines[i])
        if not match:
            i += 1
            continue
        name = match.group(1)
        start = i
        i += 1
        while i < len(lines) and lines[i].strip():
            i += 1
        if name in tickets:
            log.warning(
                "%s: duplicate ticket '%s' (line %d), keeping the first one",
                source.name, name, start + 1,
            )
            continue
        tickets[name] = Ticket(name, "\n".join(lines[start:i]))
    return list(tickets.values())


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if cfg.provider == "anthropic":
            from anthropic import AsyncAnthropic

            self._anthropic = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
        elif cfg.provider == "ollama":
            self._http = httpx.AsyncClient(base_url=cfg.ollama_host, timeout=600)

    async def complete(self, prompt: str) -> str:
        if self.cfg.provider == "mock":
            await asyncio.sleep(1)
            # Skip the #ticket-<name> line so Obsidian doesn't pick it up as a tag here too
            body = "\n".join(f"> {line}" for line in prompt.splitlines()[1:])
            return f"**[MOCK]** Placeholder answer for:\n\n{body}"

        if self.cfg.provider == "anthropic":
            kwargs = {"system": self.cfg.system_prompt} if self.cfg.system_prompt else {}
            resp = await self._anthropic.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            )
            return "".join(b.text for b in resp.content if b.type == "text")

        messages = []
        if self.cfg.system_prompt:
            messages.append({"role": "system", "content": self.cfg.system_prompt})
        messages.append({"role": "user", "content": prompt})
        resp = await self._http.post(
            "/api/chat",
            json={"model": self.cfg.model, "messages": messages, "stream": False},
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    async def aclose(self) -> None:
        if self.cfg.provider == "anthropic":
            await self._anthropic.close()
        elif self.cfg.provider == "ollama":
            await self._http.aclose()


class Processor:
    def __init__(self, cfg: Config, llm: LLM):
        self.cfg = cfg
        self.llm = llm
        self.state: dict[str, dict] = self._load_state()

    def _load_state(self) -> dict[str, dict]:
        try:
            return json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            log.warning("corrupted state file, starting fresh: %s", self.cfg.state_file)
            return {}

    def _save_state(self) -> None:
        self.cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cfg.state_file)

    def answer_path(self, name: str) -> Path:
        return self.cfg.answers_dir / f"ticket-{name}.md"

    def is_relevant(self, path: Path) -> bool:
        """Return True for .md notes inside the vault and outside the answers folder."""
        if path.suffix.lower() != ".md":
            return False
        try:
            path = path.resolve()
        except OSError:
            return False
        if path.is_relative_to(self.cfg.answers_dir):
            return False
        if not path.is_relative_to(self.cfg.vault):
            return False
        rel = path.relative_to(self.cfg.vault)
        return not any(part in IGNORED_DIRS for part in rel.parts)

    async def process_file(self, path: Path) -> None:
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except UnicodeDecodeError:
            log.warning("%s: not UTF-8, skipped", path)
            return

        rel_source = path.resolve().relative_to(self.cfg.vault).as_posix()
        todo = []
        for t in parse_tickets(content, path):
            prev = self.state.get(t.name)
            if prev and prev.get("source") != rel_source:
                log.warning(
                    "ticket '%s' was defined in '%s', now overwritten by '%s'",
                    t.name, prev.get("source"), rel_source,
                )
            if prev and prev.get("hash") == t.hash and self.answer_path(t.name).exists():
                continue
            todo.append(t)

        if not todo:
            log.info("%s: no new or changed tickets", rel_source)
            return

        log.info("%s: processing %d ticket(s): %s", rel_source, len(todo), ", ".join(t.name for t in todo))
        await asyncio.gather(*(self._run_ticket(t, rel_source) for t in todo))

    async def _run_ticket(self, ticket: Ticket, rel_source: str) -> None:
        try:
            answer = await self.llm.complete(ticket.text)
        except Exception as e:
            # One failing ticket shouldn't take down the others
            log.error("ticket '%s' failed: %s", ticket.name, e)
            return

        out = self.answer_path(ticket.name)
        out.parent.mkdir(parents=True, exist_ok=True)
        model = "mock" if self.cfg.provider == "mock" else self.cfg.model
        header = (
            "---\n"
            f"ticket: {ticket.name}\n"
            f"source: \"[[{rel_source.removesuffix('.md')}]]\"\n"
            f"generated: {datetime.now().isoformat(timespec='seconds')}\n"
            f"model: {model}\n"
            "---\n\n"
        )
        tmp = out.with_suffix(".tmp")
        tmp.write_text(header + answer.strip() + "\n", encoding="utf-8")
        tmp.replace(out)

        self.state[ticket.name] = {"hash": ticket.hash, "source": rel_source}
        self._save_state()
        log.info("ticket '%s' -> %s", ticket.name, out)


class DebouncedHandler(FileSystemEventHandler):
    """Forward watchdog events to the asyncio loop, debounced per file."""

    def __init__(self, loop: asyncio.AbstractEventLoop, processor: Processor):
        self.loop = loop
        self.processor = processor
        self.timers: dict[Path, asyncio.TimerHandle] = {}
        self.running: set[Path] = set()
        self.dirty: set[Path] = set()

    # Called from the watchdog thread

    def on_modified(self, event: FileSystemEvent) -> None:
        self._dispatch(event.src_path, event.is_directory)

    def on_created(self, event: FileSystemEvent) -> None:
        self._dispatch(event.src_path, event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._dispatch(event.dest_path, event.is_directory)

    def _dispatch(self, raw_path, is_dir: bool) -> None:
        if is_dir:
            return
        path = Path(raw_path if isinstance(raw_path, str) else raw_path.decode())
        if self.processor.is_relevant(path):
            self.loop.call_soon_threadsafe(self._schedule, path.resolve())

    # Called on the event loop

    def _schedule(self, path: Path) -> None:
        if (timer := self.timers.pop(path, None)) is not None:
            timer.cancel()
        self.timers[path] = self.loop.call_later(DEBOUNCE_SECONDS, self._fire, path)

    def _fire(self, path: Path) -> None:
        self.timers.pop(path, None)
        if path in self.running:
            self.dirty.add(path)  # rerun once the current pass finishes
            return
        self.loop.create_task(self._run(path))

    async def _run(self, path: Path) -> None:
        self.running.add(path)
        try:
            await self.processor.process_file(path)
        except Exception:
            log.exception("error while processing %s", path)
        finally:
            self.running.discard(path)
        if path in self.dirty:
            self.dirty.discard(path)
            self.loop.create_task(self._run(path))


async def watch(processor: Processor) -> None:
    loop = asyncio.get_running_loop()
    handler = DebouncedHandler(loop, processor)
    observer = Observer()
    observer.schedule(handler, str(processor.cfg.vault), recursive=True)
    observer.start()
    log.info("watching %s (answers in %s), press Ctrl+C to stop", processor.cfg.vault, processor.cfg.answers_dir)
    try:
        await asyncio.Event().wait()
    finally:
        observer.stop()
        observer.join()


async def amain(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    if not cfg.vault.is_dir():
        log.error("vault not found: %s", cfg.vault)
        return 1

    llm = LLM(cfg)
    processor = Processor(cfg, llm)
    try:
        if args.once:
            path = args.once.expanduser().resolve()
            if not path.is_file():
                log.error("file not found: %s", path)
                return 1
            if not processor.is_relevant(path):
                log.error("not a vault note, or inside the answers folder: %s", path)
                return 1
            await processor.process_file(path)
        else:
            await watch(processor)
    finally:
        await llm.aclose()
    return 0


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", type=Path, metavar="FILE", help="process this file only, then exit")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.toml"),
        help="path to config.toml (default: next to this script)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        sys.exit(asyncio.run(amain(args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
