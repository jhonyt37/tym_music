#!/usr/bin/env python3
"""
Integration tests for TYM Music API.
Spins up a real server subprocess in a temp dir and tests all endpoints.
"""
import json, os, re, subprocess, sys, tempfile, time, threading, unittest
import urllib.request, urllib.error, urllib.parse

HERE   = os.path.dirname(os.path.abspath(__file__))
APP    = os.path.join(HERE, "..", "app")
SERVER = os.path.join(APP, "server.py")
PORT   = 9871

# ---------------------------------------------------------------------------
# Server lifecycle helpers
# ---------------------------------------------------------------------------
_proc  = None
_tmpdir = None

def _start_server():
    global _proc, _tmpdir
    _tmpdir = tempfile.mkdtemp(prefix="tym_test_")
    import shutil
    shutil.copy(SERVER, _tmpdir)
    env = os.environ.copy()
    env["PORT"]  = str(PORT)
    env["UPSTASH_REDIS_REST_URL"]   = ""
    env["UPSTASH_REDIS_REST_TOKEN"] = ""
    env["TYM_OWNER_BARDEMO_PASS"] = "tym1234"  # contraseña conocida para tests
    _proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=_tmpdir, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    # Wait until the server responds
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/api/state?v=bardemo", timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Test server didn't start in time")

def _stop_server():
    global _proc, _tmpdir
    if _proc:
        _proc.terminate()
        _proc.wait()
    if _tmpdir:
        import shutil
        shutil.rmtree(_tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Base class with HTTP helpers
# ---------------------------------------------------------------------------
class TYMTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Admin login once per class (session persists in server)
        cls.admin_cookie = cls._admin_login()
        cls._reset()

    @classmethod
    def _reset(cls):
        cls._post("/api/admin/reset", {}, cls.admin_cookie)

    @classmethod
    def _admin_login(cls):
        body = json.dumps({"user": "bardemo", "pass": "tym1234"}).encode()
        req  = urllib.request.Request(
            f"http://localhost:{PORT}/api/login",
            body, {"Content-Type": "application/json"}, method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=5)
        cookies = resp.getheader("Set-Cookie") or ""
        m = re.search(r"tymauth=([^;]+)", cookies)
        return m.group(1) if m else None

    @classmethod
    def _post(cls, path, data, auth=None, venue="bardemo"):
        body = json.dumps({**data, "v": venue}).encode()
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Cookie"] = f"tymauth={auth}"
        req = urllib.request.Request(
            f"http://localhost:{PORT}{path}", body, headers, method="POST"
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return json.loads(resp.read()), resp.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read()), e.code

    @classmethod
    def _get(cls, path, auth=None):
        headers = {}
        if auth:
            headers["Cookie"] = f"tymauth={auth}"
        req = urllib.request.Request(f"http://localhost:{PORT}{path}", headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return json.loads(resp.read()), resp.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read()), e.code

    @classmethod
    def _state(cls, admin=False):
        suffix = "?v=bardemo" + ("&admin=1" if admin else "")
        d, _ = cls._get(f"/api/state{suffix}", auth=(cls.admin_cookie if admin else None))
        return d

    @classmethod
    def _make_session(cls, table="Mesa 1", pin="1111"):
        d, _ = cls._post("/api/session", {"pin": pin, "v": "bardemo"})
        return d.get("token")

    @classmethod
    def _request_song(cls, token, yt="dQw4w9WgXcQ", title="Test Song",
                      artist="Tester", priority=False, super_=False):
        return cls._post("/api/request", {
            "token": token, "yt": yt, "title": title,
            "artist": artist, "priority": priority, "super": super_,
            "length": "3:30"
        })


# ===========================================================================
# Tests: session management
# ===========================================================================
class TestSession(TYMTestCase):

    def test_valid_pin_creates_session(self):
        d, status = self._post("/api/session", {"pin": "1111", "v": "bardemo"})
        self.assertEqual(status, 200)
        self.assertIn("token", d)
        self.assertEqual(d["session"]["table"], "Mesa 1")

    def test_invalid_pin_rejected(self):
        d, status = self._post("/api/session", {"pin": "9999", "v": "bardemo"})
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    def test_second_table_pin(self):
        d, status = self._post("/api/session", {"pin": "2222", "v": "bardemo"})
        self.assertEqual(status, 200)
        self.assertEqual(d["session"]["table"], "Mesa 2")

    def test_state_returns_tables(self):
        s = self._state()
        self.assertIn("tables", s)
        self.assertIn("Mesa 1", s["tables"])


# ===========================================================================
# Tests: song requests & queue
# ===========================================================================
class TestQueue(TYMTestCase):

    def setUp(self):
        self._reset()
        self.token = self._make_session()

    def test_request_song_appears_in_queue_or_playing(self):
        # First request with empty queue auto-promotes to now_playing
        d, status = self._request_song(self.token)
        self.assertEqual(status, 200)
        s = self._state()
        in_queue = any(it["yt"] == "dQw4w9WgXcQ" for it in s["queue"])
        in_play  = s.get("now_playing") and s["now_playing"]["yt"] == "dQw4w9WgXcQ"
        self.assertTrue(in_queue or in_play, "Song must be in queue or now_playing")

    def test_duplicate_song_rejected(self):
        self._request_song(self.token)
        d, status = self._request_song(self.token, yt="dQw4w9WgXcQ")
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    def test_requires_token(self):
        d, status = self._post("/api/request", {
            "yt": "abc12345678", "title": "X", "artist": "Y", "length": "3:00"
        })
        self.assertEqual(status, 400)

    def test_queue_priority_order(self):
        # Fill now_playing first so subsequent requests stay in queue (not auto-promoted)
        self._request_song(self.token, yt="fillerSong00", title="Filler")
        # Admin-add two normal songs (admin add never triggers auto-promote)
        self._post("/api/admin/add",
            {"yt": "normalSong11", "title": "Normal 1", "length": "3:00"}, self.admin_cookie)
        self._post("/api/admin/add",
            {"yt": "normalSong22", "title": "Normal 2", "length": "3:00"}, self.admin_cookie)
        # Client priority request — now_playing is occupied so it stays in queue
        t2 = self._make_session("Mesa 2", "2222")
        self._request_song(t2, yt="priorSong333", title="Priority", priority=True)
        s = self._state()
        q = s["queue"]
        positions = {it["yt"]: i for i, it in enumerate(q)}
        self.assertIn("priorSong333", positions, "Priority song must be in queue")
        self.assertIn("normalSong11", positions, "Normal song must be in queue")
        self.assertLess(positions["priorSong333"], positions["normalSong11"],
                        "Priority song should come before normal song")

    def test_repeat_block_active(self):
        # Put a song in history via admin reset workaround: add then advance
        self._request_song(self.token, yt="histYT00000", title="Historic Song")
        self._post("/api/advance", {}, self.admin_cookie)   # promote to now_playing
        self._post("/api/advance", {}, self.admin_cookie)   # push to history
        d, status = self._request_song(self.token, yt="histYT00000")
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    def test_admin_reset_clears_queue(self):
        self._request_song(self.token)
        self._reset()
        s = self._state()
        self.assertEqual(s["queue"], [])
        self.assertIsNone(s["now_playing"])


# ===========================================================================
# Tests: admin endpoints
# ===========================================================================
class TestAdmin(TYMTestCase):

    def setUp(self):
        self._reset()
        self.token = self._make_session()

    def test_admin_endpoints_require_auth(self):
        """Admin endpoints must return 401 without auth cookie."""
        for path, body in [
            ("/api/admin/add",          {"yt": "x", "title": "X"}),
            ("/api/admin/move",         {"id": 1, "dir": "up"}),
            ("/api/admin/allow_repeat", {"yt": "x"}),
            ("/api/admin/remove",       {"id": 1}),
        ]:
            _, status = self._post(path, body, auth=None)
            self.assertEqual(status, 401, f"{path} should require admin auth")

    def test_admin_add_song_to_queue(self):
        d, status = self._post("/api/admin/add",
            {"yt": "adminYT00000", "title": "Admin Song", "artist": "Admin", "length": "3:00"},
            self.admin_cookie)
        self.assertEqual(status, 200)
        s = self._state(admin=True)
        yts = [it["yt"] for it in s["queue"]]
        self.assertIn("adminYT00000", yts)

    def test_admin_add_at_position_one(self):
        self._request_song(self.token, yt="firstSong000", title="First Normal")
        self._post("/api/admin/add",
            {"yt": "insertedFirst", "title": "Admin Inserted", "length": "3:00", "position": 1},
            self.admin_cookie)
        s = self._state()
        self.assertEqual(s["queue"][0]["yt"], "insertedFirst",
                         "Position 1 should be first in queue")

    def test_admin_remove_song(self):
        # Use admin add so song stays in queue (client request would auto-promote to now_playing)
        self._post("/api/admin/add",
            {"yt": "removeMe0000", "title": "To Remove", "length": "3:00"}, self.admin_cookie)
        s = self._state()
        item_id = next(it["id"] for it in s["queue"] if it["yt"] == "removeMe0000")
        self._post("/api/admin/remove", {"id": item_id}, self.admin_cookie)
        s2 = self._state()
        yts = [it["yt"] for it in s2["queue"]]
        self.assertNotIn("removeMe0000", yts)

    def test_admin_move_up(self):
        # Use admin add so both songs stay in queue
        self._post("/api/admin/add", {"yt": "song1aaaaaaa", "title": "Song 1", "length": "3:00"}, self.admin_cookie)
        self._post("/api/admin/add", {"yt": "song2bbbbbbb", "title": "Song 2", "length": "3:00"}, self.admin_cookie)
        s = self._state()
        id_song2 = next(it["id"] for it in s["queue"] if it["yt"] == "song2bbbbbbb")
        self._post("/api/admin/move", {"id": id_song2, "dir": "up"}, self.admin_cookie)
        s2 = self._state()
        self.assertEqual(s2["queue"][0]["yt"], "song2bbbbbbb",
                         "Song 2 should move to first position after moving up")

    def test_admin_move_down(self):
        # Use admin add so both songs stay in queue
        self._post("/api/admin/add", {"yt": "song1aaaaaaa", "title": "Song 1", "length": "3:00"}, self.admin_cookie)
        self._post("/api/admin/add", {"yt": "song2bbbbbbb", "title": "Song 2", "length": "3:00"}, self.admin_cookie)
        s = self._state()
        id_song1 = next(it["id"] for it in s["queue"] if it["yt"] == "song1aaaaaaa")
        self._post("/api/admin/move", {"id": id_song1, "dir": "down"}, self.admin_cookie)
        s2 = self._state()
        self.assertEqual(s2["queue"][1]["yt"], "song1aaaaaaa",
                         "Song 1 should be at position 2 after moving down")

    def test_admin_skip_advances_queue(self):
        self._request_song(self.token, yt="song1aaaaaaa", title="Song 1")
        self._post("/api/advance", {}, self.admin_cookie)
        s = self._state()
        # After advance, song should be now_playing or in history
        np = s.get("now_playing")
        hist = [h["yt"] for h in s.get("history", [])]
        self.assertTrue(
            (np and np["yt"] == "song1aaaaaaa") or "song1aaaaaaa" in hist,
            "Advanced song should be now_playing or in history"
        )


# ===========================================================================
# Tests: repeat exceptions
# ===========================================================================
class TestRepeatBlock(TYMTestCase):

    def setUp(self):
        self._reset()
        self.token = self._make_session()

    def _put_in_history(self, yt, title="Song"):
        # Request song (auto-promotes to now_playing when queue empty)
        self._request_song(self.token, yt=yt, title=title)
        # manual=True forces the song to history (avoids requeue logic)
        self._post("/api/advance", {"manual": True}, self.admin_cookie)

    def test_song_in_history_is_blocked(self):
        self._put_in_history("blockedYT000")
        d, status = self._request_song(self.token, yt="blockedYT000")
        self.assertEqual(status, 400)

    def test_allow_repeat_removes_block(self):
        self._put_in_history("repeatYT0000")
        # Block is active
        _, status_blocked = self._request_song(self.token, yt="repeatYT0000")
        self.assertEqual(status_blocked, 400)
        # Admin allows repeat
        d, _ = self._post("/api/admin/allow_repeat", {"yt": "repeatYT0000"}, self.admin_cookie)
        self.assertTrue(d.get("allowed"))
        # Now request should succeed
        d2, status2 = self._request_song(self.token, yt="repeatYT0000", title="Repeat Song")
        self.assertEqual(status2, 200, f"Song with repeat exception should be requestable: {d2}")

    def test_allow_repeat_toggle(self):
        self._post("/api/admin/allow_repeat", {"yt": "toggleYT000"}, self.admin_cookie)
        d, _ = self._post("/api/admin/allow_repeat", {"yt": "toggleYT000"}, self.admin_cookie)
        self.assertFalse(d.get("allowed"), "Second toggle should remove the exception")

    def test_repeat_exceptions_shown_in_admin_state(self):
        self._post("/api/admin/allow_repeat", {"yt": "excYT0000000"}, self.admin_cookie)
        s = self._state(admin=True)
        self.assertIn("excYT0000000", s.get("repeat_exceptions", []))


# ===========================================================================
# Tests: reactions
# ===========================================================================
class TestReactions(TYMTestCase):

    def setUp(self):
        self._reset()
        self.token = self._make_session()
        # Add a song and advance it to now_playing
        self._request_song(self.token, yt="reactSong000", title="React Song")
        self._post("/api/advance", {}, self.admin_cookie)

    def _react(self, emoji="❤️", public=True):
        s = self._state()
        np = s.get("now_playing")
        if not np:
            return None, 400
        return self._post("/api/react",
            {"token": self.token, "emoji": emoji, "id": np["id"], "public": public})

    def test_react_increments_count(self):
        self._react("❤️")
        s = self._state()
        np = s.get("now_playing")
        self.assertIsNotNone(np)
        self.assertGreater(np["reactions"].get("❤️", 0), 0)

    def test_react_twice_unreacts(self):
        self._react("❤️")
        self._react("❤️")   # second = undo
        s = self._state()
        np = s.get("now_playing")
        self.assertEqual(np["reactions"].get("❤️", 0), 0)

    def test_public_react_logs_table(self):
        self._react("❤️", public=True)
        s = self._state()
        np = s.get("now_playing")
        reacts = np.get("recent_reacts", [])
        public_with_table = [r for r in reacts if r.get("table") is not None]
        self.assertGreater(len(public_with_table), 0,
                           "Public reaction should have a table in recent_reacts")

    def test_private_react_logs_null_table(self):
        self._react("🔥", public=False)
        s = self._state()
        np = s.get("now_playing")
        reacts = np.get("recent_reacts", [])
        fire = [r for r in reacts if r["emoji"] == "🔥"]
        self.assertGreater(len(fire), 0)
        self.assertIsNone(fire[0]["table"],
                          "Private reaction should have null table in recent_reacts")

    def test_invalid_emoji_rejected(self):
        s = self._state()
        np = s.get("now_playing")
        d, status = self._post("/api/react",
            {"token": self.token, "emoji": "💀", "id": np["id"], "public": True})
        self.assertEqual(status, 400)

    def test_react_without_session_rejected(self):
        s = self._state()
        np = s.get("now_playing")
        d, status = self._post("/api/react",
            {"emoji": "❤️", "id": np["id"], "public": True})
        self.assertEqual(status, 400)


# ===========================================================================
# Tests: race condition
# ===========================================================================
class TestRaceCondition(TYMTestCase):

    def setUp(self):
        self._reset()

    def test_race_condition_detection(self):
        t1 = self._make_session("Mesa 1", "1111")
        t2 = self._make_session("Mesa 2", "2222")
        d1, s1 = self._request_song(t1, yt="raceTestYT00", title="Race Song")
        self.assertEqual(s1, 200, "First request should succeed")
        d2, s2 = self._request_song(t2, yt="raceTestYT00", title="Race Song")
        self.assertEqual(s2, 400, "Second request for same song should fail")
        # Should be either race or duplicate error (duplicate if >10s passed — unlikely)
        self.assertIn("error", d2)

    def test_same_song_different_sessions_race(self):
        results = []
        def do_req(pin, table):
            tok = self._make_session(table, pin)
            d, s = self._request_song(tok, yt="raceSong2222", title="Race2")
            results.append((s, d))
        threads = [
            threading.Thread(target=do_req, args=("1111", "Mesa 1")),
            threading.Thread(target=do_req, args=("2222", "Mesa 2")),
        ]
        for t in threads: t.start()
        for t in threads: t.join()
        successes = sum(1 for s, _ in results if s == 200)
        # At most one should succeed (the other gets duplicate/race)
        self.assertLessEqual(successes, 1, "Only one request for the same song should succeed")


# ===========================================================================
# Tests: assist (bell)
# ===========================================================================
class TestAssist(TYMTestCase):

    def setUp(self):
        self._reset()
        self.token = self._make_session()

    def test_assist_request_appears_in_state(self):
        self._post("/api/assist", {"token": self.token})
        s = self._state()
        assists = s.get("assists", [])
        self.assertGreater(len(assists), 0)
        self.assertEqual(assists[0]["table"], "Mesa 1")

    def test_assist_cancel_removes_it(self):
        self._post("/api/assist", {"token": self.token})
        s = self._state()
        self.assertGreater(len(s.get("assists", [])), 0, "Assist must exist before cancel")
        self._post("/api/assist", {"token": self.token, "cancel": True})
        s2 = self._state()
        active = [a for a in s2.get("assists", []) if not a.get("resolved")]
        self.assertEqual(len(active), 0)

    def test_buzz_requires_existing_assist(self):
        d, status = self._post("/api/assist", {"token": self.token, "buzz": True})
        self.assertEqual(status, 400, "Buzz without active assist must return 400")

    def test_buzz_cooldown_enforced(self):
        self._post("/api/assist", {"token": self.token})
        self._post("/api/assist", {"token": self.token, "buzz": True})
        d, status = self._post("/api/assist", {"token": self.token, "buzz": True})
        self.assertEqual(status, 400, "Second buzz within 30s should be rejected")
        self.assertIn("wait", d)

    def test_duplicate_assist_treated_as_buzz(self):
        # Second request from same token while assist is active = buzz (not new entry)
        self._post("/api/assist", {"token": self.token})
        s1 = self._state()
        self.assertEqual(len(s1.get("assists", [])), 1, "Only 1 assist after first request")
        # Second request (no buzz flag) — should NOT create a duplicate
        d, status = self._post("/api/assist", {"token": self.token})
        # Either cooldown (400 with wait) or buzz success (200) — but never a new entry
        s2 = self._state()
        self.assertEqual(len(s2.get("assists", [])), 1, "Still only 1 assist after second request")

    def test_buzz_count_increments(self):
        self._post("/api/assist", {"token": self.token})
        s0 = self._state()
        self.assertEqual(s0["assists"][0].get("buzz_count", 1), 1, "Initial buzz_count is 1")
        # Immediate buzz is blocked for 60s (dar tiempo al personal de ver la asistencia)
        d, status = self._post("/api/assist", {"token": self.token, "buzz": True})
        self.assertEqual(status, 400, "Buzz inmediato bloqueado durante el primer minuto")
        self.assertIn("wait", d, "Respuesta incluye tiempo de espera")
        self.assertGreater(d["wait"], 0, "Tiempo de espera es positivo")
        # buzz_count no sube porque el buzz fue bloqueado
        s1 = self._state()
        self.assertEqual(s1["assists"][0]["buzz_count"], 1, "buzz_count no aumenta si buzz fue bloqueado")

    def test_assist_has_buzz_count_field(self):
        self._post("/api/assist", {"token": self.token})
        s = self._state()
        self.assertIn("buzz_count", s["assists"][0], "assist must have buzz_count field")


# ===========================================================================
# Tests: state structure
# ===========================================================================
class TestStateStructure(TYMTestCase):

    def test_state_has_required_keys(self):
        s = self._state()
        required = ["settings", "tables", "now_playing", "queue", "history",
                    "top_loved", "top_requested", "assists"]
        for key in required:
            self.assertIn(key, s, f"State missing key: {key}")

    def test_admin_state_has_extra_keys(self):
        s = self._state(admin=True)
        for key in ["pending", "ledger", "history", "repeat_exceptions"]:
            self.assertIn(key, s, f"Admin state missing key: {key}")

    def test_now_playing_has_recent_reacts_when_playing(self):
        self._reset()
        tok = self._make_session()
        self._request_song(tok, yt="recentTest00", title="Recent Reacts Test")
        self._post("/api/advance", {}, self.admin_cookie)
        s = self._state()
        np = s.get("now_playing")
        self.assertIsNotNone(np)
        self.assertIn("recent_reacts", np, "now_playing should have recent_reacts field")
        self.assertIsInstance(np["recent_reacts"], list)

    def test_settings_has_required_keys(self):
        s = self._state()
        for key in ["venue_name", "price_priority", "auto_approve",
                    "repeat_block_songs", "repeat_block_min"]:
            self.assertIn(key, s["settings"], f"Settings missing key: {key}")


# ===========================================================================
# Module entry point
# ===========================================================================
if __name__ == "__main__":
    print(f"Starting test server on port {PORT}…")
    _start_server()
    try:
        unittest.main(argv=[sys.argv[0], "-v"], exit=False)
    finally:
        print("\nStopping test server…")
        _stop_server()
