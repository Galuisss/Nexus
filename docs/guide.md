# User guide

## Startup and access

The control machine needs Python 3.11+ and [uv](https://docs.astral.sh/uv/). Execution machines need Codex CLI 0.169.0+ with OAuth configured.

```bash
uv sync
uv run nexus serve --data-dir .nexus
```

Open <http://127.0.0.1:8765>. The server listens on `0.0.0.0:8765` by default. Nexus is designed for a single owner and trusted devices; the network controls access to its endpoints.

Optional startup arguments:

- `--host 127.0.0.1`: allow local access only.
- `--tailscale-only`: listen on Tailscale and loopback addresses; mutually exclusive with `--host`.
- `--allow-url https://nexus.example.com`: register a custom domain or reverse proxy URL; repeat as needed.
- `--demo --data-dir .nexus-demo --port 8766`: start an interactive demo.
- `--demo-config /path/to/scene.json`: start a demo with a custom scene.

## Connecting machines

The local machine is connected by default. Add remote machines in the web interface. The control machine needs non-interactive SSH access, and the remote machine needs Bash and a configured Codex installation. SSH uses the system configuration; hosts can be IP addresses, domain names, or SSH aliases.

You can also edit and import the [example configuration](../config.example.json). Machines with matching names are updated:

```bash
uv run nexus import-machines config.example.json
```

Use the + button beside a machine to add a project directory, then create or open a session. Removing a project stops tracking it and preserves its files, history, and running tasks. Unexpected disconnections retry automatically; manual disconnection stops retries.

## Starting at boot

On Linux, install a systemd user service as the regular user running Nexus:

```bash
uv run nexus service install --data-dir .nexus
uv run nexus service enable
```

`install` accepts the same network arguments. `enable` starts the service immediately and enables startup at boot. Some systems require `sudo loginctl enable-linger "$USER"` first.

Use the `uv run nexus service` subcommands `status` to check the service, `logs -f` to follow logs, and `disable` to stop it and disable startup at boot. After changing startup arguments, rerun `install`, then `restart`. Proxy and other environment settings live in `~/.config/nexus/service.env`.

## Mobile installation and notifications

Mobile installation and push notifications require a browser-trusted HTTPS URL. Use a reverse proxy or Tailscale Serve:

```bash
sudo tailscale serve --bg --yes http://127.0.0.1:8765
```

Open the HTTPS URL on your phone. On Android, choose **Install app**; on iPhone, choose **Add to Home Screen** from Safari's share menu. Keep Tailscale connected when using a Tailscale URL.

Enable completion notifications in **Settings → General**, then send a test notification. On iPhone and iPad, iOS/iPadOS 16.4+ is required; launch Nexus from the home screen before enabling notifications. The server needs access to browser push services and supports `HTTPS_PROXY`.

**Settings → Features → Tool call ID** is off by default. Its value is captured when creating a conversation and retained across resume and reconnection. Enabled conversations label tool returns with sequential call IDs and provide `read_original` for archived evidence. Forks and side conversations inherit the IDs present at their context boundary and continue from the next number.

Enabled conversations have a **Tool call compact** toggle in the composer. It starts off; new conversations use the last selected setting. Each conversation retains its own setting. Changes apply to subsequent returns, while an ongoing compact completes. Compaction considers original text of at least 2,048 tokens and keeps only useful facts from that return. Failed or token-growing projections keep the original. The transcript distinguishes compacted, kept original, failed fallback and token-growth fallback, with elapsed and final duration; keep and fallback outcomes display the original once.

## Everyday use

- Language: choose **English** or **简体中文** in **Settings → General → Language**. The choice is saved in this browser and takes effect immediately.
- Account limits: open **Account limits** at the bottom of the sidebar to view each OAuth account's five-hour and weekly quotas, reset times, and reset-credit count and expiration dates. Cards list associated machines; other authentication methods appear separately.
- Sessions: search, archive, edit history, fork, and regenerate. Messages sent during a run steer the active turn. Stop the current turn and background tasks separately.
- Side chats: use `/side question` or `/btw question` to start from the current context while the main session keeps running.
- Files and terminals: browse, edit, and download files; open interactive terminals from projects or sessions.
- Group chats: add sessions to a group to coordinate agents. Explicit agent group sends appear in the group timeline.

See the [UI specification](ui-spec.md) for detailed interaction behavior.

## CLI, API, and MCP

```bash
uv run nexus machines
uv run nexus threads --machine MACHINE_ID
```

Clients connect to `http://127.0.0.1:8765` by default. Use `--url` or `NEXUS_URL` to select another server. The HTTP API is at `/api`; Streamable HTTP MCP is at `/mcp`.

Connecting an SSH machine automatically configures its `nexus` MCP server through a reverse tunnel to the control machine. The remote SSH server must allow remote TCP forwarding, and port `18765` must be available. Other MCP clients can use the service URL:

```toml
[mcp_servers.nexus]
url = "http://127.0.0.1:8765/mcp"
```

Agents can manage tasks with `list_machines` → `create_thread` → `send_message` → `wait_for_thread`, and send messages to six-character session IDs or groups. See the [API and protocol contract](api-contract.md) for endpoints, tools, and events.

## Demo scenes and screenshots

[demo.json](../src/nexus/demo.json) defines the built-in scene; [demo.py](../src/nexus/demo.py) loads it and handles interactive behavior. Copy the JSON to customize machines, projects, sessions, messages, and approvals:

```bash
uv run nexus serve --demo-config /tmp/scene.json --data-dir .nexus-demo --port 8766
```

Each thread has a unique `key`, `machineId`, `name`, and `cwd`. Turns have a `key` and native Codex `items`. Thread `age` sets last activity; turn `age` sets completion time, or start time for running turns, in seconds before startup. Optional thread `settings` configure the model, effort, and service tier (`priority` enables Fast). `tokenUsage` supplies shared usage, with per-thread overrides. An optional thread `goal` sets the objective, status, token budget, and usage shown above the composer. Requests reference thread and turn keys. Incoming agent messages use `origin.delivery.senderThread` to reference the sender's key.

[capture-demo.py](../scripts/capture-demo.py) starts the same demo runtime, captures the real UI, and stops the server. Install Chromium once with `uv run --extra dev playwright install chromium`, then provide a separate capture configuration:

```json
{
  "scene": "scene.json",
  "thread": "main",
  "output": "workspace.png",
  "language": "en",
  "theme": "light",
  "hideDemoLabel": true
}
```

```bash
uv run --extra dev python scripts/capture-demo.py /tmp/capture.json
```

Paths resolve relative to the capture configuration. Omit `scene` to use the built-in scene. Optional `viewport`, `deviceScaleFactor`, `panes` (`sidebar`, `session`), and `searchMode` control the view. Add `frame` with `padding`, `radius` (pixels), and `background` (CSS) to use the [window frame](../scripts/demo-frame.html). Scene and capture configurations can stay outside the repository.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
```

Python uses Ruff with a 100-column line length and sorted imports. Run `uv run ruff check --fix .` and `uv run ruff format .` to apply the style.

After upgrading `openai-codex`, verify session creation, resume, steering, history editing, and approval replies with a real Codex runtime.

After changing Java in `src/nexus/android/`, rebuild the DEX with JDK 21 and Android SDK Platform 36 / Build Tools 36.0.0: `ANDROID_HOME=/path/to/sdk bash scripts/build-phone-numbers.sh`.

For API or protocol changes, deploy the backend before switching frontend assets, then verify page loading, requests, and reconnection in a real client. Keep the frontend entry point, module dependencies, and Service Worker resource versions in sync, and verify clients with existing caches.
