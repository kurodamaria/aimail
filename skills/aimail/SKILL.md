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

### 2. Inspect Task Details & Markdown Content
```bash
aimail show <message_id_or_topic>
```
Review the instructions in the markdown body. If an attachment is linked (e.g. `attachments/<name>.tar.xz`), extract and examine it if necessary:
```bash
tar -xf ~/Desktop/aimail/attachments/<attachment_filename> -C <target_dir>
```

### 3. Mark in Progress / Read
```bash
aimail mark <message_id> --status read
```

### 4. Execute Task & Send Reply
Once work is completed:
```bash
# Mark original message as replied
aimail mark <message_id> --status replied

# Send completion notice back to the original sender
aimail send --from <my_id> --to <sender_id> --topic <topic>_reply -c "Task completed successfully. Results saved to ..."
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

---

## ⚠️ Important Guidelines

1. **Decoupled Architecture:** Never attempt to inspect or modify another agent's private workspace or directory directly. Exchange information strictly via `aimail` markdown bodies and `attachments/`.
2. **Deterministic Naming:** All messages are automatically organized in `~/Desktop/aimail/markdown/` and `~/Desktop/aimail/attachments/`.
3. **Status Transitions:** Always keep message statuses updated (`new` → `read` → `replied`) so senders and operators have full visibility.
