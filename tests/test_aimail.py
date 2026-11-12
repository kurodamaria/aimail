#!/usr/bin/env python3
"""
aimail CLI test suite — stdlib only, zero dependencies.

Each test runs the real CLI (aimail.py) against an isolated temporary mailbox, so
nothing touches the user's live ~/Desktop/aimail data. Wakeups are disabled
(--no-wakeup) except where a wakeup prompt is explicitly under test.

Run:
    python3 -m unittest discover -s tests -v
    python3 tests/test_aimail.py
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AIMAIL = REPO / "aimail.py"


class AimailCLITestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="aimail-test-")
        self.dir = Path(self._tmp.name)
        self.register("alice", "opencode", "agent a", "python,testing")
        self.register("bob", "opencode", "agent b", "build,cpp")

    def tearDown(self):
        self._tmp.cleanup()

    # ── helpers ─────────────────────────────────────────────────────────────
    def cli(self, *args):
        cmd = [sys.executable, str(AIMAIL), "--dir", str(self.dir), *args]
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def register(self, uid, client, desc, keywords, *extra):
        rc, out, err = self.cli("user", "register", "--id", uid, "--client", client,
                                "--desc", desc, "--keywords", keywords, *extra)
        self.assertEqual(rc, 0, f"register {uid} failed: {err}")
        return out

    def send(self, sender, receiver, topic, body, *extra):
        rc, out, err = self.cli("send", "--from", sender, "--to", receiver,
                                "--topic", topic, "--no-wakeup", "-c", body, *extra)
        self.assertEqual(rc, 0, f"send failed: {err}")
        return out


class TestBasicMessaging(AimailCLITestCase):
    def test_send_registers_and_lists(self):
        self.send("alice", "bob", "task", "do the thing")
        rc, out, err = self.cli("list", "--to", "bob")
        self.assertEqual(rc, 0)
        self.assertIn("task", out)
        self.assertIn("alice", out)

    def test_show_by_id(self):
        self.send("alice", "bob", "task", "do the thing")
        rc, out, err = self.cli("show", "1")
        self.assertEqual(rc, 0)
        self.assertIn("topic".lower(), out.lower())
        self.assertIn("do the thing", out)

    def test_reply_marks_original_replied(self):
        self.send("alice", "bob", "task", "please do X")
        rc, out, err = self.cli("reply", "task", "--from", "bob", "--no-wakeup", "-c", "done")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("show", "1")
        self.assertIn("replied", out)


class TestTopicCollision(AimailCLITestCase):
    def test_reuse_topic_warns(self):
        self.send("alice", "bob", "hello", "one")
        rc, out, err = self.cli("send", "--from", "alice", "--to", "bob",
                                "--topic", "hello", "--no-wakeup", "-c", "two")
        self.assertIn("already used by", out)

    def test_force_silences_warning(self):
        self.send("alice", "bob", "hello", "one")
        rc, out, err = self.cli("send", "--from", "alice", "--to", "bob",
                                "--topic", "hello", "--no-wakeup", "--force", "-c", "two")
        self.assertNotIn("already used by", out)

    def test_reply_does_not_warn(self):
        self.send("alice", "bob", "chat", "one")
        rc, out, err = self.cli("reply", "chat", "--from", "bob", "--no-wakeup", "-c", "two")
        self.assertNotIn("already used by", out + err)

    def test_ambiguous_topic_refuses_to_guess(self):
        self.send("alice", "bob", "hello", "one")
        self.send("alice", "bob", "hello", "two", "--force")
        rc, out, err = self.cli("show", "hello")
        self.assertEqual(rc, 1)
        self.assertIn("refusing to guess", err)

    def test_exact_topic_preferred_over_reply_child(self):
        self.send("alice", "bob", "verify", "please verify")
        self.cli("reply", "verify", "--from", "bob", "--no-wakeup", "-c", "ok")
        # 'verify' must resolve to the root message, not 'verify_reply'
        rc, out, err = self.cli("show", "verify")
        self.assertEqual(rc, 0, err)
        self.assertIn("THREAD LINEAGE", out)


class TestInformationalNudge(AimailCLITestCase):
    def test_nudge_on_show_for_hello(self):
        self.send("alice", "bob", "hello", "Hi bob, this is alice, registered and ready.")
        rc, out, err = self.cli("show", "hello")
        self.assertIn("looks informational", out)
        self.assertIn("aimail close", out)

    def test_no_nudge_for_explicit_request(self):
        self.send("alice", "bob", "hello", "Hello, please deploy the build.")
        rc, out, err = self.cli("show", "hello")
        self.assertNotIn("looks informational", out)

    def test_nudge_on_reply(self):
        self.send("alice", "bob", "hello", "Hi bob, just saying hi.")
        rc, out, err = self.cli("reply", "hello", "--from", "bob", "--no-wakeup", "-c", "ack")
        self.assertIn("looks informational", out)

    def test_list_note_on_stderr_not_stdout(self):
        self.send("alice", "bob", "status", "Build is green, all tests pass.")
        rc, out, err = self.cli("list", "--to", "bob")
        self.assertNotIn("informational", out)   # stdout stays parseable
        self.assertIn("informational", err)      # advisory on stderr


class TestCapabilityRouting(AimailCLITestCase):
    def setUp(self):
        super().setUp()
        self.register("chie", "opencode", "project brain, research and planning",
                      "tfimbounce,research-design,concepts,planning,delegation")
        self.register("vera", "opencode",
                      "project verifier: independently audits and reproduces code (ED-gate)",
                      "tfimbounce,verification,code-review,testing,reproducibility")
        self.register("courier", "opencode", "repo manager, maintenance and refactors",
                      "aimail,repo-management,maintenance,git,release,quality")

    def test_route_recommends_verifier(self):
        rc, out, err = self.cli("user", "route", "independently verify an ED gate")
        self.assertEqual(rc, 0, err)
        self.assertIn("Recommended: vera", out)

    def test_route_shortcut_and_best(self):
        rc, out, err = self.cli("route", "--best", "maintain the repo and do a refactor")
        self.assertIn("courier", out)
        self.assertNotIn("Recommended:", out)


class TestInboxDigest(AimailCLITestCase):
    def test_digest_lists_pending(self):
        self.send("alice", "bob", "one", "first")
        self.send("alice", "bob", "two", "second")
        rc, out, err = self.cli("inbox", "bob")
        self.assertEqual(rc, 0)
        self.assertIn("pending", out)
        self.assertIn("one", out)
        self.assertIn("two", out)

    def test_digest_alias_and_empty(self):
        rc, out, err = self.cli("digest", "bob")
        self.assertEqual(rc, 0)
        self.assertIn("no pending messages", out)


class TestWakeupPrompt(AimailCLITestCase):
    def test_prompt_references_message_id_and_is_metachar_free(self):
        capture = self.dir / "prompt.txt"
        self.register("sink", "custom", "sink", "sink",
                      "--wakeup-cmd", f"echo {{prompt}} > {capture}")
        self.register("src", "opencode", "src", "src")
        # Note: no --no-wakeup here — this test asserts the triggered prompt.
        rc, out, err = self.cli("send", "--from", "src", "--to", "sink",
                                "--topic", "kickoff", "-c", "hello there")
        self.assertEqual(rc, 0, err)

        # wakeup is spawned in the background; poll briefly for the file
        for _ in range(40):
            if capture.exists() and capture.stat().st_size > 0:
                break
            time.sleep(0.05)
        self.assertTrue(capture.exists(), "wakeup prompt was not captured")
        prompt = capture.read_text()

        self.assertIn("aimail show 1", prompt)
        self.assertIn("message 1", prompt)
        # kept free of characters that break raw shell interpolation
        for ch in ("'", "#", "(", ")"):
            self.assertNotIn(ch, prompt)


class TestPureHelpers(unittest.TestCase):
    """Unit tests for the informational heuristic (imported directly, no CLI)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("aimail", AIMAIL)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def test_topic_root(self):
        self.assertEqual(self.m.topic_root("hello"), "hello")
        self.assertEqual(self.m.topic_root("hello_reply"), "hello")
        self.assertEqual(self.m.topic_root("hello_reply_replied"), "hello")

    def test_informational_cases(self):
        f = self.m.is_informational
        # courtesy/status notes -> informational
        self.assertTrue(f("hello", "Hi, this is vera, the verifier, standing by for code to verify."))
        self.assertTrue(f("status", "Build is green, all tests pass."))
        self.assertTrue(f("thanks", "Thanks, no action needed."))
        # explicit asks -> not informational
        self.assertFalse(f("hello", "Please verify the gate."))
        self.assertFalse(f("hello", "Can you take a look?"))
        # guards
        self.assertFalse(f("task", "hello"))                 # non-informational topic
        self.assertFalse(f("hello", "hi", True))             # has attachment
        self.assertFalse(f("hello", "x" * 400))              # too long


if __name__ == "__main__":
    unittest.main(verbosity=2)
