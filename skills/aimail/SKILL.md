---
name: aimail
description: >-
  Use this skill to communicate asynchronously with other AI agents or the real human user (kuro)
  via the aimail messaging system. Activate this skill whenever you need to delegate tasks to
  another specialized agent, check your own agent inbox for incoming tasks, send completion notices,
  or discover which agents exist and what capabilities they offer.
---

# aimail: Asynchronous Multi-Agent Communication

`aimail` is a local-first message bus and task queue for AI agents and the human operator (`kuro`). It enables decoupled collaboration across different workspaces and processes using Markdown task instructions and paired binary attachments.

> [!IMPORTANT]
> **`aimail` is a pre-installed, standalone system CLI utility in `$PATH`.**
> - **DO NOT** inspect, browse, locate, or run `ls` on internal system directories or mailbox files.
> - **DO NOT** read, audit, or edit internal database files or implementation scripts.
> - Treat `aimail` strictly as an opaque black-box command-line tool (like `git` or `curl`).
> - Interact **exclusively** through documented `aimail` CLI subcommands (`aimail list`, `aimail show`, `aimail send`, `aimail reply`, `aimail close`, `aimail download`, `aimail extract`, `aimail revoke`, `aimail users`).

---

## 👥 Understanding Users in aimail

Always query the user directory before dispatching messages:

- **`kuro` (REAL HUMAN USER):** `kuro` is the human developer, owner, and operator.  
  - Send messages to `kuro` when you need human review, feedback, architectural decisions, or high-level milestone notifications.
  - Do not treat `kuro` as an automated AI process.
- **`oschan` (AI ASSISTANT):** General-purpose background task execution, code review, Python, and testing.
- **Other Agents:** Agents register themselves with specific capability keywords.

### Discovering Agents & Capabilities

```bash
# List all registered agents and their capabilities
aimail users

# Search for an agent with specific skills (e.g. python, frontend, testing)
aimail user find "<keyword>"

# Inspect an agent's full profile
aimail user show <agent_id>

# Self-registration:
# 1. Antigravity (agy) agent:
aimail user register --id <my_id> --client agy --desc "<what I do>" --keywords "<tags>" --conv-id "<conversation_id>"

# 2. OpenCode agent:
aimail user register --id <my_id> --client opencode --desc "<what I do>" --keywords "<tags>" --conv-id "<session_id>"

# 3. Custom / Other runtimes (tmux, script, curl):
aimail user register --id <my_id> --client custom --desc "<what I do>" --keywords "<tags>" --wakeup-cmd "tmux send-keys -t worker '{prompt}' Enter"
```

---

## 📬 Receiving & Processing Incoming Tasks

If you are an agent operating under an assigned ID (e.g. `oschan`):

### 1. Check Inbox
```bash
aimail list --to <my_id> --status new
```

### 2. Inspect Task Details & Download/Extract Payloads
Inspect the task instructions and preview the markdown body:
```bash
aimail show <message_id_or_topic>

# Optionally save the markdown instructions directly to a local file:
aimail show <message_id_or_topic> --out task_spec.md
```

If an attachment is linked, you can either **download** the raw file or **extract** an archive cleanly into your local workspace without knowing internal mailbox paths:
```bash
# 1. Download raw attachment file (archive, binary, image, dataset) into current workspace:
aimail download <message_id_or_topic> --out ./

# 2. Or unpack an archive directly into a target folder:
aimail extract <message_id_or_topic> --out ./task_inputs
```

### 3. Claim Task & Mark as Processing
As soon as you wake up and inspect the task, immediately mark it as `processing` so the operator (`kuro`) and the sender can monitor execution progress in real-time:
```bash
aimail mark <message_id_or_topic> --status processing
```

### 4. Execute Task & Send Reply
Once work is completed, use `aimail reply` to close the loop in one command (this marks the original message as replied, creates the reply message, and automatically wakes up the sender):
```bash
# 1-step inline reply (marks original replied + wakes sender):
aimail reply <message_id_or_topic> -c "Task completed successfully. Output saved to ..."

# Or reply with a markdown file and attachment:
aimail reply <message_id_or_topic> -f results.md -a output.tar.xz --erase-original
```
*(Alternatively, running `aimail mark <id> --status replied` will also automatically notify and wake up the original sender).*

### 5. Terminate Finished Threads Silently (`aimail close`)
To **prevent infinite conversational ping-pong loops** (e.g. repeated "thanks" / "acknowledged" wakeups) or when an incoming message is purely informational and needs no response:
**DO NOT send a courtesy reply.** Instead, simply close the thread:
```bash
# Silently marks the message as 'closed' with ZERO wakeups:
aimail close <message_id_or_topic>
```

---

## 📤 Delegating Tasks to Another Agent

When a task exceeds your current workspace scope or requires another specialist:

### 1. Find the Right Agent
```bash
aimail user find "database"
```

### 2. Send the Task
```bash
# Quick inline request
aimail send --from <my_id> --to <target_agent> --topic <action_name> -c "Please inspect the crash log and suggest fixes."

# Or send a comprehensive markdown specification file (and optionally erase original)
aimail send --from <my_id> --to <target_agent> --topic <action_name> -f /path/to/spec.md --erase-original

# With an attachment tarball
aimail send --from <my_id> --to <target_agent> --topic <action_name> -f spec.md -a payload.tar.xz
```

### 3. Revoke / Unsend a Sent Message
If you sent a task in error or need to recall it before (or after) processing:
```bash
# Revoke your last sent message:
aimail revoke --from <my_id>

# Revoke a specific message by ID or topic:
aimail revoke <message_id_or_topic>

# Force revoke if the message was already marked read or replied:
aimail revoke <message_id_or_topic> --force
```

---

## ⚠️ Important Guidelines

1. **Decoupled Architecture:** Never attempt to inspect or modify another agent's private workspace or directory directly. Exchange information strictly via `aimail` CLI commands and attachments.
2. **Black-Box Encapsulation:** Treat `aimail` as a closed-source system binary in `$PATH`. Do not explore the installation folder or inspect source code.
3. **Status Transitions:** Always keep message statuses updated (`new` → `processing` → `replied`, or `closed`) so senders and operator `kuro` have full real-time visibility into who is actively working on what.
