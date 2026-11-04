#!/usr/bin/env python3
"""
aimail - Asynchronous Agent Messaging System
Local-first message bus and SQLite registry for agent-to-agent and human-to-agent communication.
"""

import argparse
import datetime
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

DEFAULT_MESSAGES_DIR = Path.home() / "Desktop" / "aimail"
MARKDOWN_SUBDIR = "markdown"
ATTACHMENTS_SUBDIR = "attachments"
DB_NAME = "aimail.db"

# Match pattern: [sender-id]-[receiver-id]-[topic]-[timestamp].md
# e.g.: alice-oschan-daily_report-20261103_181500.md
FILENAME_PATTERN = re.compile(
    r"^(?P<sender>[A-Za-z0-9_-]+)-(?P<receiver>[A-Za-z0-9_-]+)-(?P<topic>[A-Za-z0-9_]+)-(?P<timestamp>[A-Za-z0-9_T:-]+)\.md$"
)


def get_subdirs(messages_dir: Path):
    md_dir = messages_dir / MARKDOWN_SUBDIR
    att_dir = messages_dir / ATTACHMENTS_SUBDIR
    md_dir.mkdir(parents=True, exist_ok=True)
    att_dir.mkdir(parents=True, exist_ok=True)
    return md_dir, att_dir


def get_db_path(messages_dir: Path) -> Path:
    return messages_dir / DB_NAME


def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                message_timestamp TEXT NOT NULL,
                md_file_path TEXT NOT NULL UNIQUE,
                attachment_path TEXT,
                status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'processing', 'read', 'replied', 'closed', 'failed')),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_inbox
            ON messages(receiver_id, status, message_timestamp);
        """)

        # Migration: Ensure messages table allows new statuses ('processing', 'closed', 'failed')
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'")
        table_sql = cursor.fetchone()
        if table_sql and ("closed" not in table_sql[0] or "processing" not in table_sql[0]):
            cursor.execute("PRAGMA foreign_keys = OFF;")
            cursor.execute("""
                CREATE TABLE messages_migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id TEXT NOT NULL,
                    receiver_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    message_timestamp TEXT NOT NULL,
                    md_file_path TEXT NOT NULL UNIQUE,
                    attachment_path TEXT,
                    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'processing', 'read', 'replied', 'closed', 'failed')),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("""
                INSERT INTO messages_migration (id, sender_id, receiver_id, topic, message_timestamp, md_file_path, attachment_path, status, created_at, updated_at)
                SELECT id, sender_id, receiver_id, topic, message_timestamp, md_file_path, attachment_path, status, created_at, updated_at
                FROM messages;
            """)
            cursor.execute("DROP TABLE messages;")
            cursor.execute("ALTER TABLE messages_migration RENAME TO messages;")
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_inbox
                ON messages(receiver_id, status, message_timestamp);
            """)
            cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                internal_id TEXT UNIQUE,
                description TEXT NOT NULL,
                keywords TEXT NOT NULL,
                client_type TEXT DEFAULT 'agy',
                conversation_id TEXT,
                wakeup_cmd TEXT,
                aliases TEXT DEFAULT '',
                registered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Backward-compatible column migration
        cursor.execute("PRAGMA table_info(users)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        if "conversation_id" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN conversation_id TEXT;")
        if "wakeup_cmd" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN wakeup_cmd TEXT;")
        if "client_type" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN client_type TEXT DEFAULT 'agy';")
        if "internal_id" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN internal_id TEXT;")
        if "aliases" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN aliases TEXT DEFAULT '';")

        # Backfill internal_id for existing users
        cursor.execute("SELECT user_id FROM users WHERE internal_id IS NULL OR internal_id = ''")
        for u in cursor.fetchall():
            iid = f"usr_{uuid.uuid4().hex[:10]}"
            cursor.execute("UPDATE users SET internal_id = ? WHERE user_id = ?", (iid, u[0]))
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_internal_id ON users(internal_id);")

        # Migrate messages table to include sender_internal_id and receiver_internal_id
        cursor.execute("PRAGMA table_info(messages)")
        msg_cols = {row[1] for row in cursor.fetchall()}
        if "sender_internal_id" not in msg_cols:
            cursor.execute("ALTER TABLE messages ADD COLUMN sender_internal_id TEXT;")
        if "receiver_internal_id" not in msg_cols:
            cursor.execute("ALTER TABLE messages ADD COLUMN receiver_internal_id TEXT;")

        # Populate message internal IDs if missing
        cursor.execute("""
            UPDATE messages
            SET sender_internal_id = (SELECT internal_id FROM users WHERE users.user_id = messages.sender_id)
            WHERE sender_internal_id IS NULL AND EXISTS (SELECT 1 FROM users WHERE users.user_id = messages.sender_id);
        """)
        cursor.execute("""
            UPDATE messages
            SET receiver_internal_id = (SELECT internal_id FROM users WHERE users.user_id = messages.receiver_id)
            WHERE receiver_internal_id IS NULL AND EXISTS (SELECT 1 FROM users WHERE users.user_id = messages.receiver_id);
        """)

        # Seed initial users if empty
        cursor.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] == 0:
            cursor.executemany("""
                INSERT INTO users (user_id, internal_id, description, keywords, client_type, conversation_id, wakeup_cmd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                ("kuro", f"usr_{uuid.uuid4().hex[:10]}", "Real human user, developer, and system owner.", "human, developer, owner, operator, feedback, decisions", "human", None, 'osascript -e \'display notification "{prompt}" with title "aimail: message from {sender}"\''),
                ("oschan", f"usr_{uuid.uuid4().hex[:10]}", "AI assistant and autonomous agent for background task execution, code review, and development.", "agent, python, coding, git, review, automation, testing", "agy", None, None)
            ])
        conn.commit()


def get_connection(messages_dir: Path) -> sqlite3.Connection:
    db_path = get_db_path(messages_dir)
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_user(conn: sqlite3.Connection, identifier: str):
    """Resolve an identifier (username handle, internal_id, or alias) to a canonical user Row."""
    if not identifier:
        return None
    cursor = conn.cursor()
    # 1. Exact match on user_id
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (identifier,))
    row = cursor.fetchone()
    if row:
        return row
    # 2. Match on internal_id
    cursor.execute("SELECT * FROM users WHERE internal_id = ?", (identifier,))
    row = cursor.fetchone()
    if row:
        return row
    # 3. Match on aliases (comma-separated list)
    cursor.execute("SELECT * FROM users WHERE (',' || COALESCE(aliases, '') || ',') LIKE ?", (f"%,{identifier},%",))
    row = cursor.fetchone()
    if row:
        return row
    return None


def get_canonical_user_id(conn: sqlite3.Connection, identifier: str, notify: bool = False) -> str:
    """Return the current user_id for an identifier, optionally notifying if resolved from an alias/ID."""
    if not identifier:
        return identifier
    user = resolve_user(conn, identifier)
    if user:
        if user["user_id"] != identifier and notify:
            print(f"Notice: Resolved alias/ID '{identifier}' → '{user['user_id']}'.")
        return user["user_id"]
    return identifier


def format_table(
    headers: list,
    rows: list,
    elastic_cols: list = None,
    min_widths: list = None,
    max_widths: list = None,
    style: str = None,
    target_width: int = None
) -> str:
    """Format tabular data into an adaptive, cleanly-aligned terminal table."""
    if not rows:
        return "No records found."

    cols = len(headers)
    elastic_cols = elastic_cols or []

    # Detect style: default to box if interactive TTY, plain if piped
    if not style or style == "auto":
        style = "box" if sys.stdout.isatty() else "plain"

    # Detect UTF-8 capabilities
    utf8 = True
    try:
        "┌─┐│└─┘├─┤┬┴┼…".encode(sys.stdout.encoding or "utf-8")
    except Exception:
        utf8 = False

    ellipsis_char = "…" if utf8 else "..."

    # Normalize rows to strings
    str_rows = []
    for r in rows:
        str_rows.append([str(val) if val is not None else "" for val in r])

    # Plain tab-separated for pipes/scripts
    if style == "plain":
        lines = ["\t".join(headers)]
        for r in str_rows:
            lines.append("\t".join(r))
        return "\n".join(lines)

    term_w = target_width or shutil.get_terminal_size(fallback=(100, 24)).columns
    term_w = max(term_w, 60)

    # Initial widths based on max content
    data_widths = [len(h) for h in headers]
    for r in str_rows:
        for i, val in enumerate(r):
            data_widths[i] = max(data_widths[i], len(val))

    col_widths = list(data_widths)
    if max_widths:
        for i, m in enumerate(max_widths):
            if m is not None and i not in elastic_cols:
                col_widths[i] = min(col_widths[i], m)
    if min_widths:
        for i, m in enumerate(min_widths):
            if m is not None:
                col_widths[i] = max(col_widths[i], m)

    # Calculate overhead
    if style == "clean":
        overhead = 3 * (cols - 1)
    else:  # 'box'
        overhead = 3 * cols + 1

    non_elastic_width = sum(col_widths[i] for i in range(cols) if i not in elastic_cols)
    avail_for_elastic = term_w - overhead - non_elastic_width

    if elastic_cols:
        min_elastic_sum = sum((min_widths[i] if min_widths and min_widths[i] else 10) for i in elastic_cols)
        if avail_for_elastic < min_elastic_sum:
            avail_for_elastic = min_elastic_sum

        if len(elastic_cols) == 1:
            col_widths[elastic_cols[0]] = max(avail_for_elastic, min_widths[elastic_cols[0]] if min_widths else 10)
        elif len(elastic_cols) == 2:
            w0 = max(int(avail_for_elastic * 0.58), min_widths[elastic_cols[0]] if min_widths else 10)
            w1 = max(avail_for_elastic - w0, min_widths[elastic_cols[1]] if min_widths else 10)
            col_widths[elastic_cols[0]] = w0
            col_widths[elastic_cols[1]] = w1
        else:
            share = avail_for_elastic // len(elastic_cols)
            for c in elastic_cols:
                col_widths[c] = max(share, min_widths[c] if min_widths else 10)

    def trunc(val, w):
        val = val.strip()
        if len(val) > w:
            cut = w - len(ellipsis_char)
            return (val[:cut] if cut > 0 else val[:w]) + ellipsis_char
        return val.ljust(w)

    if style == "clean":
        sep = "   "
        head_line = sep.join(headers[i].ljust(col_widths[i]) for i in range(cols))
        div_char = "─" if utf8 else "-"
        div_line = sep.join(div_char * col_widths[i] for i in range(cols))
        lines = [head_line, div_line]
        for r in str_rows:
            lines.append(sep.join(trunc(r[i], col_widths[i]) for i in range(cols)))
        return "\n".join(lines)

    # Box style
    c_top_l = "┌" if utf8 else "+"
    c_top_m = "┬" if utf8 else "+"
    c_top_r = "┐" if utf8 else "+"
    c_mid_l = "├" if utf8 else "+"
    c_mid_m = "┼" if utf8 else "+"
    c_mid_r = "┤" if utf8 else "+"
    c_bot_l = "└" if utf8 else "+"
    c_bot_m = "┴" if utf8 else "+"
    c_bot_r = "┘" if utf8 else "+"
    h_bar   = "─" if utf8 else "-"
    v_bar   = "│" if utf8 else "|"

    top = c_top_l + h_bar + (h_bar + c_top_m + h_bar).join(h_bar * col_widths[i] for i in range(cols)) + h_bar + c_top_r
    head = v_bar + " " + (" " + v_bar + " ").join(headers[i].ljust(col_widths[i]) for i in range(cols)) + " " + v_bar
    mid = c_mid_l + h_bar + (h_bar + c_mid_m + h_bar).join(h_bar * col_widths[i] for i in range(cols)) + h_bar + c_mid_r
    bot = c_bot_l + h_bar + (h_bar + c_bot_m + h_bar).join(h_bar * col_widths[i] for i in range(cols)) + h_bar + c_bot_r

    lines = [top, head, mid]
    for r in str_rows:
        lines.append(v_bar + " " + (" " + v_bar + " ").join(trunc(r[i], col_widths[i]) for i in range(cols)) + " " + v_bar)
    lines.append(bot)
    return "\n".join(lines)


def cmd_sync(args, quiet_if_zero: bool = False):
    messages_dir = Path(args.dir).resolve()
    if not messages_dir.exists():
        print(f"Error: Directory {messages_dir} does not exist.", file=sys.stderr)
        sys.exit(1)

    md_dir, att_dir = get_subdirs(messages_dir)

    # Auto-move any stray .md or .tar.xz dropped directly into root messages_dir
    for stray_md in messages_dir.glob("*.md"):
        if FILENAME_PATTERN.match(stray_md.name):
            target_file = md_dir / stray_md.name
            shutil.move(str(stray_md), str(target_file))

    for stray_att in messages_dir.glob("*.tar.xz"):
        target_file = att_dir / stray_att.name
        shutil.move(str(stray_att), str(target_file))

    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    # Get already indexed md files
    cursor.execute("SELECT md_file_path FROM messages")
    indexed_files = {row["md_file_path"] for row in cursor.fetchall()}

    added = 0
    updated_attachments = 0

    # Scan markdown subdirectory
    for entry in sorted(md_dir.glob("*.md")):
        rel_path = f"{MARKDOWN_SUBDIR}/{entry.name}"
        match = FILENAME_PATTERN.match(entry.name)
        if not match:
            continue

        sender = get_canonical_user_id(conn, match.group("sender"))
        receiver = get_canonical_user_id(conn, match.group("receiver"))
        topic = match.group("topic")
        timestamp = match.group("timestamp")

        sender_u = resolve_user(conn, sender)
        receiver_u = resolve_user(conn, receiver)
        sender_iid = sender_u["internal_id"] if sender_u else None
        receiver_iid = receiver_u["internal_id"] if receiver_u else None

        # Check for corresponding attachment in attachments_dir
        stem = entry.stem
        attachment_path = None
        for cand in att_dir.glob(f"{stem}.*"):
            attachment_path = f"{ATTACHMENTS_SUBDIR}/{cand.name}"
            break

        if rel_path not in indexed_files:
            cursor.execute("""
                INSERT INTO messages (sender_id, receiver_id, sender_internal_id, receiver_internal_id, topic, message_timestamp, md_file_path, attachment_path, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new')
            """, (sender, receiver, sender_iid, receiver_iid, topic, timestamp, rel_path, attachment_path))
            added += 1
        else:
            # Check if attachment appeared after indexing
            if attachment_path:
                cursor.execute("""
                    UPDATE messages
                    SET attachment_path = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE md_file_path = ? AND attachment_path IS NULL
                """, (attachment_path, rel_path))
                if cursor.rowcount > 0:
                    updated_attachments += 1

    conn.commit()
    conn.close()

    if not (quiet_if_zero and added == 0 and updated_attachments == 0):
        print(f"Sync complete. New messages registered: {added}, attachments updated: {updated_attachments}")


def cmd_list(args):
    messages_dir = Path(args.dir).resolve()
    # Auto-sync before listing
    cmd_sync(args, quiet_if_zero=True)

    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    query = "SELECT * FROM messages WHERE 1=1"
    params = []

    if args.to:
        query += " AND receiver_id = ?"
        params.append(get_canonical_user_id(conn, args.to))
    if args.sender:
        query += " AND sender_id = ?"
        params.append(get_canonical_user_id(conn, args.sender))
    if args.status and args.status != "all":
        query += " AND status = ?"
        params.append(args.status)

    query += " ORDER BY message_timestamp ASC"
    if args.limit:
        query += f" LIMIT {int(args.limit)}"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No messages found matching criteria.")
        return

    headers = ["ID", "STATUS", "FROM", "TO", "TOPIC", "TIME", "ATT"]
    data = []
    for r in rows:
        att = "yes" if r["attachment_path"] else "-"
        ts = r["message_timestamp"]
        if len(ts) >= 15 and "_" in ts and ts[:8].isdigit():
            ts = f"{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}"
        elif len(ts) >= 16 and "-" in ts:
            ts = ts[5:16]
        data.append([str(r["id"]), r["status"], r["sender_id"], r["receiver_id"], r["topic"], ts, att])

    style = getattr(args, "style", None)
    table_str = format_table(
        headers=headers,
        rows=data,
        elastic_cols=[4],
        min_widths=[2, 6, 8, 8, 12, 11, 3],
        max_widths=[4, 10, 14, 14, None, 11, 4],
        style=style
    )
    print(table_str)


def cmd_mark(args):
    messages_dir = Path(args.dir).resolve()
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    target = args.target
    new_status = args.status

    if target.isdigit():
        cursor.execute("SELECT * FROM messages WHERE id = ?", (int(target),))
    else:
        cursor.execute("SELECT * FROM messages WHERE md_file_path = ? OR md_file_path LIKE ?", (target, f"%{target}%"))

    msg_row = cursor.fetchone()
    if not msg_row:
        print(f"No message found matching identifier '{target}'.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    cursor.execute("""
        UPDATE messages
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (new_status, msg_row["id"]))
    conn.commit()
    conn.close()

    if new_status == "closed":
        print(f"✓ Message #{msg_row['id']} ('{msg_row['topic']}') marked as 'closed'. Thread terminated without wakeup.")
    else:
        print(f"Message '{target}' marked as '{new_status}'.")

    # If marked as replied, wake up the original sender
    if new_status == "replied" and not getattr(args, "no_wakeup", False):
        trigger_wakeup(
            receiver=msg_row["sender_id"],
            sender=msg_row["receiver_id"],
            topic=f"{msg_row['topic']}_replied",
            file_path=msg_row["md_file_path"],
            messages_dir=messages_dir
        )


def cmd_close(args):
    """Mark a message thread as 'closed' silently without triggering any wakeups (breaks reply loops)."""
    args.status = "closed"
    args.no_wakeup = True
    cmd_mark(args)


def cmd_revoke(args):
    messages_dir = Path(args.dir).resolve()
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    target = args.target
    sender = getattr(args, "sender", None)

    msg_row = None
    if not target or target.lower() == "last" or getattr(args, "last", False):
        if sender:
            cursor.execute("SELECT * FROM messages WHERE sender_id = ? ORDER BY id DESC LIMIT 1", (sender,))
        else:
            cursor.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 1")
        msg_row = cursor.fetchone()
    elif target.isdigit():
        cursor.execute("SELECT * FROM messages WHERE id = ?", (int(target),))
        msg_row = cursor.fetchone()
    else:
        cursor.execute("SELECT * FROM messages WHERE md_file_path = ? OR md_file_path LIKE ?", (target, f"%{target}%"))
        msg_row = cursor.fetchone()

    if not msg_row:
        ident_str = f" from sender '{sender}'" if sender else ""
        print(f"Error: No message found to revoke{ident_str}.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Check status
    if msg_row["status"] in ("processing", "read", "replied", "closed") and not getattr(args, "force", False):
        print(
            f"Notice: Message #{msg_row['id']} ('{msg_row['topic']}') has already reached status '{msg_row['status']}' (assigned to '{msg_row['receiver_id']}').\n"
            f"Pass --force to permanently revoke and delete it anyway.",
            file=sys.stderr
        )
        conn.close()
        sys.exit(1)

    # Delete markdown file
    md_file = messages_dir / msg_row["md_file_path"]
    if not md_file.exists():
        md_file = messages_dir / MARKDOWN_SUBDIR / Path(msg_row["md_file_path"]).name
    if md_file.exists():
        try:
            md_file.unlink()
        except OSError as e:
            print(f"Warning: Failed to delete markdown file: {e}", file=sys.stderr)

    # Delete attachment file if present
    if msg_row["attachment_path"]:
        att_file = messages_dir / msg_row["attachment_path"]
        if not att_file.exists():
            att_file = messages_dir / ATTACHMENTS_SUBDIR / Path(msg_row["attachment_path"]).name
        if att_file.exists():
            try:
                att_file.unlink()
            except OSError as e:
                print(f"Warning: Failed to delete attachment file: {e}", file=sys.stderr)

    # Delete record from database
    cursor.execute("DELETE FROM messages WHERE id = ?", (msg_row["id"],))
    conn.commit()
    conn.close()

    print(f"✓ Revoked message #{msg_row['id']} ('{msg_row['topic']}') from '{msg_row['sender_id']}' to '{msg_row['receiver_id']}'. Deleted from inbox.")


def cmd_show(args):
    messages_dir = Path(args.dir).resolve()
    cmd_sync(args)
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    target = args.target
    if target.isdigit():
        cursor.execute("SELECT * FROM messages WHERE id = ?", (int(target),))
    else:
        cursor.execute("SELECT * FROM messages WHERE md_file_path = ? OR md_file_path LIKE ?", (target, f"%{target}%"))

    row = cursor.fetchone()
    conn.close()

    if not row:
        print(f"Error: Message '{target}' not found.", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print(f"ID:          {row['id']}")
    print(f"Sender:      {row['sender_id']}")
    print(f"Receiver:    {row['receiver_id']}")
    print(f"Topic:       {row['topic']}")
    print(f"Timestamp:   {row['message_timestamp']}")
    print(f"Status:      {row['status']}")
    print(f"Markdown:    {row['md_file_path']}")
    print(f"Attachment:  {row['attachment_path'] or 'None'}")
    print(f"Created:     {row['created_at']}")
    print(f"Updated:     {row['updated_at']}")
    print("=" * 60)

    file_path = messages_dir / row["md_file_path"]
    if not file_path.exists():
        file_path = messages_dir / MARKDOWN_SUBDIR / Path(row["md_file_path"]).name

    if file_path.exists():
        print("\n--- CONTENT PREVIEW ---\n")
        print(file_path.read_text(encoding="utf-8"))
        if getattr(args, "out", None):
            out_dest = Path(args.out).resolve()
            if out_dest.is_dir():
                out_file = out_dest / file_path.name
            else:
                out_dest.parent.mkdir(parents=True, exist_ok=True)
                out_file = out_dest
            shutil.copy2(file_path, out_file)
            print(f"\n✓ Saved markdown file to: {out_file}")
    else:
        print(f"\nWarning: File {file_path} not found on disk.")


def trigger_wakeup(receiver: str, sender: str, topic: str, file_path: str, messages_dir: Path):
    conn = get_connection(messages_dir)
    row = resolve_user(conn, receiver)
    conn.close()

    if not row:
        return

    canonical_receiver = row["user_id"]
    client_type = (row["client_type"] or "agy").lower()
    conv_id = row["conversation_id"]
    wakeup_template = row["wakeup_cmd"]

    prompt = f"New aimail received from {sender} on topic '{topic}'. Check inbox with 'aimail show {topic}', mark it with 'aimail mark {topic} --status processing', and reply with 'aimail reply' when done."
    escaped_prompt = prompt.replace('"', '\\"')

    final_cmd = None
    if wakeup_template:
        try:
            final_cmd = wakeup_template.format(
                sender=sender,
                receiver=canonical_receiver,
                topic=topic,
                file=file_path,
                prompt=prompt,
                conv_id=conv_id or "",
                client=client_type
            )
        except (KeyError, IndexError):
            final_cmd = wakeup_template
    elif conv_id:
        if client_type == "agy":
            final_cmd = f'agy --conversation "{conv_id}" -p "{escaped_prompt}"'
        elif client_type == "opencode":
            session_id = conv_id if conv_id.startswith("ses") else f"ses_{conv_id}"
            final_cmd = f'opencode run --auto -s "{session_id}" "{escaped_prompt}"'
        else:
            final_cmd = f'agy --conversation "{conv_id}" -p "{escaped_prompt}"'
    elif client_type == "human" or canonical_receiver == "kuro":
        escaped_msg = f"New message from {sender} on '{topic}'".replace('"', '\\"')
        final_cmd = f'osascript -e \'display notification "{escaped_msg}" with title "aimail"\''

    if final_cmd:
        try:
            subprocess.Popen(
                final_cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            print(f"Triggered background wakeup for '{canonical_receiver}'.")
        except Exception as e:
            print(f"Warning: Failed to execute wakeup for '{canonical_receiver}': {e}", file=sys.stderr)


def cmd_send(args):
    messages_dir = Path(args.dir).resolve()
    messages_dir.mkdir(parents=True, exist_ok=True)
    md_dir, att_dir = get_subdirs(messages_dir)

    source_md = None
    if args.file:
        source_md = Path(args.file).resolve()
        if not source_md.exists() or not source_md.is_file():
            print(f"Error: Markdown file '{args.file}' not found or is not a valid file.", file=sys.stderr)
            sys.exit(1)

    # Validate attachment if provided
    source_attach = None
    if args.attach:
        source_attach = Path(args.attach).resolve()
        if not source_attach.exists() or not source_attach.is_file():
            print(f"Error: Attachment file '{args.attach}' not found.", file=sys.stderr)
            sys.exit(1)

    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    sender = get_canonical_user_id(conn, args.sender, notify=True)
    receiver = get_canonical_user_id(conn, args.receiver, notify=True)
    sender_u = resolve_user(conn, sender)
    receiver_u = resolve_user(conn, receiver)
    sender_iid = sender_u["internal_id"] if sender_u else None
    receiver_iid = receiver_u["internal_id"] if receiver_u else None

    topic = args.topic
    ts = args.timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    target_md_filename = f"{sender}-{receiver}-{topic}-{ts}.md"
    target_md_path = md_dir / target_md_filename
    db_md_path = f"{MARKDOWN_SUBDIR}/{target_md_filename}"

    # Write or copy markdown content
    if args.file:
        shutil.copy2(source_md, target_md_path)
        print(f"Copied markdown to: {target_md_path}")
    else:
        target_md_path.write_text(args.content, encoding="utf-8")
        print(f"Created markdown message at: {target_md_path}")

    target_attach_filename = None
    db_attach_path = None
    if source_attach:
        # Preserve original extension suffix (e.g. .tar.xz, .tar.gz, .tar.bz2)
        name_lower = source_attach.name.lower()
        if name_lower.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst")):
            ext = "." + ".".join(source_attach.name.split(".")[-2:])
        else:
            ext = source_attach.suffix
        target_attach_filename = f"{sender}-{receiver}-{topic}-{ts}{ext}"
        target_attach_path = att_dir / target_attach_filename
        db_attach_path = f"{ATTACHMENTS_SUBDIR}/{target_attach_filename}"
        shutil.copy2(source_attach, target_attach_path)
        print(f"Copied attachment to: {target_attach_path}")

    # Informative notice if receiver is not registered
    if not receiver_u:
        print(f"Notice: Receiver '{receiver}' is not in user directory. Run 'aimail users' to see registered agents.")

    cursor.execute("""
        INSERT INTO messages (sender_id, receiver_id, sender_internal_id, receiver_internal_id, topic, message_timestamp, md_file_path, attachment_path, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new')
    """, (sender, receiver, sender_iid, receiver_iid, topic, ts, db_md_path, db_attach_path))
    conn.commit()
    conn.close()
    print(f"Message registered in SQLite database with status 'new'.")

    # Trigger background wakeup unless explicitly disabled
    if not getattr(args, "no_wakeup", False):
        trigger_wakeup(receiver, sender, topic, db_md_path, messages_dir)

    # Erase original files if requested
    if args.erase_original:
        if source_md:
            try:
                source_md.unlink()
                print(f"Erased original markdown file: {source_md}")
            except OSError as e:
                print(f"Warning: Failed to erase original file: {e}", file=sys.stderr)
        if source_attach and source_attach.exists():
            try:
                source_attach.unlink()
                print(f"Erased original attachment: {source_attach}")
            except OSError as e:
                print(f"Warning: Failed to erase original attachment: {e}", file=sys.stderr)


def cmd_reply(args):
    messages_dir = Path(args.dir).resolve()
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    target = args.target
    if target.isdigit():
        cursor.execute("SELECT * FROM messages WHERE id = ?", (int(target),))
    else:
        cursor.execute("SELECT * FROM messages WHERE md_file_path = ? OR md_file_path LIKE ?", (target, f"%{target}%"))
    msg_row = cursor.fetchone()

    if not msg_row:
        print(f"Error: Original message '{target}' not found.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    original_id = msg_row["id"]
    original_sender = msg_row["sender_id"]
    original_receiver = msg_row["receiver_id"]
    original_topic = msg_row["topic"]

    # Mark original message as replied
    cursor.execute("""
        UPDATE messages
        SET status = 'replied', updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (original_id,))
    conn.commit()
    conn.close()
    print(f"Original message #{original_id} marked as 'replied'.")

    # Set up parameters for cmd_send
    args.sender = getattr(args, "sender", None) or original_receiver
    args.receiver = original_sender
    if getattr(args, "topic", None):
        args.topic = args.topic
    elif original_topic.endswith("_reply"):
        args.topic = original_topic
    else:
        args.topic = f"{original_topic}_reply"
    args.timestamp = getattr(args, "timestamp", None)

    cmd_send(args)


def cmd_extract(args):
    messages_dir = Path(args.dir).resolve()
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    target = args.target
    if target.isdigit():
        cursor.execute("SELECT * FROM messages WHERE id = ?", (int(target),))
    else:
        cursor.execute("SELECT * FROM messages WHERE md_file_path = ? OR md_file_path LIKE ?", (target, f"%{target}%"))
    row = cursor.fetchone()
    conn.close()

    if not row:
        print(f"Error: Message '{target}' not found.", file=sys.stderr)
        sys.exit(1)

    if not row["attachment_path"]:
        print(f"Message #{row['id']} ('{row['topic']}') has no attachment.", file=sys.stderr)
        sys.exit(1)

    archive_path = messages_dir / row["attachment_path"]
    if not archive_path.exists():
        archive_path = messages_dir / ATTACHMENTS_SUBDIR / Path(row["attachment_path"]).name

    if not archive_path.exists():
        print(f"Error: Attachment file '{archive_path}' not found on disk.", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out or ".").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    name_lower = archive_path.name.lower()
    if name_lower.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tar", ".tgz")):
        import tarfile
        with tarfile.open(archive_path) as tar:
            tar.extractall(path=out_dir)
        print(f"Unpacked attachment '{archive_path.name}' into: {out_dir}")
    elif name_lower.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(archive_path) as z:
            z.extractall(path=out_dir)
        print(f"Unzipped attachment '{archive_path.name}' into: {out_dir}")
    else:
        dest_file = out_dir / archive_path.name
        shutil.copy2(archive_path, dest_file)
        print(f"Copied attachment '{archive_path.name}' to: {dest_file}")


def cmd_download(args):
    """Download/copy message attachment or markdown body to caller workspace."""
    messages_dir = Path(args.dir).resolve()
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    target = args.target
    if target.isdigit():
        cursor.execute("SELECT * FROM messages WHERE id = ?", (int(target),))
    else:
        cursor.execute("SELECT * FROM messages WHERE md_file_path = ? OR md_file_path LIKE ?", (target, f"%{target}%"))
    row = cursor.fetchone()
    conn.close()

    if not row:
        print(f"Error: Message '{target}' not found.", file=sys.stderr)
        sys.exit(1)

    out_dest = Path(args.out or ".").resolve()

    # If --body, download markdown
    if getattr(args, "body", False):
        md_file = messages_dir / row["md_file_path"]
        if not md_file.exists():
            md_file = messages_dir / MARKDOWN_SUBDIR / Path(row["md_file_path"]).name
        if not md_file.exists():
            print(f"Error: Markdown file '{md_file}' not found on disk.", file=sys.stderr)
            sys.exit(1)
        if out_dest.is_dir() or str(args.out or "").endswith("/"):
            out_dest.mkdir(parents=True, exist_ok=True)
            dest = out_dest / md_file.name
        else:
            out_dest.parent.mkdir(parents=True, exist_ok=True)
            dest = out_dest
        shutil.copy2(md_file, dest)
        print(f"✓ Downloaded markdown file to: {dest}")
        return

    # Download attachment
    if not row["attachment_path"]:
        print(f"Message #{row['id']} ('{row['topic']}') has no attachment.", file=sys.stderr)
        sys.exit(1)

    archive_path = messages_dir / row["attachment_path"]
    if not archive_path.exists():
        archive_path = messages_dir / ATTACHMENTS_SUBDIR / Path(row["attachment_path"]).name

    if not archive_path.exists():
        print(f"Error: Attachment file '{archive_path}' not found on disk.", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "extract", False):
        args.out = str(out_dest)
        cmd_extract(args)
        return

    if out_dest.is_dir() or str(args.out or "").endswith("/"):
        out_dest.mkdir(parents=True, exist_ok=True)
        dest_file = out_dest / archive_path.name
    else:
        out_dest.parent.mkdir(parents=True, exist_ok=True)
        dest_file = out_dest

    shutil.copy2(archive_path, dest_file)
    print(f"✓ Downloaded attachment '{archive_path.name}' to: {dest_file}")


def cmd_inspect(args):
    """Open interactive TUI for an agent or message participant (agy or opencode)."""
    messages_dir = Path(args.dir).resolve()
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    target = args.target
    user_id = None

    # Check if target is a message ID or keyword
    if not target or target.lower() == "last":
        cursor.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            print("Error: No messages found in database.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        user_id = row["sender_id"] if args.sender else row["receiver_id"]
    elif target.isdigit():
        cursor.execute("SELECT * FROM messages WHERE id = ?", (int(target),))
        row = cursor.fetchone()
        if not row:
            print(f"Error: Message #{target} not found.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        user_id = row["sender_id"] if args.sender else row["receiver_id"]
    else:
        # Check if target is a registered user ID, alias, or internal ID
        u_row = resolve_user(conn, target)
        if u_row:
            user_id = u_row["user_id"]
        else:
            # Check if target is a message topic substring
            cursor.execute("SELECT * FROM messages WHERE md_file_path = ? OR md_file_path LIKE ? ORDER BY id DESC LIMIT 1", (target, f"%{target}%"))
            row = cursor.fetchone()
            if row:
                user_id = row["sender_id"] if args.sender else row["receiver_id"]
            else:
                user_id = target

    user_row = resolve_user(conn, user_id)
    conn.close()

    if not user_row:
        print(f"Error: User '{user_id}' is not registered in aimail directory.", file=sys.stderr)
        sys.exit(1)

    client_type = (user_row["client_type"] or "agy").lower()
    conv_id = user_row["conversation_id"]

    if not conv_id:
        print(f"Error: User '{user_id}' does not have a registered conversation_id. Run 'aimail user register' to set one.", file=sys.stderr)
        sys.exit(1)

    if client_type == "agy":
        cmd_args = ["agy", f"--conversation={conv_id}"]
        display_cmd = f"agy --conversation={conv_id}"
    elif client_type == "opencode":
        session_id = conv_id if conv_id.startswith("ses") else f"ses_{conv_id}"
        cmd_args = ["opencode", "-s", session_id]
        display_cmd = f"opencode -s {session_id}"
    else:
        print(f"Error: Client type '{client_type}' does not support interactive TUI inspection.", file=sys.stderr)
        sys.exit(1)

    print(f"Opening {client_type} session for agent '{user_id}' ({conv_id})...")

    if getattr(args, "window", False):
        apple_script = f'tell application "Terminal" to do script "{display_cmd}"'
        try:
            subprocess.run(["osascript", "-e", apple_script], check=True)
            print(f"✓ Launched {client_type} in a new Terminal window.")
        except Exception as e:
            print(f"Notice: Failed to spawn new window ({e}), launching in current terminal instead...", file=sys.stderr)
            subprocess.run(cmd_args)
    else:
        try:
            subprocess.run(cmd_args)
        except KeyboardInterrupt:
            pass


def cmd_user(args):
    messages_dir = Path(args.dir).resolve()
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    action = getattr(args, "user_action", "list")

    if action == "register":
        user_id = args.id.strip()
        desc = args.desc.strip()
        keywords = args.keywords.strip()
        client_type = getattr(args, "client", "agy").strip()
        conv_id = getattr(args, "conv_id", None)
        if conv_id:
            conv_id = conv_id.strip()
        wakeup_cmd = getattr(args, "wakeup_cmd", None)
        if wakeup_cmd:
            wakeup_cmd = wakeup_cmd.strip()

        new_iid = f"usr_{uuid.uuid4().hex[:10]}"
        cursor.execute("""
            INSERT INTO users (user_id, internal_id, description, keywords, client_type, conversation_id, wakeup_cmd, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                description=excluded.description,
                keywords=excluded.keywords,
                client_type=excluded.client_type,
                conversation_id=COALESCE(excluded.conversation_id, users.conversation_id),
                wakeup_cmd=COALESCE(excluded.wakeup_cmd, users.wakeup_cmd),
                updated_at=CURRENT_TIMESTAMP
        """, (user_id, new_iid, desc, keywords, client_type, conv_id, wakeup_cmd))
        conn.commit()
        print(f"User '{user_id}' ({client_type}) registered/updated successfully.")

    elif action == "rename":
        old_identifier = args.old_name.strip()
        new_name = args.new_name.strip()

        if not re.match(r"^[A-Za-z0-9_-]+$", new_name):
            print(f"Error: Invalid username '{new_name}'. Only letters, numbers, underscores, and hyphens are allowed.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        user_row = resolve_user(conn, old_identifier)
        if not user_row:
            print(f"Error: User '{old_identifier}' not found in directory.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        old_canonical = user_row["user_id"]
        internal_id = user_row["internal_id"]

        if old_canonical == new_name:
            print(f"User is already named '{new_name}'. Nothing to rename.")
            conn.close()
            return

        conflict = resolve_user(conn, new_name)
        if conflict and conflict["internal_id"] != internal_id:
            print(f"Error: Username or alias '{new_name}' is already taken by user '{conflict['user_id']}'.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        # Update aliases list
        existing_aliases = [a.strip() for a in (user_row["aliases"] or "").split(",") if a.strip()]
        if old_canonical not in existing_aliases:
            existing_aliases.append(old_canonical)
        if new_name in existing_aliases:
            existing_aliases.remove(new_name)
        new_aliases_str = ",".join(existing_aliases)

        # 1. Update users table
        cursor.execute("""
            UPDATE users
            SET user_id = ?, aliases = ?, updated_at = CURRENT_TIMESTAMP
            WHERE internal_id = ?
        """, (new_name, new_aliases_str, internal_id))

        # 2. Update messages table
        cursor.execute("UPDATE messages SET sender_id = ? WHERE sender_id = ?", (new_name, old_canonical))
        sent_updated = cursor.rowcount
        cursor.execute("UPDATE messages SET receiver_id = ? WHERE receiver_id = ?", (new_name, old_canonical))
        recv_updated = cursor.rowcount

        # 3. Rename on-disk files in markdown/ and attachments/
        md_dir = messages_dir / MARKDOWN_SUBDIR
        att_dir = messages_dir / ATTACHMENTS_SUBDIR
        files_renamed = 0

        cursor.execute("SELECT id, sender_id, receiver_id, topic, message_timestamp, md_file_path, attachment_path FROM messages WHERE md_file_path LIKE ? OR (attachment_path IS NOT NULL AND attachment_path LIKE ?)", (f"%{old_canonical}%", f"%{old_canonical}%"))
        affected_msgs = cursor.fetchall()

        for m in affected_msgs:
            mid = m["id"]
            orig_md = messages_dir / m["md_file_path"]
            new_md_name = f"{m['sender_id']}-{m['receiver_id']}-{m['topic']}-{m['message_timestamp']}.md"
            new_md_path = md_dir / new_md_name
            new_db_md = f"{MARKDOWN_SUBDIR}/{new_md_name}"

            if orig_md.exists() and orig_md != new_md_path:
                try:
                    orig_md.rename(new_md_path)
                    files_renamed += 1
                except OSError as e:
                    print(f"Warning: Failed to rename {orig_md.name} to {new_md_name}: {e}", file=sys.stderr)

            new_db_att = m["attachment_path"]
            if m["attachment_path"]:
                orig_att = messages_dir / m["attachment_path"]
                ext = ""
                name_l = orig_att.name.lower()
                if name_l.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst")):
                    ext = "." + ".".join(orig_att.name.split(".")[-2:])
                else:
                    ext = orig_att.suffix
                new_att_name = f"{m['sender_id']}-{m['receiver_id']}-{m['topic']}-{m['message_timestamp']}{ext}"
                new_att_path = att_dir / new_att_name
                new_db_att = f"{ATTACHMENTS_SUBDIR}/{new_att_name}"

                if orig_att.exists() and orig_att != new_att_path:
                    try:
                        orig_att.rename(new_att_path)
                        files_renamed += 1
                    except OSError as e:
                        print(f"Warning: Failed to rename {orig_att.name} to {new_att_name}: {e}", file=sys.stderr)

            cursor.execute("UPDATE messages SET md_file_path = ?, attachment_path = ? WHERE id = ?",
                           (new_db_md, new_db_att, mid))

        conn.commit()
        print(f"✓ Renamed agent user '{old_canonical}' → '{new_name}'")
        print(f"  • Internal ID:      {internal_id}")
        print(f"  • Aliases recorded: {new_aliases_str or '(none)'}")
        print(f"  • Messages updated: {sent_updated} sent, {recv_updated} received")
        print(f"  • Files renamed:    {files_renamed} on disk")

    elif action == "show":
        user_id = args.id.strip()
        row = resolve_user(conn, user_id)
        if not row:
            print(f"User '{user_id}' not found.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        print("=" * 60)
        print(f"User ID:         {row['user_id']}")
        print(f"Internal ID:     {row['internal_id'] or 'None'}")
        if row['aliases']:
            print(f"Aliases:         {row['aliases']}")
        print(f"Client Type:     {row['client_type'] or 'agy'}")
        print(f"Description:     {row['description']}")
        print(f"Keywords:        {row['keywords']}")
        print(f"Conversation ID: {row['conversation_id'] or 'None'}")
        print(f"Wakeup Command:  {row['wakeup_cmd'] or 'None'}")
        print(f"Registered:      {row['registered_at']}")
        print(f"Last Updated:    {row['updated_at']}")
        print("=" * 60)

    elif action in ("list", "find"):
        if action == "find":
            query = f"%{args.query.strip()}%"
            cursor.execute("""
                SELECT * FROM users 
                WHERE user_id LIKE ? OR description LIKE ? OR keywords LIKE ? OR client_type LIKE ?
                ORDER BY user_id ASC
            """, (query, query, query, query))
            rows = cursor.fetchall()
            if not rows:
                print(f"No users found matching '{args.query}'.")
                conn.close()
                return
        else:
            cursor.execute("SELECT * FROM users ORDER BY user_id ASC")
            rows = cursor.fetchall()
            if not rows:
                print("No users registered in directory.")
                conn.close()
                return

        is_verbose = getattr(args, "verbose", False)
        style = getattr(args, "style", None)

        if is_verbose:
            headers = ["USER ID", "CLIENT", "CONVERSATION ID", "DESCRIPTION", "CAPABILITIES"]
            data = []
            for r in rows:
                c_type = r["client_type"] or "agy"
                conv = r["conversation_id"] or "-"
                data.append([r["user_id"], c_type, conv, r["description"], r["keywords"]])
            table_str = format_table(
                headers=headers,
                rows=data,
                elastic_cols=[3, 4],
                min_widths=[11, 8, 20, 25, 20],
                max_widths=[14, 8, 38, None, None],
                style=style
            )
        else:
            headers = ["USER ID", "CLIENT", "SESSION", "DESCRIPTION", "CAPABILITIES"]
            data = []
            for r in rows:
                c_type = r["client_type"] or "agy"
                conv = r["conversation_id"] or "-"
                if conv != "-" and len(conv) > 10:
                    conv = conv[:8] + "…"
                data.append([r["user_id"], c_type, conv, r["description"], r["keywords"]])
            table_str = format_table(
                headers=headers,
                rows=data,
                elastic_cols=[3, 4],
                min_widths=[11, 8, 9, 18, 14],
                max_widths=[14, 8, 10, None, None],
                style=style
            )

        print(table_str)
        if not is_verbose and sys.stdout.isatty() and style != "plain":
            print("Tip: Run 'aimail user show <id>' for full profile, or 'aimail users -v' for expanded IDs.")

    conn.close()


def cmd_users(args):
    args.user_action = "list"
    cmd_user(args)


def cmd_serve(args):
    """Start the aimail web UI dashboard (single-page app served over HTTP)."""
    import json
    import threading
    import time
    import webbrowser
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    messages_dir = Path(args.dir).resolve()
    host = args.host
    port = args.port

    # -------------------------------------------------------------------------
    # HTML / CSS / JS  (single-file SPA — zero external deps)
    # -------------------------------------------------------------------------
    HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>aimail</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--surface:#161b22;--surface2:#1c2128;--border:#30363d;
  --accent:#58a6ff;--text:#e6edf3;--muted:#8b949e;
  --new:#388bfd;--processing:#d29922;--replied:#3fb950;
  --closed:#484f58;--failed:#f85149;--read:#8b949e;
  --radius:8px;--transition:0.18s ease;
}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:14px;line-height:1.5}

/* ── HEADER ──────────────────────────────────────────── */
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 18px;height:52px;
  background:var(--surface);border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:100;
  backdrop-filter:blur(10px);
}
header .brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:16px;letter-spacing:-0.3px}
header .brand .dot{width:9px;height:9px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent)}
header .hright{display:flex;align-items:center;gap:10px}
#conn-indicator{width:8px;height:8px;border-radius:50%;background:var(--failed);transition:background 0.4s;flex-shrink:0}
#conn-indicator.ok{background:var(--replied)}
.port-badge{font-size:11px;color:var(--muted);background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:2px 9px}
.btn{
  display:inline-flex;align-items:center;gap:5px;
  padding:6px 14px;border-radius:6px;border:1px solid var(--border);
  background:var(--surface2);color:var(--text);cursor:pointer;font-size:13px;
  transition:all var(--transition);text-decoration:none;white-space:nowrap;
}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.primary{background:var(--accent);color:#000;border-color:var(--accent);font-weight:600}
.btn.primary:hover{background:#79b8ff;border-color:#79b8ff}
.btn.danger{background:transparent;border-color:var(--failed);color:var(--failed)}
.btn.danger:hover{background:var(--failed);color:#fff}
.btn.sm{padding:3px 10px;font-size:12px}

/* ── LAYOUT ──────────────────────────────────────────── */
#app{display:flex;height:calc(100vh - 52px)}

/* ── SIDEBAR ─────────────────────────────────────────── */
#sidebar{
  width:220px;min-width:160px;max-width:260px;
  background:var(--surface);border-right:1px solid var(--border);
  display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;
}
.sidebar-section{padding:12px 10px 6px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;font-weight:600}
.nav-tabs{padding:4px 8px;display:flex;flex-direction:column;gap:2px}
.nav-tab{
  display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:6px;
  cursor:pointer;color:var(--muted);transition:all var(--transition);font-size:13px;
}
.nav-tab:hover{background:var(--surface2);color:var(--text)}
.nav-tab.active{background:var(--accent);color:#000;font-weight:600}
.nav-tab .tab-count{
  margin-left:auto;font-size:11px;background:rgba(255,255,255,0.12);
  border-radius:10px;padding:1px 7px;min-width:20px;text-align:center;
}
.nav-tab.active .tab-count{background:rgba(0,0,0,0.2)}
#sidebar-divider{height:1px;background:var(--border);margin:8px 10px}
.agent-chips{padding:4px 8px 10px;display:flex;flex-direction:column;gap:2px;overflow-y:auto}
.agent-chip{
  display:flex;align-items:center;gap:7px;padding:5px 10px;border-radius:6px;
  cursor:pointer;color:var(--muted);font-size:12px;transition:all var(--transition);
}
.agent-chip:hover{background:var(--surface2);color:var(--text)}
.agent-chip.active{color:var(--accent);background:rgba(88,166,255,0.08)}
.agent-avatar{
  width:22px;height:22px;border-radius:50%;background:var(--border);
  display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;
  flex-shrink:0;text-transform:uppercase;color:var(--text);
}

/* ── MESSAGE LIST ────────────────────────────────────── */
#main{display:flex;flex:1;overflow:hidden;min-width:0}
#msg-list-pane{
  width:340px;min-width:260px;border-right:1px solid var(--border);
  display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;
}
#list-header{
  padding:12px 14px 10px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:8px;
}
#list-header h2{font-size:15px;font-weight:600;flex:1}
#search-box{
  width:100%;padding:7px 10px;background:var(--surface2);
  border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;
  outline:none;transition:border var(--transition);
}
#search-box:focus{border-color:var(--accent)}
#msg-list{flex:1;overflow-y:auto;padding:4px 0}
.msg-row{
  display:flex;flex-direction:column;gap:3px;
  padding:10px 14px;cursor:pointer;
  border-bottom:1px solid rgba(48,54,61,0.5);
  transition:background var(--transition);position:relative;
}
.msg-row:hover{background:var(--surface2)}
.msg-row.selected{background:rgba(88,166,255,0.08);border-left:3px solid var(--accent)}
.msg-row-top{display:flex;align-items:center;gap:6px}
.msg-row-id{
  font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
  font-size:11px;
  color:var(--muted);
  font-weight:700;
  padding:1px 5px;
  background:rgba(255,255,255,0.06);
  border-radius:4px;
  flex-shrink:0;
}
.agent-tag{
  padding:1px 6px;
  border-radius:4px;
  font-size:12px;
  font-weight:600;
  display:inline-flex;
  align-items:center;
}
.msg-row-from{font-weight:600;font-size:13px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:flex;align-items:center;gap:4px}
.msg-row-time{font-size:11px;color:var(--muted);flex-shrink:0}
.msg-row-topic{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.msg-row-bottom{display:flex;align-items:center;gap:5px}
#empty-state{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  height:100%;color:var(--muted);gap:8px;font-size:13px;
}
#empty-state .empty-icon{font-size:36px;opacity:0.4}

/* ── STATUS BADGE ────────────────────────────────────── */
.badge{
  font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px;
  text-transform:uppercase;letter-spacing:.4px;white-space:nowrap;
}
.badge-new{background:rgba(56,139,253,0.15);color:var(--new)}
.badge-processing{background:rgba(210,153,34,0.15);color:var(--processing)}
.badge-replied{background:rgba(63,185,80,0.15);color:var(--replied)}
.badge-closed{background:rgba(72,79,88,0.3);color:var(--closed)}
.badge-failed{background:rgba(248,81,73,0.15);color:var(--failed)}
.badge-read{background:rgba(139,148,158,0.15);color:var(--read)}

/* ── DETAIL PANE ─────────────────────────────────────── */
#detail-pane{
  flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;
}
#no-select{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  height:100%;color:var(--muted);gap:8px;font-size:13px;
}
#no-select .ns-icon{font-size:48px;opacity:0.25}
#detail-content{display:none;flex-direction:column;height:100%;overflow:hidden}
#detail-header{
  padding:14px 20px 12px;border-bottom:1px solid var(--border);
  display:flex;flex-direction:column;gap:6px;
}
#detail-topic{font-size:17px;font-weight:700;line-height:1.3;display:flex;align-items:center}
.detail-id{
  font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
  font-size:14px;
  color:var(--muted);
  font-weight:700;
  margin-right:6px;
}
.meta-label{
  font-size:11px;
  color:var(--muted);
  text-transform:uppercase;
  letter-spacing:.3px;
  font-weight:600;
}
#detail-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
#detail-meta .meta-pill{font-size:12px;color:var(--muted)}
#detail-meta .arrow{color:var(--border)}
#detail-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding-top:4px}
#detail-body{
  flex:1;overflow-y:auto;padding:20px;
}
.md-body{line-height:1.7;color:var(--text)}
.md-body h1{font-size:1.5em;margin:0.8em 0 0.4em;border-bottom:1px solid var(--border);padding-bottom:0.3em}
.md-body h2{font-size:1.25em;margin:0.8em 0 0.3em}
.md-body h3{font-size:1.1em;margin:0.6em 0 0.2em}
.md-body p{margin:0.5em 0}
.md-body code{background:var(--surface2);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-family:monospace;font-size:0.88em}
.md-body pre{background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:12px;overflow-x:auto;margin:0.7em 0}
.md-body pre code{background:none;border:none;padding:0}
.md-body blockquote{border-left:3px solid var(--accent);padding:4px 12px;margin:0.5em 0;color:var(--muted);background:rgba(88,166,255,0.05);border-radius:0 4px 4px 0}
.md-body ul,.md-body ol{margin:0.4em 0 0.4em 1.4em}
.md-body li{margin:0.2em 0}
.md-body a{color:var(--accent);text-decoration:none}
.md-body a:hover{text-decoration:underline}
.md-body hr{border:none;border-top:1px solid var(--border);margin:1em 0}
.md-body strong{font-weight:700}
.md-body em{font-style:italic}
.md-body table{width:100%;border-collapse:collapse;margin:0.7em 0;font-size:0.9em}
.md-body th,.md-body td{border:1px solid var(--border);padding:6px 10px;text-align:left}
.md-body th{background:var(--surface2);font-weight:600}
.attach-row{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;margin:4px 0}
.attach-name{font-size:13px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ── REPLY BOX ───────────────────────────────────────── */
#reply-box{
  border-top:1px solid var(--border);padding:14px 20px;
  display:flex;flex-direction:column;gap:8px;
  background:var(--surface);
}
#reply-box label{font-size:12px;color:var(--muted);font-weight:600}
#reply-textarea{
  width:100%;min-height:80px;max-height:200px;resize:vertical;
  background:var(--surface2);border:1px solid var(--border);border-radius:6px;
  color:var(--text);font-size:13px;padding:9px 11px;outline:none;
  font-family:inherit;transition:border var(--transition);
}
#reply-textarea:focus{border-color:var(--accent)}
#reply-row{display:flex;align-items:center;gap:8px}
#reply-topic-input{
  flex:1;padding:7px 10px;background:var(--surface2);border:1px solid var(--border);
  border-radius:6px;color:var(--text);font-size:12px;outline:none;
}
#reply-topic-input:focus{border-color:var(--accent)}

/* ── COMPOSE MODAL ───────────────────────────────────── */
.modal-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,0.65);
  z-index:200;align-items:center;justify-content:center;
}
.modal-overlay.open{display:flex}
.modal{
  background:var(--surface);border:1px solid var(--border);border-radius:12px;
  width:min(560px, 95vw);max-height:90vh;overflow:auto;padding:24px;
  display:flex;flex-direction:column;gap:16px;
  box-shadow:0 20px 60px rgba(0,0,0,0.5);
}
.modal h3{font-size:16px;font-weight:700}
.modal label{font-size:12px;color:var(--muted);font-weight:600;display:block;margin-bottom:4px}
.modal input,.modal textarea,.modal select{
  width:100%;padding:8px 11px;background:var(--surface2);border:1px solid var(--border);
  border-radius:6px;color:var(--text);font-size:13px;outline:none;
  transition:border var(--transition);font-family:inherit;
}
.modal input:focus,.modal textarea:focus,.modal select:focus{border-color:var(--accent)}
.modal textarea{min-height:120px;resize:vertical}
.modal-row{display:flex;gap:10px}
.modal-row>div{flex:1}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:4px}
.form-group{display:flex;flex-direction:column;gap:4px}

/* ── TOAST ───────────────────────────────────────────── */
#toast-container{position:fixed;bottom:20px;right:20px;z-index:300;display:flex;flex-direction:column;gap:6px}
.toast{
  padding:10px 16px;background:var(--surface);border:1px solid var(--border);
  border-radius:8px;font-size:13px;box-shadow:0 4px 20px rgba(0,0,0,0.4);
  animation:slide-in 0.2s ease;max-width:300px;
}
.toast.success{border-color:var(--replied);color:var(--replied)}
.toast.error{border-color:var(--failed);color:var(--failed)}
.toast.info{border-color:var(--accent);color:var(--accent)}
@keyframes slide-in{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}

/* ── LOADER ──────────────────────────────────────────── */
.spinner{
  width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:spin 0.7s linear infinite;display:inline-block;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── SCROLLBAR ───────────────────────────────────────── */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}

/* ── MOBILE ──────────────────────────────────────────── */
@media(max-width:700px){
  #sidebar{display:none}
  #msg-list-pane{width:100%;border-right:none}
  #detail-pane{display:none;position:fixed;inset:0;z-index:50;background:var(--bg);padding-top:52px}
  #detail-pane.mobile-open{display:flex}
  .back-btn{display:inline-flex!important}
}
.back-btn{display:none;align-items:center;gap:5px;background:none;border:none;color:var(--accent);cursor:pointer;font-size:14px;padding:0 0 4px}
</style>
</head>
<body>

<!-- HEADER -->
<header>
  <div class="brand">
    <div class="dot"></div>
    aimail
  </div>
  <div class="hright">
    <div id="conn-indicator" title="SSE connection status"></div>
    <span class="port-badge" id="port-badge">:PORT</span>
    <button class="btn primary" onclick="openCompose()">✉ Compose</button>
  </div>
</header>

<!-- APP BODY -->
<div id="app">

  <!-- SIDEBAR -->
  <nav id="sidebar">
    <div class="sidebar-section">Mailbox</div>
    <div class="nav-tabs">
      <div class="nav-tab active" data-tab="inbox" onclick="switchTab(this,'inbox')">
        📥 Inbox <span class="tab-count" id="cnt-inbox">0</span>
      </div>
      <div class="nav-tab" data-tab="sent" onclick="switchTab(this,'sent')">
        📤 Sent <span class="tab-count" id="cnt-sent">0</span>
      </div>
      <div class="nav-tab" data-tab="all" onclick="switchTab(this,'all')">
        📋 All <span class="tab-count" id="cnt-all">0</span>
      </div>
    </div>
    <div id="sidebar-divider"></div>
    <div class="sidebar-section">Agents</div>
    <div class="agent-chips" id="agent-chips"></div>
  </nav>

  <!-- MAIN -->
  <div id="main">
    <!-- MESSAGE LIST -->
    <div id="msg-list-pane">
      <div id="list-header">
        <h2 id="list-title">Inbox</h2>
        <button class="btn sm" id="sort-toggle-btn" onclick="toggleSortOrder()" title="Toggle chronological order (Newest first / Oldest first)">⬇ Newest</button>
      </div>
      <div style="padding:8px 10px;border-bottom:1px solid var(--border)">
        <input type="search" id="search-box" placeholder="Search messages…" oninput="onSearch(this.value)"/>
      </div>
      <div id="msg-list">
        <div id="empty-state">
          <div class="empty-icon">📭</div>
          <span>No messages</span>
        </div>
      </div>
    </div>

    <!-- DETAIL PANE -->
    <div id="detail-pane">
      <div id="no-select">
        <div class="ns-icon">✉️</div>
        <span>Select a message to view</span>
      </div>
      <div id="detail-content">
        <div id="detail-header">
          <button class="back-btn" onclick="closeMobileDetail()">← Back</button>
          <div id="detail-topic">
            <span class="detail-id" id="detail-id-badge"></span>
            <span id="detail-topic-text">Topic</span>
          </div>
          <div id="detail-meta">
            <span class="meta-label">From:</span>
            <span class="meta-pill" id="dmeta-from"></span>
            <span class="arrow">→</span>
            <span class="meta-label">To:</span>
            <span class="meta-pill" id="dmeta-to"></span>
            <span id="dmeta-badge"></span>
            <span class="meta-pill" id="dmeta-time" style="margin-left:auto"></span>
          </div>
          <div id="detail-actions"></div>
        </div>
        <div id="detail-body" class="md-body"></div>
        <div id="reply-box">
          <label id="reply-box-label">Quick Reply</label>
          <div id="reply-row">
            <input id="reply-topic-input" type="text" placeholder="Topic (auto-filled)"/>
            <button class="btn sm" onclick="submitReply()">Send Reply</button>
            <button class="btn sm danger" id="btn-close-thread" onclick="closeThread()">Close</button>
          </div>
          <textarea id="reply-textarea" placeholder="Write your reply in markdown…"></textarea>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- COMPOSE MODAL -->
<div class="modal-overlay" id="compose-modal">
  <div class="modal">
    <h3>✉ New Message</h3>
    <div class="modal-row">
      <div class="form-group">
        <label>From</label>
        <select id="compose-from"></select>
      </div>
      <div class="form-group">
        <label>To</label>
        <select id="compose-to"></select>
      </div>
    </div>
    <div class="form-group">
      <label>Topic <small style="color:var(--muted)">(alphanumeric + underscore)</small></label>
      <input type="text" id="compose-topic" placeholder="e.g. task_request"/>
    </div>
    <div class="form-group">
      <label>Body (Markdown)</label>
      <textarea id="compose-body" placeholder="Write your message…"></textarea>
    </div>
    <div class="modal-actions">
      <button class="btn" onclick="closeCompose()">Cancel</button>
      <button class="btn primary" onclick="submitCompose()">Send Message</button>
    </div>
  </div>
</div>

<!-- TOAST CONTAINER -->
<div id="toast-container"></div>

<script>
// ════════════════════════════════════════════════════════
//  STATE
// ════════════════════════════════════════════════════════
let allMessages = [];
let allUsers = [];
let currentTab = 'inbox';
let currentAgent = null;
let selectedId = null;
let searchQuery = '';
let selfUser = null;  // best-guess "me" for reply defaults
let sortOrder = 'desc'; // 'desc' = newest first, 'asc' = oldest first

function toggleSortOrder() {
  sortOrder = sortOrder === 'desc' ? 'asc' : 'desc';
  const btn = document.getElementById('sort-toggle-btn');
  if (btn) btn.textContent = sortOrder === 'desc' ? '⬇ Newest' : '⬆ Oldest';
  renderList();
}

// ════════════════════════════════════════════════════════
//  BOOTSTRAP
// ════════════════════════════════════════════════════════
document.getElementById('port-badge').textContent = ':' + location.port;

async function init() {
  await Promise.all([loadUsers(), loadMessages()]);
  renderSidebar();
  renderList();
  connectSSE();
}
init();

// ════════════════════════════════════════════════════════
//  DATA FETCHING
// ════════════════════════════════════════════════════════
async function loadMessages(params = {}) {
  const qs = new URLSearchParams(params).toString();
  const url = '/api/messages' + (qs ? '?' + qs : '');
  const r = await fetch(url);
  if (!r.ok) return;
  allMessages = await r.json();
}

async function loadUsers() {
  const r = await fetch('/api/users');
  if (!r.ok) return;
  allUsers = await r.json();
  const human = allUsers.find(u => u.client_type === 'human');
  if (human) selfUser = human.user_id;
  else if (allUsers.length) selfUser = allUsers[0].user_id;
}

async function loadDetail(id) {
  const r = await fetch('/api/messages/' + id);
  if (!r.ok) { toast('Failed to load message', 'error'); return null; }
  return r.json();
}

// ════════════════════════════════════════════════════════
//  SSE  (live refresh)
// ════════════════════════════════════════════════════════
let sseRetryMs = 2000;
function connectSSE() {
  const es = new EventSource('/api/stream');
  es.onopen = () => { setConn(true); sseRetryMs = 2000; };
  es.onmessage = async (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'refresh') {
        await loadMessages();
        renderSidebar();
        renderList();
        if (selectedId) await refreshDetail(selectedId);
      }
    } catch {}
  };
  es.onerror = () => {
    setConn(false);
    es.close();
    setTimeout(connectSSE, sseRetryMs);
    sseRetryMs = Math.min(sseRetryMs * 2, 30000);
  };
}
function setConn(ok) {
  document.getElementById('conn-indicator').className = 'ok'.repeat(ok) || '';
  if (ok) document.getElementById('conn-indicator').classList.add('ok');
  else document.getElementById('conn-indicator').classList.remove('ok');
}

// ════════════════════════════════════════════════════════
//  FILTERING
// ════════════════════════════════════════════════════════
function getFiltered() {
  let msgs = allMessages;

  if (currentAgent) {
    if (currentTab === 'inbox') {
      msgs = allMessages.filter(m => m.receiver_id === currentAgent);
    } else if (currentTab === 'sent') {
      msgs = allMessages.filter(m => m.sender_id === currentAgent);
    } else { // 'all'
      msgs = allMessages.filter(m => m.sender_id === currentAgent || m.receiver_id === currentAgent);
    }
  } else {
    if (currentTab === 'inbox') {
      msgs = allMessages.filter(m => ['new', 'processing'].includes(m.status));
    } else if (currentTab === 'sent') {
      msgs = allMessages.filter(m => m.status === 'replied');
    } else { // 'all'
      msgs = allMessages;
    }
  }

  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    msgs = msgs.filter(m =>
      (m.topic && m.topic.toLowerCase().includes(q)) ||
      (m.sender_id && m.sender_id.toLowerCase().includes(q)) ||
      (m.receiver_id && m.receiver_id.toLowerCase().includes(q)) ||
      (String(m.id).includes(q))
    );
  }
  return msgs;
}

// ════════════════════════════════════════════════════════
//  RENDER — SIDEBAR
// ════════════════════════════════════════════════════════
function renderSidebar() {
  // Update counts
  if (currentAgent) {
    document.getElementById('cnt-inbox').textContent = allMessages.filter(m => m.receiver_id === currentAgent).length;
    document.getElementById('cnt-sent').textContent = allMessages.filter(m => m.sender_id === currentAgent).length;
    document.getElementById('cnt-all').textContent = allMessages.filter(m => m.sender_id === currentAgent || m.receiver_id === currentAgent).length;
  } else {
    document.getElementById('cnt-inbox').textContent = allMessages.filter(m => ['new','processing'].includes(m.status)).length;
    document.getElementById('cnt-sent').textContent = allMessages.filter(m => m.status === 'replied').length;
    document.getElementById('cnt-all').textContent = allMessages.length;
  }

  // Agent chips
  const agentSet = new Set();
  allUsers.forEach(u => agentSet.add(u.user_id));
  allMessages.forEach(m => {
    if (m.sender_id) agentSet.add(m.sender_id);
    if (m.receiver_id) agentSet.add(m.receiver_id);
  });

  const container = document.getElementById('agent-chips');
  if (!container) return;
  container.innerHTML = '';

  // "All agents" chip
  const allChip = document.createElement('div');
  allChip.className = 'agent-chip' + (currentAgent === null ? ' active' : '');
  allChip.innerHTML = '<div class="agent-avatar" style="background:var(--border)">★</div> All agents';
  allChip.onclick = () => selectAgent(null);
  container.appendChild(allChip);

  Array.from(agentSet).sort().forEach(uid => {
    const userInfo = allUsers.find(u => u.user_id === uid);
    const chip = document.createElement('div');
    chip.className = 'agent-chip' + (currentAgent === uid ? ' active' : '');
    const initials = uid.slice(0,2).toUpperCase();
    const hue = [...uid].reduce((a,c) => a + c.charCodeAt(0), 0) % 360;
    chip.innerHTML = `<div class="agent-avatar" style="background:hsl(${hue},45%,30%)">${initials}</div> ${esc(uid)}`;
    chip.title = userInfo ? userInfo.description : uid;
    chip.onclick = () => selectAgent(uid);
    container.appendChild(chip);
  });

  // Detect human user as selfUser if not set
  if (!selfUser) {
    const human = allUsers.find(u => u.client_type === 'human');
    if (human) selfUser = human.user_id;
  }
}

function selectAgent(uid) {
  currentAgent = uid;
  updateListTitle();
  renderSidebar();
  renderList();
}

function updateListTitle() {
  const titles = {inbox: 'Inbox', sent: 'Sent', all: 'All Messages'};
  let title = titles[currentTab] || currentTab;
  if (currentAgent) {
    title += ' · ' + currentAgent;
  }
  const el = document.getElementById('list-title');
  if (el) el.textContent = title;
}

// ════════════════════════════════════════════════════════
//  RENDER — MESSAGE LIST
// ════════════════════════════════════════════════════════
function renderList() {
  const msgs = getFiltered();
  const container = document.getElementById('msg-list');
  if (!container) return;

  if (!msgs.length) {
    container.innerHTML = `
      <div id="empty-state">
        <div class="empty-icon">📭</div>
        <span>No messages</span>
      </div>`;
    return;
  }

  container.innerHTML = '';
  const ordered = sortOrder === 'desc' ? msgs.slice().reverse() : msgs.slice();
  ordered.forEach(m => {
    const row = document.createElement('div');
    row.className = 'msg-row' + (m.id === selectedId ? ' selected' : '');
    row.dataset.id = m.id;

    const fromHue = [...m.sender_id].reduce((a,c) => a + c.charCodeAt(0), 0) % 360;
    const toHue = [...m.receiver_id].reduce((a,c) => a + c.charCodeAt(0), 0) % 360;
    const ts = formatTime(m.message_timestamp);
    row.innerHTML = `
      <div class="msg-row-top">
        <span class="msg-row-id">#${m.id}</span>
        <span class="msg-row-from">
          <span class="agent-tag" style="background:hsl(${fromHue},30%,20%);color:hsl(${fromHue},70%,75%)">${esc(m.sender_id)}</span>
          <span class="arrow">→</span>
          <span class="agent-tag" style="background:hsl(${toHue},30%,20%);color:hsl(${toHue},70%,75%)">${esc(m.receiver_id)}</span>
        </span>
        <span class="msg-row-time">${ts}</span>
      </div>
      <div class="msg-row-bottom">
        <span class="msg-row-topic">${esc(m.topic)}</span>
        ${badgeHtml(m.status)}
        ${m.attachment_path ? '<span style="font-size:10px;color:var(--muted)">📎</span>' : ''}
      </div>
    `;
    row.onclick = () => selectMessage(m.id);
    container.appendChild(row);
  });
}

// ════════════════════════════════════════════════════════
//  RENDER — DETAIL
// ════════════════════════════════════════════════════════
async function selectMessage(id) {
  selectedId = id;
  document.querySelectorAll('.msg-row').forEach(r => {
    r.classList.toggle('selected', +r.dataset.id === id);
  });

  document.getElementById('no-select').style.display = 'none';
  document.getElementById('detail-content').style.display = 'flex';

  // Mobile
  document.getElementById('detail-pane').classList.add('mobile-open');

  const data = await loadDetail(id);
  if (!data) return;
  renderDetail(data);
}

async function refreshDetail(id) {
  if (selectedId !== id) return;
  const data = await loadDetail(id);
  if (data) renderDetail(data);
}

function renderDetail(data) {
  const badgeEl = document.getElementById('detail-id-badge');
  if (badgeEl) badgeEl.textContent = '#' + data.id;
  const topicEl = document.getElementById('detail-topic-text') || document.getElementById('detail-topic');
  if (topicEl) topicEl.textContent = data.topic;
  document.getElementById('dmeta-from').textContent = data.sender_id;
  document.getElementById('dmeta-to').textContent = data.receiver_id;
  document.getElementById('dmeta-badge').innerHTML = badgeHtml(data.status);
  document.getElementById('dmeta-time').textContent = formatTime(data.message_timestamp);

  // Actions
  const actions = document.getElementById('detail-actions');
  actions.innerHTML = '';
  if (data.status === 'closed') {
    addAction(actions, '🔓 Reopen', () => markMsg(data.id, 'processing'));
  } else {
    if (data.status === 'new' || data.status === 'processing') {
      addAction(actions, '✓ Mark Read', () => markMsg(data.id, 'read'));
    }
    addAction(actions, '🔒 Close', () => markMsg(data.id, 'closed'));
  }
  if (data.status !== 'replied' && data.status !== 'closed') {
    addAction(actions, '↩ Mark Replied', () => markMsg(data.id, 'replied'));
  }

  // Body
  const body = document.getElementById('detail-body');
  body.innerHTML = '';

  if (data.attachment_path) {
    const fname = data.attachment_path.split('/').pop();
    const att = document.createElement('div');
    att.className = 'attach-row';
    att.innerHTML = `
      <span style="font-size:18px">📎</span>
      <span class="attach-name">${esc(fname)}</span>
      <a class="btn sm" href="/api/attachments/${data.id}" download="${esc(fname)}">Download</a>
    `;
    body.appendChild(att);
  }

  const mdDiv = document.createElement('div');
  mdDiv.className = 'md-body';
  mdDiv.innerHTML = renderMarkdown(data.body || '');
  body.appendChild(mdDiv);

  // Pre-fill reply
  const replyTopic = data.topic.endsWith('_reply') ? data.topic : data.topic + '_reply';
  document.getElementById('reply-topic-input').value = replyTopic;
  document.getElementById('reply-textarea').value = '';

  // Bottom reply box close button & label
  const btnClose = document.getElementById('btn-close-thread');
  const lblReply = document.getElementById('reply-box-label');
  if (data.status === 'closed') {
    if (btnClose) btnClose.style.display = 'none';
    if (lblReply) lblReply.innerHTML = 'Quick Reply <span style="font-size:11px;color:var(--muted);font-weight:normal">(Thread closed)</span>';
  } else {
    if (btnClose) btnClose.style.display = '';
    if (lblReply) lblReply.textContent = 'Quick Reply';
  }

  // Store for reply
  document.getElementById('reply-box').dataset.fromId = data.receiver_id;
  document.getElementById('reply-box').dataset.toId = data.sender_id;
  document.getElementById('reply-box').dataset.msgId = data.id;
}

function addAction(container, label, fn) {
  const b = document.createElement('button');
  b.className = 'btn sm';
  b.textContent = label;
  b.onclick = fn;
  container.appendChild(b);
}

// ════════════════════════════════════════════════════════
//  ACTIONS
// ════════════════════════════════════════════════════════
async function markMsg(id, status) {
  const r = await fetch('/api/messages/' + id + '/mark', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status})
  });
  if (r.ok) {
    toast('Status → ' + status, 'success');
    await loadMessages();
    renderList();
    await refreshDetail(id);
  } else {
    toast('Failed to update status', 'error');
  }
}

async function closeThread() {
  const box = document.getElementById('reply-box');
  const id = +box.dataset.msgId;
  if (id) await markMsg(id, 'closed');
}

async function submitReply() {
  const box = document.getElementById('reply-box');
  const from = box.dataset.fromId;
  const to = box.dataset.toId;
  const topic = document.getElementById('reply-topic-input').value.trim();
  const content = document.getElementById('reply-textarea').value.trim();
  if (!content) { toast('Reply body is empty', 'error'); return; }
  if (!topic) { toast('Topic is required', 'error'); return; }

  const r = await fetch('/api/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({from, to, topic, content})
  });
  if (r.ok) {
    toast('Reply sent!', 'success');
    document.getElementById('reply-textarea').value = '';
    // Mark original as replied
    const msgId = +box.dataset.msgId;
    if (msgId) await markMsg(msgId, 'replied');
    await loadMessages();
    renderList();
  } else {
    const err = await r.json().catch(() => ({}));
    toast('Send failed: ' + (err.error || 'unknown'), 'error');
  }
}

// ════════════════════════════════════════════════════════
//  COMPOSE
// ════════════════════════════════════════════════════════
function openCompose() {
  // Populate selects
  const selFrom = document.getElementById('compose-from');
  const selTo = document.getElementById('compose-to');
  selFrom.innerHTML = '';
  selTo.innerHTML = '';
  const ids = Array.from(new Set([...allUsers.map(u => u.user_id),
    ...allMessages.flatMap(m => [m.sender_id, m.receiver_id])])).sort();
  ids.forEach(uid => {
    selFrom.add(new Option(uid, uid));
    selTo.add(new Option(uid, uid));
  });
  if (selfUser) selFrom.value = selfUser;
  document.getElementById('compose-modal').classList.add('open');
}

function closeCompose() {
  document.getElementById('compose-modal').classList.remove('open');
}

async function submitCompose() {
  const from = document.getElementById('compose-from').value;
  const to = document.getElementById('compose-to').value;
  const topic = document.getElementById('compose-topic').value.trim();
  const content = document.getElementById('compose-body').value.trim();
  if (!from || !to || !topic || !content) { toast('All fields are required', 'error'); return; }
  if (!/^[A-Za-z0-9_]+$/.test(topic)) { toast('Topic: only letters, digits, underscores', 'error'); return; }

  const r = await fetch('/api/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({from, to, topic, content})
  });
  if (r.ok) {
    toast('Message sent!', 'success');
    closeCompose();
    document.getElementById('compose-topic').value = '';
    document.getElementById('compose-body').value = '';
    await loadMessages();
    renderList();
  } else {
    const err = await r.json().catch(() => ({}));
    toast('Send failed: ' + (err.error || 'unknown'), 'error');
  }
}

// Close modal on overlay click
document.getElementById('compose-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeCompose();
});

// ════════════════════════════════════════════════════════
//  UI HELPERS
// ════════════════════════════════════════════════════════
function switchTab(el, tab) {
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  currentTab = tab;
  updateListTitle();
  renderSidebar();
  renderList();
}

function onSearch(val) {
  searchQuery = val.trim();
  renderList();
}

function closeMobileDetail() {
  document.getElementById('detail-pane').classList.remove('mobile-open');
  selectedId = null;
}

function badgeHtml(status) {
  const cls = {new:'new',processing:'processing',replied:'replied',closed:'closed',failed:'failed',read:'read'}[status] || 'read';
  return `<span class="badge badge-${cls}">${status}</span>`;
}

function formatTime(ts) {
  if (!ts) return '';
  // Format: 20261103_181500 or ISO
  if (/^\d{8}_\d{6}$/.test(ts)) {
    return ts.slice(4,6)+'-'+ts.slice(6,8)+' '+ts.slice(9,11)+':'+ts.slice(11,13);
  }
  // ISO-like
  try { return ts.slice(5,16).replace('T',' '); } catch { return ts; }
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ════════════════════════════════════════════════════════
//  MINIMAL MARKDOWN RENDERER
// ════════════════════════════════════════════════════════
function renderMarkdown(md) {
  if (!md) return '';
  const lines = md.split('\n');
  let html = '';
  let inCode = false, codeLang = '', codeLines = [];
  let inUl = false, inOl = false;

  function flushList() {
    if (inUl) { html += '</ul>'; inUl = false; }
    if (inOl) { html += '</ol>'; inOl = false; }
  }

  function inlineProcess(line) {
    // Escape HTML first
    line = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    // Bold + italic
    line = line.replace(/\*\*\*(.+?)\*\*\*/g,'<strong><em>$1</em></strong>');
    line = line.replace(/___(.+?)___/g,'<strong><em>$1</em></strong>');
    line = line.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
    line = line.replace(/__(.+?)__/g,'<strong>$1</strong>');
    line = line.replace(/\*(.+?)\*/g,'<em>$1</em>');
    line = line.replace(/_(.+?)_/g,'<em>$1</em>');
    // Inline code
    line = line.replace(/`([^`]+)`/g,'<code>$1</code>');
    // Links
    line = line.replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
    // Strikethrough
    line = line.replace(/~~(.+?)~~/g,'<del>$1</del>');
    return line;
  }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];

    // Fenced code blocks
    if (/^```/.test(raw)) {
      if (!inCode) {
        flushList();
        codeLang = raw.slice(3).trim();
        codeLines = [];
        inCode = true;
      } else {
        const escaped = codeLines.join('\n').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        html += `<pre><code class="lang-${esc(codeLang)}">${escaped}</code></pre>`;
        inCode = false;
      }
      continue;
    }
    if (inCode) { codeLines.push(raw); continue; }

    // HR
    if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(raw)) { flushList(); html += '<hr>'; continue; }

    // Headings
    const hm = raw.match(/^(#{1,6})\s+(.*)/);
    if (hm) { flushList(); const lvl=hm[1].length; html += `<h${lvl}>${inlineProcess(hm[2])}</h${lvl}>`; continue; }

    // Blockquote
    if (/^>/.test(raw)) {
      flushList();
      const bq = raw.replace(/^>\s?/, '');
      html += `<blockquote>${inlineProcess(bq)}</blockquote>`;
      continue;
    }

    // Ordered list
    if (/^\d+\.\s/.test(raw)) {
      if (inUl) { html += '</ul>'; inUl = false; }
      if (!inOl) { html += '<ol>'; inOl = true; }
      html += `<li>${inlineProcess(raw.replace(/^\d+\.\s/, ''))}</li>`;
      continue;
    }

    // Unordered list
    if (/^[-*+]\s/.test(raw)) {
      if (inOl) { html += '</ol>'; inOl = false; }
      if (!inUl) { html += '<ul>'; inUl = true; }
      html += `<li>${inlineProcess(raw.replace(/^[-*+]\s/, ''))}</li>`;
      continue;
    }

    // Blank line
    if (!raw.trim()) { flushList(); html += ''; continue; }

    // Paragraph
    flushList();
    html += `<p>${inlineProcess(raw)}</p>`;
  }

  flushList();
  if (inCode && codeLines.length) {
    const escaped = codeLines.join('\n').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    html += `<pre><code>${escaped}</code></pre>`;
  }
  return html;
}

// ════════════════════════════════════════════════════════
//  TOAST
// ════════════════════════════════════════════════════════
function toast(msg, type = 'info', ms = 3000) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), ms);
}
</script>
</body>
</html>"""

    # -------------------------------------------------------------------------
    # HTTP  REQUEST HANDLER
    # -------------------------------------------------------------------------
    class AimailHandler(BaseHTTPRequestHandler):
        """Single handler for the aimail web UI + JSON API."""

        # -- SSE state (class-level) ------------------------------------------
        _sse_lock = threading.Lock()
        _last_row_count = -1
        _last_updated_at = None

        def log_message(self, fmt, *a):
            # Minimal console output
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"  [{ts}] {fmt % a}")

        # ── routing ──────────────────────────────────────────────────────────
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if path == "/":
                self._send_html(HTML_TEMPLATE.encode())
            elif path == "/api/messages":
                self._api_messages(qs)
            elif re.match(r"^/api/messages/\d+$", path):
                mid = int(path.split("/")[-1])
                self._api_message_detail(mid)
            elif path == "/api/users":
                self._api_users()
            elif path == "/api/stream":
                self._api_stream()
            elif re.match(r"^/api/attachments/\d+$", path):
                mid = int(path.split("/")[-1])
                self._api_attachment(mid)
            else:
                self._send_json({"error": "not found"}, 404)

        def do_HEAD(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/":
                data = HTML_TEMPLATE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")

            if re.match(r"^/api/messages/\d+/mark$", path):
                mid = int(path.split("/")[-2])
                self._api_mark(mid)
            elif path == "/api/send":
                self._api_send()
            else:
                self._send_json({"error": "not found"}, 404)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        # ── helpers ───────────────────────────────────────────────────────────
        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length)

        def _send_html(self, data: bytes, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, obj, code: int = 200):
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def _db(self):
            conn = get_connection(messages_dir)
            return conn

        # ── API: GET /api/messages ────────────────────────────────────────────
        def _api_messages(self, qs):
            conn = self._db()
            cur = conn.cursor()
            query = "SELECT * FROM messages WHERE 1=1"
            params = []
            status = (qs.get("status") or [None])[0]
            to = (qs.get("to") or [None])[0]
            frm = (qs.get("from") or [None])[0]
            limit = (qs.get("limit") or [None])[0]

            if status and status != "all":
                query += " AND status = ?"
                params.append(status)
            if to:
                query += " AND receiver_id = ?"
                params.append(to)
            if frm:
                query += " AND sender_id = ?"
                params.append(frm)
            query += " ORDER BY message_timestamp ASC"
            if limit:
                try:
                    query += f" LIMIT {int(limit)}"
                except ValueError:
                    pass

            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            self._send_json(rows)

        # ── API: GET /api/messages/<id> ───────────────────────────────────────
        def _api_message_detail(self, mid):
            conn = self._db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM messages WHERE id = ?", (mid,))
            row = cur.fetchone()
            conn.close()
            if not row:
                self._send_json({"error": "not found"}, 404)
                return
            data = dict(row)
            # Attach body text
            md_path = messages_dir / row["md_file_path"]
            if not md_path.exists():
                md_path = messages_dir / MARKDOWN_SUBDIR / Path(row["md_file_path"]).name
            try:
                data["body"] = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
            except Exception:
                data["body"] = ""
            self._send_json(data)

        # ── API: GET /api/users ───────────────────────────────────────────────
        def _api_users(self):
            conn = self._db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM users ORDER BY user_id ASC")
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            self._send_json(rows)

        # ── API: POST /api/messages/<id>/mark ─────────────────────────────────
        def _api_mark(self, mid):
            try:
                body = json.loads(self._read_body())
                new_status = body.get("status", "")
            except Exception:
                self._send_json({"error": "invalid json"}, 400)
                return
            valid = ("new", "processing", "read", "replied", "closed", "failed")
            if new_status not in valid:
                self._send_json({"error": f"status must be one of {valid}"}, 400)
                return
            conn = self._db()
            cur = conn.cursor()
            cur.execute("UPDATE messages SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (new_status, mid))
            if cur.rowcount == 0:
                conn.close()
                self._send_json({"error": "not found"}, 404)
                return
            conn.commit()
            conn.close()
            self._send_json({"ok": True, "id": mid, "status": new_status})

        # ── API: POST /api/send ───────────────────────────────────────────────
        def _api_send(self):
            try:
                body = json.loads(self._read_body())
            except Exception:
                self._send_json({"error": "invalid json"}, 400)
                return
            frm = str(body.get("from", "")).strip()
            to = str(body.get("to", "")).strip()
            topic = str(body.get("topic", "")).strip()
            content = str(body.get("content", "")).strip()

            if not all([frm, to, topic, content]):
                self._send_json({"error": "from, to, topic, content are required"}, 400)
                return
            if not re.match(r"^[A-Za-z0-9_-]+$", topic):
                self._send_json({"error": "topic must be alphanumeric+underscore"}, 400)
                return

            conn = self._db()
            frm = get_canonical_user_id(conn, frm)
            to = get_canonical_user_id(conn, to)
            sender_u = resolve_user(conn, frm)
            receiver_u = resolve_user(conn, to)
            sender_iid = sender_u["internal_id"] if sender_u else None
            receiver_iid = receiver_u["internal_id"] if receiver_u else None

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            md_dir = messages_dir / MARKDOWN_SUBDIR
            md_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{frm}-{to}-{topic}-{ts}.md"
            md_path = md_dir / filename
            db_md_path = f"{MARKDOWN_SUBDIR}/{filename}"

            try:
                md_path.write_text(content, encoding="utf-8")
            except Exception as e:
                conn.close()
                self._send_json({"error": f"write failed: {e}"}, 500)
                return

            cur = conn.cursor()
            cur.execute(
                "INSERT INTO messages (sender_id, receiver_id, sender_internal_id, receiver_internal_id, topic, message_timestamp, md_file_path, status) VALUES (?,?,?,?,?,?,?, 'new')",
                (frm, to, sender_iid, receiver_iid, topic, ts, db_md_path),
            )
            new_id = cur.lastrowid
            conn.commit()
            conn.close()

            # Trigger wakeup in background thread
            def _wake():
                try:
                    trigger_wakeup(to, frm, topic, db_md_path, messages_dir)
                except Exception:
                    pass
            threading.Thread(target=_wake, daemon=True).start()

            self._send_json({"ok": True, "id": new_id})

        # ── API: GET /api/attachments/<id> ────────────────────────────────────
        def _api_attachment(self, mid):
            conn = self._db()
            cur = conn.cursor()
            cur.execute("SELECT attachment_path FROM messages WHERE id = ?", (mid,))
            row = cur.fetchone()
            conn.close()
            if not row or not row["attachment_path"]:
                self._send_json({"error": "no attachment"}, 404)
                return
            att_path = messages_dir / row["attachment_path"]
            if not att_path.exists():
                att_path = messages_dir / ATTACHMENTS_SUBDIR / Path(row["attachment_path"]).name
            if not att_path.exists():
                self._send_json({"error": "file not found"}, 404)
                return
            data = att_path.read_bytes()
            fname = att_path.name
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ── API: GET /api/stream  (SSE) ───────────────────────────────────────
        def _api_stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            last_count = -1
            last_upd = None

            def _db_state():
                try:
                    conn = self._db()
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*), MAX(updated_at) FROM messages")
                    row = cur.fetchone()
                    conn.close()
                    return row[0], row[1]
                except Exception:
                    return -1, None

            try:
                while True:
                    count, upd = _db_state()
                    if count != last_count or upd != last_upd:
                        if last_count != -1 or last_upd is not None:
                            msg = b'data: {"type":"refresh"}\n\n'
                        else:
                            msg = b'data: {"type":"connected"}\n\n'
                        self.wfile.write(msg)
                        self.wfile.flush()
                        last_count = count
                        last_upd = upd
                    time.sleep(2)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # START SERVER
    # -------------------------------------------------------------------------
    # Ensure DB is initialised
    init_db(get_db_path(messages_dir))

    # Check port availability
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex((host, port)) == 0:
            print(f"Error: Port {port} is already in use. Try --port <other>.", file=sys.stderr)
            sys.exit(1)

    server = ThreadingHTTPServer((host, port), AimailHandler)
    url = f"http://{host}:{port}"
    print(f"  aimail web UI  →  {url}")
    print(f"  Messages dir   →  {messages_dir}")
    print(f"  Press Ctrl+C to stop.\n")

    if getattr(args, "open", False):
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down aimail server.")
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Message Control CLI for agent communication.")
    parser.add_argument("--dir", default=str(DEFAULT_MESSAGES_DIR), help="Directory containing messages")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # sync
    subparsers.add_parser("sync", help="Scan messages directory and register new messages into SQLite")

    # list
    p_list = subparsers.add_parser("list", help="List messages")
    p_list.add_argument("--to", help="Filter by receiver ID (e.g. oschan)")
    p_list.add_argument("--sender", help="Filter by sender ID")
    p_list.add_argument("--status", choices=["new", "processing", "read", "replied", "closed", "failed", "all"], default="all", help="Filter by status (default: all)")
    p_list.add_argument("--limit", type=int, default=20, help="Max records to return")
    p_list.add_argument("--style", choices=["box", "clean", "plain"], default=None, help="Table style: box (default in terminal), clean, plain")

    # mark
    p_mark = subparsers.add_parser("mark", help="Update message status")
    p_mark.add_argument("target", help="Message ID or filename")
    p_mark.add_argument("--status", choices=["new", "processing", "read", "replied", "closed", "failed"], required=True, help="New status")
    p_mark.add_argument("--no-wakeup", action="store_true", help="Skip triggering sender's wakeup on reply")

    # close
    p_close = subparsers.add_parser("close", help="Mark a message thread as 'closed' without triggering any wakeup (prevents reply loops)")
    p_close.add_argument("target", help="Message ID or filename to close")

    # reply
    p_reply = subparsers.add_parser("reply", help="Reply to a message, automatically marking original as replied and waking sender")
    p_reply.add_argument("target", help="Original message ID or filename to reply to")
    p_reply.add_argument("--from", dest="sender", help="Reply sender ID (defaults to original receiver)")
    
    content_group_reply = p_reply.add_mutually_exclusive_group(required=True)
    content_group_reply.add_argument("--file", "-f", help="Path to reply source markdown file")
    content_group_reply.add_argument("--content", "-c", help="Direct markdown text string content")

    p_reply.add_argument("--attach", "-a", help="Path to attachment file (e.g. tarball .tar.xz)")
    p_reply.add_argument("--erase-original", "--erase", action="store_true", help="Erase original file(s) after sending")
    p_reply.add_argument("--timestamp", help="Custom timestamp (default: current local timestamp YYYYMMDD_HHMMSS)")
    p_reply.add_argument("--no-wakeup", action="store_true", help="Skip triggering sender's wakeup")

    # show
    p_show = subparsers.add_parser("show", help="Show message details and content")
    p_show.add_argument("target", help="Message ID or filename")
    p_show.add_argument("--out", "-o", help="Save markdown content directly to specified file path")

    # send
    p_send = subparsers.add_parser("send", help="Send a message by copying markdown or passing direct string content")
    p_send.add_argument("--from", dest="sender", required=True, help="Sender ID")
    p_send.add_argument("--to", dest="receiver", required=True, help="Receiver ID")
    p_send.add_argument("--topic", required=True, help="Message topic")
    
    content_group = p_send.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--file", "-f", help="Path to source markdown file")
    content_group.add_argument("--content", "-c", help="Direct markdown text string content")

    p_send.add_argument("--attach", "-a", help="Path to attachment file (e.g. tarball .tar.xz)")
    p_send.add_argument("--erase-original", "--erase", action="store_true", help="Erase original file(s) after sending (applies when using --file / --attach)")
    p_send.add_argument("--timestamp", help="Custom timestamp (default: current local timestamp YYYYMMDD_HHMMSS)")
    p_send.add_argument("--no-wakeup", action="store_true", help="Skip triggering recipient's automatic wakeup command")

    # inspect
    p_ins = subparsers.add_parser("inspect", help="Open the interactive TUI for an agent or message participant (agy or opencode)")
    p_ins.add_argument("target", nargs="?", default="last", help="Message ID, topic substring, agent ID, or 'last' (default: last)")
    role_group = p_ins.add_mutually_exclusive_group()
    role_group.add_argument("--sender", "-s", action="store_true", help="Inspect the sender of the message")
    role_group.add_argument("--receiver", "-r", action="store_true", help="Inspect the receiver of the message (default)")
    p_ins.add_argument("--window", "-w", action="store_true", help="Open TUI in a new macOS Terminal window rather than in-place")

    # download
    p_dl = subparsers.add_parser("download", help="Download (copy) attachment or markdown body directly into your workspace")
    p_dl.add_argument("target", help="Message ID or topic/filename")
    p_dl.add_argument("--out", "-o", default=".", help="Destination file path or directory (default: current directory)")
    p_dl.add_argument("--body", action="store_true", help="Download the message markdown body instead of the attachment")
    p_dl.add_argument("--extract", "-x", action="store_true", help="Automatically unpack archive attachments into destination directory")

    # extract
    p_ext = subparsers.add_parser("extract", help="Extract or copy a message's attachment to a target directory")
    p_ext.add_argument("target", help="Message ID or filename")
    p_ext.add_argument("--out", "-o", default=".", help="Output directory to extract/copy into (default: current directory)")

    # revoke
    p_revoke = subparsers.add_parser("revoke", help="Revoke/unsend a sent message and delete its files from the recipient inbox")
    p_revoke.add_argument("target", nargs="?", default="last", help="Message ID, topic substring, filename, or 'last' (default: last)")
    p_revoke.add_argument("--from", dest="sender", help="Sender ID (to revoke the last message sent by this specific user)")
    p_revoke.add_argument("--last", action="store_true", help="Revoke the last sent message")
    p_revoke.add_argument("--force", "-f", action="store_true", help="Force revoke even if the message was already marked as read or replied")

    # user
    p_user = subparsers.add_parser("user", help="Manage and discover agent users in the directory")
    p_user_sub = p_user.add_subparsers(dest="user_action", required=True)

    # user register
    p_u_reg = p_user_sub.add_parser("register", help="Register or update an agent user")
    p_u_reg.add_argument("--id", required=True, help="User/Agent ID (e.g. oschan, kuro)")
    p_u_reg.add_argument("--client", choices=["agy", "opencode", "custom", "human"], default="agy", help="Client runtime type: agy, opencode, custom, human (default: agy)")
    p_u_reg.add_argument("--desc", required=True, help="Description of the user/agent")
    p_u_reg.add_argument("--keywords", required=True, help="Comma-separated keywords/capabilities")
    p_u_reg.add_argument("--conv-id", help="Conversation ID for agy/opencode resume")
    p_u_reg.add_argument("--wakeup-cmd", help="Custom wakeup shell command template (supports {sender}, {topic}, {prompt}, {conv_id}, {client})")

    # user list
    p_u_list = p_user_sub.add_parser("list", help="List all registered agents and users")
    p_u_list.add_argument("--style", choices=["box", "clean", "plain"], default=None, help="Table style: box (default in terminal), clean, plain")
    p_u_list.add_argument("-v", "--verbose", action="store_true", help="Show full IDs and detailed properties")

    # user show
    p_u_show = p_user_sub.add_parser("show", help="Show details for a specific user")
    p_u_show.add_argument("id", help="User ID")

    # user find
    p_u_find = p_user_sub.add_parser("find", help="Search users by keyword or capability")
    p_u_find.add_argument("query", help="Keyword or search query")
    p_u_find.add_argument("--style", choices=["box", "clean", "plain"], default=None, help="Table style: box (default in terminal), clean, plain")
    p_u_find.add_argument("-v", "--verbose", action="store_true", help="Show full IDs and detailed properties")

    # user rename
    p_u_ren = p_user_sub.add_parser("rename", help="Rename an agent user handle while preserving internal ID and message history")
    p_u_ren.add_argument("old_name", help="Current username, alias, or internal ID (e.g. antigravity)")
    p_u_ren.add_argument("new_name", help="New username handle (e.g. gymchan)")

    # shortcut: aimail rename <old> <new>
    p_ren = subparsers.add_parser("rename", help="Shortcut to rename an agent user (same as 'aimail user rename <old> <new>')")
    p_ren.add_argument("old_name", help="Current username, alias, or internal ID (e.g. antigravity)")
    p_ren.add_argument("new_name", help="New username handle (e.g. gymchan)")

    # shortcut: aimail users
    p_users = subparsers.add_parser("users", help="Shortcut to list all registered users (same as 'aimail user list')")
    p_users.add_argument("--style", choices=["box", "clean", "plain"], default=None, help="Table style: box (default in terminal), clean, plain")
    p_users.add_argument("-v", "--verbose", action="store_true", help="Show full IDs and detailed properties")

    # serve
    p_serve = subparsers.add_parser("serve", help="Start the aimail web UI dashboard")
    p_serve.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    p_serve.add_argument("--host", default="127.0.0.1", help="Host address to bind to (default: 127.0.0.1)")
    p_serve.add_argument("--open", action="store_true", help="Automatically open browser on launch")

    # ui (shortcut to serve --open)
    p_ui = subparsers.add_parser("ui", help="Launch the aimail web dashboard and open it in the browser")
    p_ui.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    p_ui.add_argument("--host", default="127.0.0.1", help="Host address to bind to (default: 127.0.0.1)")

    args = parser.parse_args()

    if args.command == "sync":
        cmd_sync(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "mark":
        cmd_mark(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "inspect":
        cmd_inspect(args)
    elif args.command == "send":
        cmd_send(args)
    elif args.command == "reply":
        cmd_reply(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "extract":
        cmd_extract(args)
    elif args.command == "close":
        cmd_close(args)
    elif args.command == "revoke":
        cmd_revoke(args)
    elif args.command == "rename":
        args.user_action = "rename"
        cmd_user(args)
    elif args.command == "user":
        cmd_user(args)
    elif args.command == "users":
        cmd_users(args)
    elif args.command in ("serve", "ui"):
        if args.command == "ui":
            args.open = True
        cmd_serve(args)


if __name__ == "__main__":
    main()
