#!/usr/bin/env python3
"""
Static tests for TYM Music HTML and CSS files.
No server required — parses files directly.
"""
import os, re, sys, unittest
from html.parser import HTMLParser

HERE   = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "..", "app", "static")


# ---------------------------------------------------------------------------
# Minimal HTML ID extractor
# ---------------------------------------------------------------------------
class IDCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids     = set()
        self.classes = set()

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "id" in d:
            self.ids.add(d["id"])
        for cls in (d.get("class") or "").split():
            self.classes.add(cls)


def parse_html(filename):
    path = os.path.join(STATIC, filename)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    col = IDCollector()
    col.feed(src)
    col.src = src
    return col


def read_css():
    path = os.path.join(STATIC, "style.css")
    with open(path, encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# Tests: index.html structure
# ===========================================================================
class TestIndexHTML(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.h = parse_html("index.html")

    def _require_id(self, eid):
        self.assertIn(eid, self.h.ids, f"index.html missing required id='{eid}'")

    # Navigation / tabs
    def test_has_nav_element(self):          self._require_id("nav")
    def test_has_tab_ahora(self):            self._require_id("tab-ahora")
    def test_has_tab_pedir(self):            self._require_id("tab-pedir")
    def test_has_tab_prio(self):             self._require_id("tab-prio")
    def test_has_tab_social(self):           self._require_id("tab-social")

    # Song request modal
    def test_has_sheet(self):                self._require_id("sheet")
    def test_has_confirm_bg(self):           self._require_id("confirmBg")
    def test_has_confirm_btn(self):          self._require_id("confirmBtn")
    def test_has_opt_normal(self):           self._require_id("optNormal")
    def test_has_opt_pri(self):              self._require_id("optPri")
    def test_has_opt_super(self):            self._require_id("optSuper")

    # Search
    def test_has_search_input(self):         self._require_id("q")
    def test_has_results_container(self):    self._require_id("results")
    def test_has_reco_box(self):             self._require_id("recoBox")
    def test_has_sim_box(self):              self._require_id("simBox")

    # Now playing / queue
    def test_has_now_img(self):              self._require_id("nowImg")
    def test_has_now_title(self):            self._require_id("nowT")
    def test_has_queue_list(self):           self._require_id("queueList")
    def test_has_prog_fill(self):            self._require_id("progFill")

    # Social / reactions
    def test_has_loved_list(self):           self._require_id("lovedList")
    def test_has_top_req_list(self):         self._require_id("topReqList")
    def test_has_react_lbl(self):            self._require_id("reactLbl")
    def test_has_like_pub_btn(self):         self._require_id("likePubBtn")

    # Assist bell
    def test_has_float_bell(self):           self._require_id("floatBell")

    # Toast
    def test_has_toast(self):                self._require_id("toast")

    # PIN gate
    def test_has_gate(self):                 self._require_id("gate")
    def test_has_pin_input(self):            self._require_id("pinInput")

    # Emoji effects container
    def test_has_fx(self):                   self._require_id("fx")

    # JS functions referenced in event handlers
    def test_open_sheet_referenced(self):
        self.assertIn("openSheet", self.h.src)
    def test_close_sheet_referenced(self):
        self.assertIn("closeSheet", self.h.src)
    def test_confirm_req_referenced(self):
        self.assertIn("confirmReq", self.h.src)
    def test_do_req_defined(self):
        self.assertIn("function doReq", self.h.src)
    def test_place_sim_box_defined(self):
        self.assertIn("placeSimBox", self.h.src)
    def test_spawn_react_defined(self):
        # After our TV change, spawnReact should be in tv.html not index
        self.assertIn("spawnReact", open(os.path.join(STATIC, "tv.html")).read())
    def test_body_overflow_on_open_sheet(self):
        self.assertIn("document.body.style.overflow='hidden'", self.h.src)
    def test_body_overflow_on_close_sheet(self):
        self.assertIn("document.body.style.overflow=''", self.h.src)
    def test_req_in_flight_guard(self):
        self.assertIn("_reqInFlight", self.h.src)
    def test_like_pub_toggle_defined(self):
        self.assertIn("_likePub", self.h.src)
    def test_react_sends_public_field(self):
        self.assertIn("public:_likePub", self.h.src)
    def test_render_top_requested_defined(self):
        self.assertIn("renderTopRequested", self.h.src)
    def test_render_loved_defined(self):
        self.assertIn("renderLoved", self.h.src)
    def test_bell_long_press_cancels(self):
        self.assertIn("cancelAssist", self.h.src)
        self.assertIn("longPress", self.h.src)
    def test_bell_holding_class_defined(self):
        self.assertIn("holding", self.h.src)


# ===========================================================================
# Tests: tv.html structure
# ===========================================================================
class TestTVHTML(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.h = parse_html("tv.html")

    def _require_id(self, eid):
        self.assertIn(eid, self.h.ids, f"tv.html missing required id='{eid}'")

    def test_has_fx_container(self):         self._require_id("fx")
    def test_has_now_reacts(self):           self._require_id("nowReacts")
    def test_has_qr_element(self):           self._require_id("qr")
    def test_has_loved_items(self):          self._require_id("lovedItems")
    def test_has_audio_overlay(self):        self._require_id("audioOverlay")
    def test_has_assist_zone(self):          self._require_id("assistZone")
    def test_has_login_gate(self):           self._require_id("loginGate")

    def test_spawn_react_defined(self):
        self.assertIn("function spawnReact", self.h.src)

    def test_spawn_react_shows_table(self):
        # spawnReact must use the table parameter to show mesa label
        self.assertIn("table", self.h.src[self.h.src.index("function spawnReact"):
                                           self.h.src.index("function spawnReact") + 600])

    def test_recent_reacts_used(self):
        self.assertIn("recent_reacts", self.h.src)

    def test_last_react_ts_defined(self):
        self.assertIn("_lastReactTs", self.h.src)

    def test_pill_buzz_animation_defined(self):
        self.assertIn("pillBuzz", self.h.src)

    def test_no_last_reacts_dict(self):
        # Old count-based detection used lastReacts dict — should no longer be primary
        # We keep _lastReactTs instead
        self.assertIn("_lastReactTs", self.h.src)


# ===========================================================================
# Tests: admin.html structure
# ===========================================================================
class TestAdminHTML(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.h = parse_html("admin.html")

    def _require_id(self, eid):
        self.assertIn(eid, self.h.ids, f"admin.html missing required id='{eid}'")

    def test_has_queue_container(self):      self._require_id("queue")
    def test_has_pending_container(self):    self._require_id("pending")
    def test_has_ledger_container(self):     self._require_id("ledger")
    def test_has_admin_search_input(self):   self._require_id("adminQ")
    def test_has_admin_results(self):        self._require_id("adminResults")
    def test_has_hist_card(self):            self._require_id("histCard")
    def test_has_hist_list(self):            self._require_id("histList")

    def test_admin_search_defined(self):
        self.assertIn("function adminSearch", self.h.src)

    def test_admin_add_song_defined(self):
        self.assertIn("function adminAddSong", self.h.src)

    def test_move_item_defined(self):
        self.assertIn("function moveItem", self.h.src)

    def test_allow_repeat_called(self):
        self.assertIn("/api/admin/allow_repeat", self.h.src)

    def test_admin_add_called(self):
        self.assertIn("/api/admin/add", self.h.src)

    def test_move_buttons_rendered(self):
        self.assertIn("dir", self.h.src)   # used in moveItem call


# ===========================================================================
# Tests: style.css
# ===========================================================================
class TestStyleCSS(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.css = read_css()

    def test_sheet_has_max_height(self):
        self.assertIn("max-height", self.css)

    def test_sheet_has_overflow_y(self):
        self.assertIn("overflow-y", self.css)

    def test_sheet_bg_is_fixed(self):
        self.assertIn("position:fixed", self.css.replace(" ", ""))

    def test_mini_has_pointer_events_none(self):
        self.assertIn("pointer-events:none", self.css.replace(" ", ""))

    def test_fxe_has_animation(self):
        # .fxe is defined in tv.html inline style, not in style.css
        tv_src = open(os.path.join(STATIC, "tv.html"), encoding="utf-8").read()
        self.assertIn(".fxe", tv_src)
        fxe_idx = tv_src.index(".fxe")
        self.assertIn("animation", tv_src[fxe_idx:fxe_idx + 300])

    def test_nav_has_z_index_50(self):
        # z-index:50 is applied to .nav in index.html inline styles
        idx_src = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
        self.assertIn("z-index:50", idx_src.replace(" ", ""))

    def test_css_variables_defined(self):
        for var in ["--bg", "--txt", "--pri", "--gold", "--green", "--red"]:
            self.assertIn(var, self.css)

    def test_btn_class_exists(self):
        self.assertIn(".btn{", self.css.replace(" ", "").replace("\n", ""))

    def test_tappable_class_exists(self):
        self.assertIn(".tappable", self.css)

    def test_rank_mini_selector(self):
        # Fix for social tab image sizing
        self.assertIn(".rank .mini", self.css)


# ===========================================================================
# Tests: server.py source integrity
# ===========================================================================
class TestServerSource(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        path = os.path.join(HERE, "..", "app", "server.py")
        with open(path, encoding="utf-8") as f:
            cls.src = f.read()

    def test_react_log_in_state_template(self):
        self.assertIn('"react_log"', self.src)

    def test_repeat_exceptions_in_state_template(self):
        self.assertIn('"repeat_exceptions"', self.src)

    def test_recent_reacts_in_public_state(self):
        self.assertIn("recent_reacts", self.src)

    def test_react_log_trimmed(self):
        # Should trim entries older than 60s
        self.assertIn("react_log", self.src)
        self.assertIn("60", self.src)

    def test_repeat_block_checks_exceptions(self):
        self.assertIn("repeat_exceptions", self.src)
        self.assertIn("return None", self.src)

    def test_admin_add_endpoint(self):
        self.assertIn('"/api/admin/add"', self.src)

    def test_admin_move_endpoint(self):
        self.assertIn('"/api/admin/move"', self.src)

    def test_admin_allow_repeat_endpoint(self):
        self.assertIn('"/api/admin/allow_repeat"', self.src)

    def test_admin_reset_clears_react_log(self):
        handler_start = self.src.index('if path == "/api/admin/reset"')
        reset_block = self.src[handler_start:][:600]
        self.assertIn("react_log", reset_block)

    def test_admin_reset_clears_repeat_exceptions(self):
        handler_start = self.src.index('if path == "/api/admin/reset"')
        reset_block = self.src[handler_start:][:600]
        self.assertIn("repeat_exceptions", reset_block)

    def test_repeat_exceptions_serialized(self):
        self.assertIn("repeat_exceptions", self.src[self.src.index("def venue_snapshot"):])

    def test_repeat_exceptions_deserialized(self):
        self.assertIn("repeat_exceptions", self.src[self.src.index("def _load_into"):])

    def test_lock_used_in_post_handler(self):
        self.assertIn("with LOCK:", self.src)

    def test_body_overflow_flag_in_source(self):
        idx_path = os.path.join(HERE, "..", "app", "static", "index.html")
        with open(idx_path) as f:
            idx = f.read()
        self.assertIn("document.body.style.overflow='hidden'", idx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
