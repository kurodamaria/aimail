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
                status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'read', 'replied')),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_inbox
            ON messages(receiver_id, status, message_timestamp);
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                keywords TEXT NOT NULL,
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

        # Seed initial users if empty
        cursor.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] == 0:
            cursor.executemany("""
                INSERT INTO users (user_id, description, keywords, conversation_id, wakeup_cmd)
                VALUES (?, ?, ?, ?, ?)
            """, [
                ("kuro", "Real human user, developer, and system owner.", "human, developer, owner, operator, feedback, decisions", None, 'osascript -e \'display notification "{prompt}" with title "aimail: message from {sender}"\''),
                ("oschan", "AI assistant and autonomous agent for background task execution, code review, and development.", "agent, python, coding, git, review, automation, testing", None, None)
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
    print(f"{'ID':<4} {'STATUS':<8} {'SENDER':<12} {'RECEIVER':<12} {'TOPIC':<16} {'TIMESTAMP':<18} {'ATTACHMENT':<12} {'FILE'}")
    print("-" * 105)
    for row in rows:
        attach_str = "yes" if row["attachment_path"] else "-"
        print(f"{row['id']:<4} {row['status']:<8} {row['sender_id']:<12} {row['receiver_id']:<12} {row['topic']:<16} {row['message_timestamp']:<18} {attach_str:<12} {row['md_file_path']}")


def cmd_mark(args):
    messages_dir = Path(args.dir).resolve()
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    target = args.target
    new_status = args.status

    if target.isdigit():
        cursor.execute("""
            UPDATE messages
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_status, int(target)))
    else:
        cursor.execute("""
            UPDATE messages
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE md_file_path = ? OR md_file_path LIKE ?
        """, (new_status, target, f"%{target}%"))

    if cursor.rowcount == 0:
        print(f"No message found matching identifier '{target}'.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.commit()
    conn.close()
    print(f"Message '{target}' marked as '{new_status}'.")


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
    else:
        print(f"\nWarning: File {file_path} not found on disk.")


def trigger_wakeup(receiver: str, sender: str, topic: str, file_path: str, messages_dir: Path):
    conn = get_connection(messages_dir)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, conversation_id, wakeup_cmd FROM users WHERE user_id = ?", (receiver,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return

    conv_id = row["conversation_id"]
    wakeup_template = row["wakeup_cmd"]

    prompt = f"New aimail received from {sender} on topic '{topic}'. Check inbox with 'aimail list --to {receiver} --status new' and 'aimail show {topic}'."

    final_cmd = None
    if wakeup_template:
        try:
            final_cmd = wakeup_template.format(
                sender=sender,
                receiver=receiver,
                topic=topic,
                file=file_path,
                prompt=prompt,
                conv_id=conv_id or ""
            )
        except (KeyError, IndexError):
            final_cmd = wakeup_template
    elif conv_id:
        escaped_prompt = prompt.replace('"', '\\"')
        final_cmd = f'agy --conversation "{conv_id}" -p "{escaped_prompt}"'
    elif receiver == "kuro":
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
        # Preserve original extension suffix (e.g. .tar.xz)
        if source_attach.name.endswith(".tar.xz"):
            ext = ".tar.xz"
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


def cmd_user(args):
    messages_dir = Path(args.dir).resolve()
    conn = get_connection(messages_dir)
    cursor = conn.cursor()

    action = getattr(args, "user_action", "list")

    if action == "register":
        user_id = args.id.strip()
        desc = args.desc.strip()
        keywords = args.keywords.strip()
        conv_id = getattr(args, "conv_id", None)
        if conv_id:
            conv_id = conv_id.strip()
        wakeup_cmd = getattr(args, "wakeup_cmd", None)
        if wakeup_cmd:
            wakeup_cmd = wakeup_cmd.strip()

        cursor.execute("""
            INSERT INTO users (user_id, description, keywords, conversation_id, wakeup_cmd, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                description=excluded.description,
                keywords=excluded.keywords,
                conversation_id=COALESCE(excluded.conversation_id, users.conversation_id),
                wakeup_cmd=COALESCE(excluded.wakeup_cmd, users.wakeup_cmd),
                updated_at=CURRENT_TIMESTAMP
        """, (user_id, desc, keywords, conv_id, wakeup_cmd))
        conn.commit()
        print(f"User '{user_id}' registered/updated successfully.")

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
            WHERE user_id LIKE ? OR description LIKE ? OR keywords LIKE ?
            ORDER BY user_id ASC
        """, (query, query, query))
        rows = cursor.fetchall()
        if not rows:
            print(f"No users found matching '{args.query}'.")
        else:
            print(f"{'USER ID':<12} {'WAKEUP / CONV_ID':<25} {'KEYWORDS':<30} {'DESCRIPTION'}")
            print("-" * 95)
            for r in rows:
                wakeup_display = r['wakeup_cmd'] or (f"conv:{r['conversation_id']}" if r['conversation_id'] else "none")
                if len(wakeup_display) > 23:
                    wakeup_display = wakeup_display[:20] + "..."
                print(f"{r['user_id']:<12} {wakeup_display:<25} {r['keywords']:<30} {r['description']}")

    elif action == "list":
        cursor.execute("SELECT * FROM users ORDER BY user_id ASC")
        rows = cursor.fetchall()
        if not rows:
            print("No users registered in directory.")
        else:
            print(f"{'USER ID':<12} {'WAKEUP / CONV_ID':<25} {'KEYWORDS':<30} {'DESCRIPTION'}")
            print("-" * 95)
            for r in rows:
                wakeup_display = r['wakeup_cmd'] or (f"conv:{r['conversation_id']}" if r['conversation_id'] else "none")
                if len(wakeup_display) > 23:
                    wakeup_display = wakeup_display[:20] + "..."
                print(f"{r['user_id']:<12} {wakeup_display:<25} {r['keywords']:<30} {r['description']}")

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
    p_list.add_argument("--status", choices=["new", "read", "replied", "all"], default="all", help="Filter by status (default: all)")
    p_list.add_argument("--limit", type=int, default=20, help="Max records to return")

    # mark
    p_mark = subparsers.add_parser("mark", help="Update message status")
    p_mark.add_argument("target", help="Message ID or filename")
    p_mark.add_argument("--status", choices=["new", "read", "replied"], required=True, help="New status")

    # show
    p_show = subparsers.add_parser("show", help="Show message details and content")
    p_show.add_argument("target", help="Message ID or filename")

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

    # user
    p_user = subparsers.add_parser("user", help="Manage and discover agent users in the directory")
    p_user_sub = p_user.add_subparsers(dest="user_action", required=True)

    # user register
    p_u_reg = p_user_sub.add_parser("register", help="Register or update an agent user")
    p_u_reg.add_argument("--id", required=True, help="User/Agent ID (e.g. oschan, kuro)")
    p_u_reg.add_argument("--desc", required=True, help="Description of the user/agent")
    p_u_reg.add_argument("--keywords", required=True, help="Comma-separated keywords/capabilities")
    p_u_reg.add_argument("--conv-id", help="Conversation ID for agy headless resume")
    p_u_reg.add_argument("--wakeup-cmd", help="Custom wakeup shell command template (supports {sender}, {topic}, {prompt}, {conv_id})")

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
    elif args.command == "user":
        cmd_user(args)
    elif args.command == "users":
        cmd_users(args)


if __name__ == "__main__":
    main()
