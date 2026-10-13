# obsidian-ticket-llm

Write a ticket in an Obsidian note, get an LLM-generated answer in a separate note.

Any block starting with a `#ticket-<name>` line is sent to the configured model, and the
answer is written to `<answers_dir>/ticket-<name>.md`. A block ends at the first blank line.

```markdown
#ticket-login
Login returns a 500 after yesterday's deploy.
Only happens for SSO users.
```

Features:

- Only new or changed blocks are regenerated (hashes are kept in `.ticket-hashes.json`).
  Deleting an answer file also triggers regeneration.
- Tickets in the same note are processed concurrently.
- Changes inside the answers folder, `.obsidian`, `.trash` and `.git` are ignored.
- Duplicate ticket names in a note produce a warning; only the first block is used.
- Supports Anthropic, Ollama, and a `mock` provider for testing without API calls.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/DavideRuperto/obsiadian-note-llm-generation.git
cd obsiadian-note-llm-generation
uv sync
cp config.example.toml config.toml
```

Edit `config.toml` and set at least `vault_path`. Then pick a provider:

- `mock`: writes a placeholder answer after one second. Good for checking your setup.
- `anthropic`: set the `ANTHROPIC_API_KEY` environment variable.
- `ollama`: have Ollama running and the model pulled (`ollama pull llama3.1`).

If `uv sync` fails with certificate errors (common behind corporate proxies), run
`uv sync --native-tls`. At runtime the script already uses the OS certificate store.

## Usage

```sh
uv run main.py                    # watch the vault, 2s debounce after each save
uv run main.py --once path/to/note.md   # process one note and exit
uv run main.py --config other.toml -v   # custom config, verbose logging
```

### Running from Obsidian

You don't need to keep the watcher running. With the
[Shell commands](https://github.com/Taitava/obsidian-shellcommands) plugin you can bind
`--once` to a hotkey and process the current note on demand.

Add a new shell command (Windows, CMD shell):

```
"C:\path\to\repo\.venv\Scripts\python.exe" "C:\path\to\repo\main.py" --once "{{file_path:absolute}}"
```

On macOS/Linux:

```
/path/to/repo/.venv/bin/python /path/to/repo/main.py --once "{{file_path:absolute}}"
```

Give it an alias in the command's settings, then assign a hotkey under
Settings → Hotkeys. Setting stderr to "Notification balloon" in the Output tab shows the
log as a popup.

Obsidian autosaves about two seconds after you stop typing, so if you trigger the command
right after editing, it may read the previous version of the note.
