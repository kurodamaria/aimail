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
                description TEXT NOT NULL,
                keywords TEXT NOT NULL,
                client_type TEXT DEFAULT 'agy',
                conversation_id TEXT,
                wakeup_cmd TEXT,
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

        # Seed initial users if empty
        cursor.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] == 0:
            cursor.executemany("""
                INSERT INTO users (user_id, description, keywords, client_type, conversation_id, wakeup_cmd)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                ("kuro", "Real human user, developer, and system owner.", "human, developer, owner, operator, feedback, decisions", "human", None, 'osascript -e \'display notification "{prompt}" with title "aimail: message from {sender}"\''),
                ("oschan", "AI assistant and autonomous agent for background task execution, code review, and development.", "agent, python, coding, git, review, automation, testing", "agy", None, None)
            ])
        conn.commit()


def get_connection(messages_dir: Path) -> sqlite3.Connection:
    db_path = get_db_path(messages_dir)
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_sync(args):
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

        sender = match.group("sender")
        receiver = match.group("receiver")
        topic = match.group("topic")
        timestamp = match.group("timestamp")

        # Check for corresponding attachment in attachments_dir
        stem = entry.stem
        attachment_path = None
        for cand in att_dir.glob(f"{stem}.*"):
            attachment_path = f"{ATTACHMENTS_SUBDIR}/{cand.name}"
            break

        if rel_path not in indexed_files:
            cursor.execute("""
                INSERT INTO messages (sender_id, receiver_id, topic, message_timestamp, md_file_path, attachment_path, status)
                VALUES (?, ?, ?, ?, ?, ?, 'new')
            """, (sender, receiver, topic, timestamp, rel_path, attachment_path))
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

    print(f"Sync complete. New messages registered: {added}, attachments updated: {updated_attachments}")


def cmd_list(args):
    messages_dir = Path(args.dir).resolve()
    # Auto-sync before listing
    cmd_sync(args)

    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    query = "SELECT * FROM messages WHERE 1=1"
    params = []

    if args.to:
        query += " AND receiver_id = ?"
        params.append(args.to)
    if args.sender:
        query += " AND sender_id = ?"
        params.append(args.sender)
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

    # Print table header
    print(f"{'ID':<4} {'STATUS':<12} {'SENDER':<12} {'RECEIVER':<12} {'TOPIC':<20} {'TIMESTAMP':<18} {'ATTACHMENT':<12} {'FILE'}")
    print("-" * 115)
    for row in rows:
        attach_str = "yes" if row["attachment_path"] else "-"
        print(f"{row['id']:<4} {row['status']:<12} {row['sender_id']:<12} {row['receiver_id']:<12} {row['topic']:<20} {row['message_timestamp']:<18} {attach_str:<12} {row['md_file_path']}")


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
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, client_type, conversation_id, wakeup_cmd FROM users WHERE user_id = ?", (receiver,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return

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
                receiver=receiver,
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
            final_cmd = f'opencode -s "{conv_id}" --prompt "{escaped_prompt}"'
        else:
            final_cmd = f'agy --conversation "{conv_id}" -p "{escaped_prompt}"'
    elif client_type == "human" or receiver == "kuro":
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
            print(f"Triggered background wakeup for '{receiver}'.")
        except Exception as e:
            print(f"Warning: Failed to execute wakeup for '{receiver}': {e}", file=sys.stderr)


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

    sender = args.sender
    receiver = args.receiver
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

    # Insert into database
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    # Informative notice if receiver is not registered
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (receiver,))
    if not cursor.fetchone():
        print(f"Notice: Receiver '{receiver}' is not in user directory. Run 'aimail users' to see registered agents.")

    cursor.execute("""
        INSERT INTO messages (sender_id, receiver_id, topic, message_timestamp, md_file_path, attachment_path, status)
        VALUES (?, ?, ?, ?, ?, ?, 'new')
    """, (sender, receiver, topic, ts, db_md_path, db_attach_path))
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

        cursor.execute("""
            INSERT INTO users (user_id, description, keywords, client_type, conversation_id, wakeup_cmd, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                description=excluded.description,
                keywords=excluded.keywords,
                client_type=excluded.client_type,
                conversation_id=COALESCE(excluded.conversation_id, users.conversation_id),
                wakeup_cmd=COALESCE(excluded.wakeup_cmd, users.wakeup_cmd),
                updated_at=CURRENT_TIMESTAMP
        """, (user_id, desc, keywords, client_type, conv_id, wakeup_cmd))
        conn.commit()
        print(f"User '{user_id}' ({client_type}) registered/updated successfully.")

    elif action == "show":
        user_id = args.id.strip()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            print(f"User '{user_id}' not found.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        print("=" * 60)
        print(f"User ID:         {row['user_id']}")
        print(f"Client Type:     {row['client_type'] or 'agy'}")
        print(f"Description:     {row['description']}")
        print(f"Keywords:        {row['keywords']}")
        print(f"Conversation ID: {row['conversation_id'] or 'None'}")
        print(f"Wakeup Command:  {row['wakeup_cmd'] or 'None'}")
        print(f"Registered:      {row['registered_at']}")
        print(f"Last Updated:    {row['updated_at']}")
        print("=" * 60)

    elif action == "find":
        query = f"%{args.query.strip()}%"
        cursor.execute("""
            SELECT * FROM users 
            WHERE user_id LIKE ? OR description LIKE ? OR keywords LIKE ? OR client_type LIKE ?
            ORDER BY user_id ASC
        """, (query, query, query, query))
        rows = cursor.fetchall()
        if not rows:
            print(f"No users found matching '{args.query}'.")
        else:
            print(f"{'USER ID':<10} {'CLIENT':<10} {'WAKEUP / CONV_ID':<26} {'KEYWORDS':<28} {'DESCRIPTION'}")
            print("-" * 105)
            for r in rows:
                c_type = r['client_type'] or 'agy'
                wakeup_display = r['wakeup_cmd'] or (f"{c_type}:{r['conversation_id']}" if r['conversation_id'] else "none")
                if len(wakeup_display) > 24:
                    wakeup_display = wakeup_display[:21] + "..."
                print(f"{r['user_id']:<10} {c_type:<10} {wakeup_display:<26} {r['keywords']:<28} {r['description']}")

    elif action == "list":
        cursor.execute("SELECT * FROM users ORDER BY user_id ASC")
        rows = cursor.fetchall()
        if not rows:
            print("No users registered in directory.")
        else:
            print(f"{'USER ID':<10} {'CLIENT':<10} {'WAKEUP / CONV_ID':<26} {'KEYWORDS':<28} {'DESCRIPTION'}")
            print("-" * 105)
            for r in rows:
                c_type = r['client_type'] or 'agy'
                wakeup_display = r['wakeup_cmd'] or (f"{c_type}:{r['conversation_id']}" if r['conversation_id'] else "none")
                if len(wakeup_display) > 24:
                    wakeup_display = wakeup_display[:21] + "..."
                print(f"{r['user_id']:<10} {c_type:<10} {wakeup_display:<26} {r['keywords']:<28} {r['description']}")

    conn.close()


def cmd_users(args):
    args.user_action = "list"
    cmd_user(args)


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
    p_user_sub.add_parser("list", help="List all registered agents and users")

    # user show
    p_u_show = p_user_sub.add_parser("show", help="Show details for a specific user")
    p_u_show.add_argument("id", help="User ID")

    # user find
    p_u_find = p_user_sub.add_parser("find", help="Search users by keyword or capability")
    p_u_find.add_argument("query", help="Keyword or search query")

    # shortcut: aimail users
    subparsers.add_parser("users", help="Shortcut to list all registered users (same as 'aimail user list')")

    args = parser.parse_args()

    if args.command == "sync":
        cmd_sync(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "mark":
        cmd_mark(args)
    elif args.command == "show":
        cmd_show(args)
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
    elif args.command == "user":
        cmd_user(args)
    elif args.command == "users":
        cmd_users(args)


if __name__ == "__main__":
    main()
