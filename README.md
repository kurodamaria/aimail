# aimail (曖昧 / AI-Mail)

> **A lightweight, local-first asynchronous messaging bus and task queue for autonomous AI agents and human operators.**

`aimail` provides a simple, robust, and completely serverless protocol for agents and humans to exchange structured tasks, status updates, and binary payloads across processes.

It pairs the transparency of human-readable Markdown files with the fast querying and atomic status tracking of an SQLite database.

---

## 💡 Why aimail?

Traditional inter-process or multi-agent communication architectures often rely on heavy infrastructure (Redis, RabbitMQ, HTTP servers) or ephemeral stdout pipes that vanish when a session ends.

`aimail` takes a **local-first, filesystem-native approach**:

- 📂 **Transparent & Inspectable:** Message contents are standard `.md` files in a folder. You can read, write, or inspect them directly in Finder, VS Code, Obsidian, or terminal.
- ⚡ **Zero-Daemon Overhead:** No running background server required. State is persisted in an embedded SQLite database (`aimail.db`).
- 🤖 **Multi-Agent Decoupled:** Senders simply drop messages with target receiver IDs. Agents process their inboxes asynchronously on demand.
- 📦 **Attachment Ready:** Direct support for paired binary archives (`.tar.xz`), code diffs, or artifacts alongside message instructions.
- 🔄 **Self-Healing Sync:** If a sender manually drops a file into the directory without using the CLI, `aimail sync` auto-discovers and registers it automatically.

---

## 📁 Directory Architecture

```text
Desktop/aimail/
├── markdown/        # Markdown message files (.md)
├── attachments/     # Binary payload archives (.tar.xz, datasets, etc.)
├── skills/          # Antigravity skill definitions (SKILL.md)
├── aimail.db        # SQLite message registry & user directory (git-ignored)
├── aimail.py        # Core CLI & Python API engine
└── README.md
```

### Naming Convention

Every message follows a deterministic naming convention:

```text
[sender-id]-[receiver-id]-[topic]-[timestamp].md
[sender-id]-[receiver-id]-[topic]-[timestamp].tar.xz   (optional attachment)
```

- **`sender-id`**: ID of the issuing human or agent (e.g. `kuro`, `researcher`, `orchestrator`).
- **`receiver-id`**: ID of the target recipient (e.g. `oschan`, `coder`, `reviewer`).
- **`topic`**: Purpose or action descriptor (e.g. `daily_sync`, `bugfix_task`, `deploy`).
- **`timestamp`**: Sorting timestamp formatted as `YYYYMMDD_HHMMSS`.

---

## 🗄️ Database Schema

The local SQLite registry (`aimail.db`) indexes and tracks message states:

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    message_timestamp TEXT NOT NULL,
    md_file_path TEXT NOT NULL UNIQUE,     -- e.g. "markdown/<filename>.md"
    attachment_path TEXT,                  -- e.g. "attachments/<filename>.tar.xz"
    status TEXT NOT NULL DEFAULT 'new'     -- 'new' | 'read' | 'replied'
        CHECK(status IN ('new', 'read', 'replied')),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_messages_inbox 
ON messages(receiver_id, status, message_timestamp);

-- User & Agent Capability Registry
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    keywords TEXT NOT NULL,                -- Comma-separated search tags
    registered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### State Machine

```
   ┌─────────┐       agent reads       ┌──────────┐      agent responds      ┌─────────────┐
   │   new   │  ────────────────────>  │   read   │  ─────────────────────>  │   replied   │
   └─────────┘                         └──────────┘                          └─────────────┘
```

---

## 🚀 Installation & Setup

`aimail` requires **Python 3.9+** and has **zero third-party dependencies** (uses only standard library modules `sqlite3`, `argparse`, `pathlib`, `shutil`).

To make `aimail` callable system-wide from any terminal directory:

```bash
# Clone or move to your desired workspace
cd ~/Desktop/aimail

# Make executable and symlink to your local bin
chmod +x aimail.py
mkdir -p ~/.local/bin
ln -sf ~/Desktop/aimail/aimail.py ~/.local/bin/aimail
```

---

## 💻 CLI Usage Guide

### 1. Sending Messages

#### Quick Inline String
Send a message directly from the command line:
```bash
aimail send --from kuro --to oschan --topic build_status -c "Alphaforge build passed all tests."
```

#### Markdown File
Copy an existing markdown file into the inbox:
```bash
aimail send --from architect --to oschan --topic sprint_plan -f ~/Documents/plan.md
```

#### With Attachment & Original Erase
Attach a project tarball and erase the source file after sending to prevent duplicate files:
```bash
aimail send \
  --from researcher \
  --to oschan \
  --topic train_weights \
  -f task.md \
  --attach checkpoints.tar.xz \
  --erase-original
```

---

### 2. Checking & Listing Messages

```bash
# View all unread messages for agent 'oschan'
aimail list --to oschan --status new

# View recent messages from a specific sender
aimail list --sender kuro --limit 10

# View all messages across all states
aimail list --status all
```

**Example Output:**
```text
ID   STATUS   SENDER       RECEIVER     TOPIC            TIMESTAMP          ATTACHMENT   FILE
-----------------------------------------------------------------------------------------------------------------
1    new      kuro         oschan       build_status     20261103_183045    -            markdown/kuro-oschan-build_status-20261103_183045.md
2    new      researcher   oschan       train_weights    20261103_183432    yes          markdown/researcher-oschan-train_weights-20261103_183432.md
```

---

### 3. Reading / Inspecting a Message

Inspect full metadata and preview the Markdown content:
```bash
aimail show 1
# Or fuzzy-search by topic/name:
aimail show train_weights
```

---

### 4. Updating Message Status

```bash
# Mark as read
aimail mark 1 --status read

# Mark as replied
aimail mark 1 --status replied
```

---

### 5. Manual Sync / Reconciliation

If messages were manually copied, dropped, or modified on disk:
```bash
aimail sync
```
*Note: `list` and `show` automatically trigger `sync` beforehand, so your query results are always fresh.*

---

### 6. Agent Directory & Discovery

`aimail` includes an agent capability registry so agents and operators know who is available and what they can do:

- **`kuro` (REAL HUMAN USER):** The human owner, developer, and operator. Messages to `kuro` are for human review, approvals, questions, or status updates.
- **`oschan` (AI ASSISTANT):** Autonomous background task execution, code review, Python, and testing.

```bash
# List all registered users/agents
aimail users

# Find agents matching a specific capability keyword
aimail user find "python"

# Show an agent's full profile
aimail user show oschan

# Register an agent with an active Antigravity conversation ID for automatic wakeup:
aimail user register --id oschan --desc "Task execution agent" --keywords "python, git" --conv-id "0ee17217-cb82-4133-b3f7-60642fb46600"

# Register an agent with a custom wakeup command (e.g. OpenCode, tmux, script):
aimail user register --id coder --desc "OpenCode agent" --keywords "code, refactor" --wakeup-cmd "opencode -s coder -p '{prompt}'"
```

---

### 7. Automatic Wakeup / Notifications

When a message is sent via `aimail send`, the recipient's wakeup handler is triggered automatically in a detached background process:

- **Antigravity Agents:** If `--conv-id` is set, `aimail` executes `agy --conversation <conv-id> -p "<prompt>"` in the background to wake the session non-interactively.
- **Custom / OpenCode Agents:** If `--wakeup-cmd` is set, `aimail` formats and executes the custom shell command (supports `{sender}`, `{receiver}`, `{topic}`, `{prompt}`, `{conv_id}`).
- **Human Operator (`kuro`):** Displays a native macOS desktop banner notification.
- **Skip Wakeup:** Pass `--no-wakeup` to `aimail send` to deposit the message silently without triggering the recipient.

---

## 🧠 Antigravity Skill Integration

`aimail` includes a native **Antigravity Skill** (`skills/aimail/SKILL.md`). Installing it allows any Antigravity CLI agent across any project to immediately understand and use `aimail` without prompt priming:

```bash
# Global install (available machine-wide to all Antigravity agents)
mkdir -p ~/.gemini/config/skills/aimail
cp ~/Desktop/aimail/skills/aimail/SKILL.md ~/.gemini/config/skills/aimail/
```

When an Antigravity agent encounters a task outside its workspace or needs to contact `kuro`, it automatically activates the skill, checks `aimail users`, and dispatches the task.

---

## 🤖 Agent Protocol Pattern (e.g. `oschan`)

When building autonomous agents that consume `aimail`:

1. **Check for Tasks:**
   ```bash
   aimail list --to <agent_id> --status new
   ```
2. **Read Content:**
   ```bash
   aimail show <message_id>
   ```
3. **Execute Actions:** Agent parses Markdown instructions and any extracted archive in `attachments/`.
4. **Update Status / Reply:**
   ```bash
   # Mark current task done
   aimail mark <message_id> --status replied

   # Send reply back to original sender
   aimail send --from <agent_id> --to <sender_id> --topic <topic>_reply -c "Task completed. Output saved."
   ```

---

## 🗺️ Roadmap & Feature Backlog

Features and architectural ideas ranked by implementation priority:

### 1. ⚡ Local Automatic Wakeup / Interrupt Vector (Priority: P0 - Implemented ✅)
- [x] **Headless Conversation Resume:** Added `conversation_id` and `wakeup_cmd` columns to `users` so `aimail send` automatically wakes up recipient agents via `agy --conversation <id> -p "..."` non-interactively.
- [x] **Human Notification Hook:** Messages addressed to human user `kuro` automatically trigger a native macOS desktop banner notification (`osascript -e 'display notification ...'`).
  - [ ] **Smartphone & Mobile Alerts (Telegram / ntfy / Pushover):** Provide first-class presets/recipes for human alerts (e.g. `wakeup_cmd` sending an instant push notification to your phone via Telegram Bot webhook or `ntfy.sh` so agents can page you when high-priority tasks finish).
- [ ] **Mailbox Watcher Daemon:** Optional lightweight background watcher (`fsevents` / `kqueue`) that monitors `markdown/` for newly arrived files and triggers recipient wakeups without manual human prompting.

### 2. 📢 Broadcast & Bounty Board (`@anyone`) (Priority: P1)
- [ ] **Broadcast Addressing:** Support sending unassigned tasks to `--to @anyone` or `--to @bounty`.
- [ ] **Atomic Quest Claiming:** Implement `aimail claim <id> --by <agent_id>` that atomically transitions status from `new` to `claimed` so multiple agents don't duplicate work.
- [ ] **Bounty Query Filter:** Add `aimail bounties` shortcut to easily inspect all unclaimed community tasks.

### 3. 🌐 Distributed Multi-Node Transport ("Mail Across Internet") (Priority: P2)
- [ ] **"Drop & Local Wake" Pattern:** Enable cross-machine message exchange (e.g. MacBook Air <-> Cloud GPU cluster) using secure, inert file synchronization (Tailscale SSH/rsync, Syncthing, or Git branches).
- [ ] **Zero Remote Attack Surface:** Network transport only writes passive files; each individual node's local `aimail` is strictly responsible for waking its own local agent instances with zero open execution ports.
- [ ] **Remote Node Configuration:** Simple CLI syntax to register remote nodes (e.g. `aimail send --to gpu_agent@cloud-rig ...`).

### 4. 🔌 Universal Agent Runtimes & Adapters (Priority: P3)
- [ ] **Multi-CLI Support:** Support non-`agy` agent runners (e.g. OpenCode, custom Python loops, or `tmux send-keys` for live visual panes).
- [ ] **Configurable Trigger Templates:** Allow each agent in the `users` table to define its own execution command template.

---

## 📄 License

MIT License. Feel free to adapt for personal multi-agent environments and workflows.
