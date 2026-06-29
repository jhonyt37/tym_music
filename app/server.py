#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TYM Music — MVP v2 (Python stdlib, sin dependencias).
Vistas: /  (cliente)  ·  /player (pantalla del local)  ·  /admin (dueno)
Novedades: sesion de mesa por PIN, paquetes (creditos + pase), progreso de
reproduccion, recomendadas (mas pedido / del local / populares / genero).
"""
import json, os, re, socket, threading, time, random
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("PORT", 8000))   # los hosts (Render/Railway/Fly) inyectan PORT
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # ej. https://tym.onrender.com (para el QR en deploy)
DEFAULT_DUR = 210  # 3:30 si no se conoce la duracion
EMOJIS = ["❤️", "🔥", "👍"]  # reacciones positivas

LOCK = threading.Lock()
_id = [0]
FB_IDX = [0]   # índice para recorrer la lista sugerida en orden (nunca silencio)
def nid():
    _id[0] += 1
    return _id[0]

def gen_pin():
    return f"{random.randint(0, 9999):04d}"

def gen_token():
    return f"{random.randint(0, 9999999999):010d}{random.randint(0,9999):04d}"

# ---- Catalogo demo (videos de YouTube conocidos y embebibles) ----
CATALOG = [
    {"title": "Despacito", "artist": "Luis Fonsi ft. Daddy Yankee", "yt": "kJQP7kiw5Fk", "genre": "reggaeton"},
    {"title": "Tusa", "artist": "Karol G, Nicki Minaj", "yt": "tbneQDc2H3I", "genre": "reggaeton"},
    {"title": "Con Calma", "artist": "Daddy Yankee, Snow", "yt": "DiItGE3eAyQ", "genre": "reggaeton"},
    {"title": "Hips Don't Lie", "artist": "Shakira ft. Wyclef Jean", "yt": "DUT5rEU6pqM", "genre": "pop latino"},
    {"title": "Waka Waka", "artist": "Shakira", "yt": "pRpeEdMmmQ0", "genre": "pop latino"},
    {"title": "Bailando", "artist": "Enrique Iglesias", "yt": "NUsoVlDFqZg", "genre": "pop latino"},
    {"title": "Sofia", "artist": "Alvaro Soler", "yt": "qaZ0oAh4evU", "genre": "pop latino"},
    {"title": "Shape of You", "artist": "Ed Sheeran", "yt": "JGwWNGJdvx8", "genre": "pop"},
    {"title": "Blinding Lights", "artist": "The Weeknd", "yt": "4NRXx6U8ABQ", "genre": "pop"},
    {"title": "Gangnam Style", "artist": "PSY", "yt": "9bZkp7q19f0", "genre": "pop"},
    {"title": "Bohemian Rhapsody", "artist": "Queen", "yt": "fJ9rUzIMcZQ", "genre": "rock"},
    {"title": "Smells Like Teen Spirit", "artist": "Nirvana", "yt": "hTWKbfoikeg", "genre": "rock"},
    {"title": "Sweet Child O' Mine", "artist": "Guns N' Roses", "yt": "1w7OgIMMRc4", "genre": "rock"},
]

def make_venue(name):
    """Crea el estado de UN bar (multi-tenant). Cada bar es un dict como este."""
    return {
        "settings": {
            "venue_name": name,
            "price_priority": 1000,
            "style": "Pon tu música 🎶",
            "auto_approve": True,
            "genre": "reggaeton",
            "credit_packages": [{"qty": 1, "price": 1000}, {"qty": 3, "price": 2500}, {"qty": 6, "price": 4500}],
            "time_pass": {"minutes": 30, "price": 5000},
            "repeat_block_min": 60,
            "repeat_block_songs": 3,
            "trim_end_secs": 0,        # corte ciego (0 = off; se usa el trim APRENDIDO por canción)
            "free_per_window": 3,
            "free_window_min": 10,
            "jump_multiplier": 3,
            "venue_logo": "",          # logo del BAR
        },
        "tables": [{"name": f"Mesa {i}", "pin": str(i) * 4} for i in range(1, 6)],  # PINs 1111..5555
        "sessions": {},
        "now_playing": None,
        "items": [],
        "ledger": [],
        "history": [],
        "curated": [dict(s) for s in CATALOG if s["genre"] in ("reggaeton", "pop latino")][:8],
        "req_counts": {},
        "reactions": {},
        "jump_used_for": None,
        "learned_end": {},     # yt -> seg donde termina de verdad (aprendido de los saltos del local)
        "_id": 0, "_fb": 0,
    }

# ---- Multi-tenant: varios bares ----
VENUES = {"bardemo": make_venue("Bar Demo TYM"), "lazona": make_venue("La Zona")}
DEFAULT_VID = "bardemo"
STATE = VENUES[DEFAULT_VID]   # "bar actual"; se reapunta por request bajo LOCK
AUTH = {}                     # cookie token -> venue_id (dueños logueados)
TOKENS = {}                   # token de cliente (mesa) -> venue_id

# ---- Datos GLOBALES de TYM Music ----
TYM = {
    "socials": {"instagram": "", "tiktok": "", "web": ""},
    "tym_logo": "",
    "subscribers": [],         # {email, table, venue, ts}
    "owners": {                # login de cada cliente TYM (dueño de bar). "*" = TYM master
        "bardemo": {"pass": "tym1234", "venue": "bardemo"},
        "lazona":  {"pass": "tym1234", "venue": "lazona"},
        "tym":     {"pass": "tymmaster", "venue": "*"},
    },
    "events": [],              # analítica: {venue, table, account, ts, ev, ...}
    "accounts": {},            # token -> {id,venue,table,opened_at,closed_at,total,orders_free,orders_premium}
}

CUR_VID = DEFAULT_VID   # bar del request actual (se fija bajo LOCK)

# ---- Analítica TYM (se alimenta de todo lo que pasa en cada bar) ----
def open_account(token, vid, table):
    TYM["accounts"][token] = {"id": token, "venue": vid, "table": table,
                              "opened_at": time.time(), "closed_at": None,
                              "total": 0, "orders_free": 0, "orders_premium": 0}

def log_order(table, token, mode, title, yt):
    premium = mode in ("single", "credito", "pase", "salto")
    TYM["events"].append({"venue": CUR_VID, "table": table, "account": token, "ts": time.time(),
                          "ev": "order", "mode": mode, "title": title, "yt": yt, "premium": premium})
    a = TYM["accounts"].get(token)
    if a:
        a["orders_premium" if premium else "orders_free"] += 1

def log_charge(table, token, amount, kind, title):
    STATE["ledger"].append({"table": table, "title": title, "amount": amount, "kind": kind, "ts": time.time()})
    TYM["events"].append({"venue": CUR_VID, "table": table, "account": token, "ts": time.time(),
                          "ev": "charge", "amount": amount, "kind": kind, "title": title})
    a = TYM["accounts"].get(token)
    if a:
        a["total"] += amount

def close_accounts(vid, table):
    total = 0
    for a in TYM["accounts"].values():
        if a["venue"] == vid and a["table"] == table and a["closed_at"] is None:
            a["closed_at"] = time.time(); total += a["total"]
    return total

def tym_analytics():
    """Resumen global para TYM: facturación por local, free vs premium, horas pico, cuentas."""
    fact_local, hour_rev, hour_ord = {}, {k: 0 for k in range(24)}, {k: 0 for k in range(24)}
    free = prem = total = 0
    for e in TYM["events"]:
        import datetime
        h = datetime.datetime.fromtimestamp(e["ts"]).hour
        if e["ev"] == "charge":
            fact_local[e["venue"]] = fact_local.get(e["venue"], 0) + e["amount"]
            hour_rev[h] += e["amount"]; total += e["amount"]
        elif e["ev"] == "order":
            hour_ord[h] += 1
            if e.get("premium"): prem += 1
            else: free += 1
    accts = sorted(TYM["accounts"].values(), key=lambda a: -a["opened_at"])[:200]
    return {"facturacion_por_local": fact_local, "facturacion_total": total,
            "orders_free": free, "orders_premium": prem,
            "hora_pico_ingresos": hour_rev, "hora_pico_pedidos": hour_ord,
            "cuentas": accts, "venues": {vid: VENUES[vid]["settings"]["venue_name"] for vid in VENUES}}

# =================== Utilidades ===================
def _parse_len(t):
    if not t:
        return 0
    try:
        s = 0
        for p in str(t).split(":"):
            s = s * 60 + int(p)
        return s
    except Exception:
        return 0

def yt_id(text):
    text = (text or "").strip()
    m = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    return None

def yt_title(vid):
    try:
        u = "https://www.youtube.com/oembed?format=json&url=https://youtu.be/" + vid
        d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=8).read())
        return d.get("title", "Canción"), d.get("author_name", "")
    except Exception:
        return "Canción", "YouTube"

# ---- Busqueda real en YouTube (sin API key) ----
_search_cache = {}
SEARCH_TTL = 300
def yt_search(q, limit=12):
    q = (q or "").strip()
    if not q:
        return []
    key = q.lower()
    now = time.time()
    hit = _search_cache.get(key)
    if hit and now - hit[0] < SEARCH_TTL:
        return hit[1]
    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote(q) +
           "&hl=es&gl=CO&sp=EgIQAQ%3D%3D")
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           "Accept-Language": "es-CO,es;q=0.9", "Cookie": "CONSENT=YES+1"}
    try:
        html = urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=12).read().decode("utf-8", "ignore")
    except Exception:
        return []
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", html) or \
        re.search(r'ytInitialData"?\]?\s*=\s*(\{.*?\});', html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    def walk(o):
        if isinstance(o, dict):
            if "videoRenderer" in o:
                v = o["videoRenderer"]
                try:
                    vid = v["videoId"]
                    title = "".join(r.get("text", "") for r in v["title"]["runs"])
                    length = v.get("lengthText", {}).get("simpleText", "")
                    ot = v.get("ownerText", {}).get("runs", [{}])
                    ch = ot[0].get("text", "") if ot else ""
                    secs = _parse_len(length)
                    if length and 0 < secs <= 720:
                        out.append({"yt": vid, "title": title, "artist": ch,
                                    "length": length, "duration": secs})
                except Exception:
                    pass
            for vv in o.values():
                walk(vv)
        elif isinstance(o, list):
            for vv in o:
                walk(vv)
    walk(data)
    seen, res = set(), []
    for it in out:
        if it["yt"] in seen:
            continue
        seen.add(it["yt"]); res.append(it)
        if len(res) >= limit:
            break
    _search_cache[key] = (now, res)
    return res

def get_session(token):
    return STATE["sessions"].get(token) if token else None

def find_table_by_pin(pin):
    pin = (pin or "").strip()
    for t in STATE["tables"]:
        if t["pin"] == pin:
            return t["name"]
    return None

def queue_view():
    appr = [i for i in STATE["items"] if i["status"] == "approved"]
    # orden: saltar-al-#1 primero, luego prioridad, luego normal; dentro de cada grupo por tiempo
    appr.sort(key=lambda i: (0 if i.get("super") else (1 if i.get("priority") else 2), i["ts"]))
    return appr

def pending_view():
    return [i for i in STATE["items"] if i["status"] == "pending"]

def promote_next():
    if STATE["now_playing"]:
        STATE["history"].insert(0, STATE["now_playing"])
        STATE["history"] = STATE["history"][:30]
    now = time.time()
    q = queue_view()
    if q:
        nxt = q[0]
        STATE["items"] = [i for i in STATE["items"] if i["id"] != nxt["id"]]
        if nxt.get("charge_on_play", 0) > 0 and not nxt.get("charged"):
            log_charge(nxt.get("charge_table", nxt["table"]), nxt.get("token"),
                       nxt["charge_on_play"], nxt.get("charge_kind", "prioridad"), nxt["title"])
            nxt["charged"] = True
        nxt["position"] = 0
        nxt["played_at"] = now
        STATE["now_playing"] = nxt
    else:
        # Nunca silencio: siguiente sugerida en orden, saltando las que sonaron hace poco
        pool = STATE["curated"] if STATE["curated"] else CATALOG
        s = None
        if pool:
            recent = {h["yt"] for h in STATE["history"][:STATE["settings"].get("repeat_block_songs", 3)]}
            for _ in range(len(pool)):
                cand = pool[FB_IDX[0] % len(pool)]; FB_IDX[0] += 1
                if cand["yt"] not in recent:
                    s = cand; break
            if s is None:
                s = pool[FB_IDX[0] % len(pool)]; FB_IDX[0] += 1
        STATE["now_playing"] = ({
            "id": nid(), "title": s["title"], "artist": s.get("artist", ""), "yt": s["yt"],
            "table": "Lista del local", "priority": False, "status": "playing",
            "ts": now, "charged": False, "fallback": True, "played_at": now,
            "duration": s.get("duration", DEFAULT_DUR), "position": 0} if s else None)
    return STATE["now_playing"]

def in_play_or_queue(yt):
    if STATE["now_playing"] and STATE["now_playing"].get("yt") == yt:
        return True
    return any(i["yt"] == yt for i in STATE["items"])

def repeat_block_reason(yt, now):
    s = STATE["settings"]
    n = max(0, int(s.get("repeat_block_songs", 3)))
    if n and any(h.get("yt") == yt for h in STATE["history"][:n]):
        return "songs"
    mins = int(s.get("repeat_block_min", 0))
    if mins > 0:
        cutoff = now - mins * 60
        for h in STATE["history"]:
            if h.get("yt") == yt and h.get("played_at", h.get("ts", 0)) >= cutoff:
                return "min"
    return None

def bump_count(yt, title, artist):
    c = STATE["req_counts"].get(yt)
    if c:
        c["count"] += 1
    else:
        STATE["req_counts"][yt] = {"yt": yt, "title": title, "artist": artist, "count": 1}

def react_counts(item_id, token=None):
    r = STATE["reactions"].get(item_id, {})
    counts = {e: len(r.get(e, ())) for e in EMOJIS}
    mine = [e for e in EMOJIS if token and token in r.get(e, ())]
    return counts, mine, sum(counts.values())

def public_item(it, token):
    counts, mine, total = react_counts(it["id"], token)
    return {"id": it["id"], "title": it["title"], "artist": it.get("artist", ""),
            "yt": it["yt"], "table": it.get("table", ""), "priority": it.get("priority", False),
            "super": it.get("super", False),
            "duration": it.get("duration", DEFAULT_DUR), "mine": bool(token) and it.get("token") == token,
            "reactions": counts, "my_reacts": mine, "react_total": total}

def public_state(token=None, admin=False):
    s = STATE["settings"]
    np = STATE["now_playing"]
    q = queue_view()
    rem = 0
    if np:
        rem = max(0, (np.get("duration") or DEFAULT_DUR) - (np.get("position") or 0))
    acc = rem
    my_pos = my_wait = None
    qout = []
    for i, it in enumerate(q):
        qout.append(public_item(it, token))
        if token and it.get("token") == token and my_pos is None:
            my_pos, my_wait = i + 1, int(acc)
        acc += it.get("duration") or DEFAULT_DUR
    sess = get_session(token)
    np_pub = None
    if np:
        ncounts, nmine, ntotal = react_counts(np["id"], token)
        np_pub = {"id": np["id"], "title": np["title"], "artist": np.get("artist", ""), "yt": np["yt"],
                  "table": np.get("table", ""), "priority": np.get("priority", False),
                  "fallback": np.get("fallback", False),
                  "duration": np.get("duration", DEFAULT_DUR), "position": np.get("position", 0),
                  "learned_end": STATE["learned_end"].get(np["yt"]),
                  "reactions": ncounts, "my_reacts": nmine, "react_total": ntotal}
    history = [public_item(h, token) for h in STATE["history"][:8]]
    # Ranking "lo más querido de la noche" (agrega reacciones por canción)
    agg = {}
    for it in (([np] if np else []) + STATE["items"] + STATE["history"]):
        _, _, tot = react_counts(it["id"])
        if tot > 0:
            a = agg.setdefault(it["yt"], {"yt": it["yt"], "title": it["title"],
                                          "artist": it.get("artist", ""), "total": 0})
            a["total"] += tot
    top_loved = sorted(agg.values(), key=lambda x: -x["total"])[:5]
    out = {
        "settings": dict({k: s[k] for k in ("venue_name", "price_priority", "style", "auto_approve",
                                       "genre", "credit_packages", "time_pass",
                                       "repeat_block_min", "repeat_block_songs", "trim_end_secs",
                                       "free_per_window", "free_window_min", "jump_multiplier",
                                       "venue_logo")},
                                       socials=TYM["socials"], tym_logo=TYM["tym_logo"]),
        "tables": [t["name"] for t in STATE["tables"]],
        "now_playing": np_pub,
        "jump_available": bool(np) and STATE.get("jump_used_for") != np["id"],
        "jump_price": s["price_priority"] * s.get("jump_multiplier", 3),
        "queue": qout,
        "queue_count": len(q),
        "queue_total_secs": int(acc),
        "now_remaining_secs": int(rem),
        "my_pos": my_pos,
        "my_wait_secs": my_wait,
        "history": history,
        "top_loved": top_loved,
        "session": (None if not sess else {"table": sess["table"], "credits": sess["credits"],
                                           "pass_until": sess["pass_until"]}),
    }
    if sess:
        tbl = sess["table"]
        mine = [l for l in STATE["ledger"] if l["table"] == tbl]
        out["my_tab"] = list(reversed(mine))[:20]
        out["my_tab_total"] = sum(l["amount"] for l in mine)
        # total de reacciones a las canciones que pidió esta sesión (para avisar al autor)
        likes = 0
        for it in (([np] if np else []) + STATE["items"] + STATE["history"]):
            if it.get("token") == token:
                likes += react_counts(it["id"])[2]
        out["my_likes_total"] = likes
    if admin:
        out["pending"] = [public_item(i, token) for i in pending_view()]
        out["ledger"] = list(reversed(STATE["ledger"]))[:40]
        out["ledger_total"] = sum(l["amount"] for l in STATE["ledger"])
        out["curated"] = STATE["curated"]
        out["subscribers"] = list(reversed(TYM["subscribers"]))[:100]
    return out

def _pad(lst, n=6, genre=None):
    """Rellena con el catálogo (preferentemente del género) hasta n, sin duplicar."""
    seen = {x["yt"] for x in lst}
    pool = [c for c in CATALOG if (not genre or c["genre"] == genre)] + CATALOG
    for c in pool:
        if len(lst) >= n:
            break
        if c["yt"] not in seen:
            seen.add(c["yt"])
            lst.append({"yt": c["yt"], "title": c["title"], "artist": c["artist"]})
    return lst

def recommendations():
    s = STATE["settings"]
    counts = sorted(STATE["req_counts"].values(), key=lambda x: -x["count"])[:10]
    mas = [{"yt": c["yt"], "title": c["title"], "artist": c["artist"]} for c in counts]
    if not mas:
        mas = [{"yt": x["yt"], "title": x["title"], "artist": x.get("artist", "")} for x in STATE["curated"][:8]]
    local = [{"yt": x["yt"], "title": x["title"], "artist": x.get("artist", "")} for x in STATE["curated"]]
    populares = [{"yt": r["yt"], "title": r["title"], "artist": r["artist"]} for r in yt_search("musica popular", 12)]
    genero = [{"yt": r["yt"], "title": r["title"], "artist": r["artist"]} for r in yt_search(f"los mejores {s['genre']}", 12)]
    return {"mas_pedido": _pad(mas), "del_local": local,
            "populares": _pad(populares), "genero": _pad(genero, genre=s["genre"])}

# =================== HTTP ===================
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        p = os.path.join(STATIC, name)
        if not os.path.exists(p):
            return self._send(404, {"error": "not found"})
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype)

    def _q(self, name, default=""):
        return parse_qs(urlparse(self.path).query).get(name, [default])[0]

    def get_cookie(self, name):
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def authed_venue(self):
        """venue_id del dueño logueado ('*' = TYM master), o None."""
        return AUTH.get(self.get_cookie("tymauth"))

    def set_venue(self, vid):
        global STATE, CUR_VID
        CUR_VID = vid
        STATE = VENUES[vid]

    def resolve_vid(self, body=None):
        av = self.authed_venue()
        if av and av in VENUES:
            return av
        tok = (body or {}).get("token") or self._q("token")
        if tok and tok in TOKENS:
            return TOKENS[tok]
        qv = (body or {}).get("v") or self._q("v")
        if qv in VENUES:
            return qv
        return DEFAULT_VID

    def do_GET(self):
        path = urlparse(self.path).path
        routes = {"/": "index.html", "/player": "player.html", "/admin": "admin.html",
                  "/tv": "tv.html", "/tym": "tym.html"}
        if path in routes:
            return self._file(routes[path], "text/html; charset=utf-8")
        if path == "/style.css":
            return self._file("style.css", "text/css; charset=utf-8")
        if path == "/qr.png":
            return self._file("qr.png", "image/png")
        if path == "/api/catalog":
            return self._send(200, CATALOG)
        if path == "/api/search":
            return self._send(200, yt_search(self._q("q")))
        if path == "/api/me":
            av = self.authed_venue()
            if not av:
                return self._send(401, {"error": "no auth"})
            name = "TYM Master" if av == "*" else VENUES[av]["settings"]["venue_name"]
            return self._send(200, {"venue": av, "venue_name": name})
        if path == "/api/tym/analytics":
            if self.authed_venue() != "*":
                return self._send(401, {"error": "Solo TYM"})
            with LOCK:
                return self._send(200, tym_analytics())
        if path == "/api/recommendations":
            with LOCK:
                self.set_venue(self.resolve_vid())
                return self._send(200, recommendations())
        if path == "/api/state":
            admin = self._q("admin") == "1"
            with LOCK:
                if admin:
                    av = self.authed_venue()
                    if not av or av not in VENUES:
                        return self._send(401, {"error": "no auth"})
                    self.set_venue(av)
                else:
                    self.set_venue(self.resolve_vid())
                return self._send(200, public_state(self._q("token") or None, admin))
        if path == "/api/admin/tables":
            av = self.authed_venue()
            if not av or av not in VENUES:
                return self._send(401, {"error": "no auth"})
            with LOCK:
                self.set_venue(av)
                return self._send(200, STATE["tables"])
        return self._send(404, {"error": "not found"})

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        path = urlparse(self.path).path
        d = self._body()
        # ---- Login / logout (dueños TYM) ----
        if path == "/api/login":
            u = (d.get("user") or "").strip()
            o = TYM["owners"].get(u)
            if not o or o["pass"] != (d.get("pass") or ""):
                return self._send(401, {"error": "Usuario o contraseña incorrectos"})
            tk = gen_token(); AUTH[tk] = o["venue"]
            body = json.dumps({"ok": True, "venue": o["venue"]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"tymauth={tk}; Path=/; SameSite=Lax; Max-Age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/api/logout":
            AUTH.pop(self.get_cookie("tymauth"), None)
            return self._send(200, {"ok": True})

        # ---- Endpoints que requieren dueño logueado ----
        ADMIN_PATHS = ("/api/advance", "/api/progress", "/api/admin/approve", "/api/admin/reject",
                       "/api/admin/remove", "/api/admin/settings", "/api/admin/tables",
                       "/api/admin/curated", "/api/admin/close_table", "/api/admin/reset")
        vid = self.resolve_vid(d)
        if path in ADMIN_PATHS:
            av = self.authed_venue()
            if not av or av not in VENUES:
                return self._send(401, {"error": "Inicia sesión"})
            vid = av
        with LOCK:
            self.set_venue(vid)
            # ---- Sesion de mesa ----
            if path == "/api/session":
                table = find_table_by_pin(d.get("pin"))
                if not table:
                    return self._send(400, {"error": "Código de mesa incorrecto"})
                token = gen_token()
                STATE["sessions"][token] = {"table": table, "credits": 0, "pass_until": 0, "created": time.time()}
                TOKENS[token] = CUR_VID
                open_account(token, CUR_VID, table)   # nueva cuenta de mesa (analítica)
                return self._send(200, {"ok": True, "token": token, "session": {
                    "table": table, "credits": 0, "pass_until": 0}})

            # ---- Comprar paquete / pase ----
            if path == "/api/buy":
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Sesión no válida. Ingresa el código de tu mesa."})
                s = STATE["settings"]
                if d.get("kind") == "credits":
                    try:
                        pkg = s["credit_packages"][int(d.get("index"))]
                    except Exception:
                        return self._send(400, {"error": "Paquete inválido"})
                    sess["credits"] += pkg["qty"]
                    log_charge(sess["table"], d.get("token"), pkg["price"], "paquete",
                               f"Paquete {pkg['qty']} prioridad(es)")
                elif d.get("kind") == "pass":
                    tp = s["time_pass"]
                    base = max(time.time(), sess["pass_until"])
                    sess["pass_until"] = base + tp["minutes"] * 60
                    log_charge(sess["table"], d.get("token"), tp["price"], "pase", f"Pase {tp['minutes']} min")
                else:
                    return self._send(400, {"error": "Tipo inválido"})
                return self._send(200, {"ok": True, "session": {
                    "table": sess["table"], "credits": sess["credits"], "pass_until": sess["pass_until"]}})

            # ---- Pedir cancion ----
            if path == "/api/request":
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa para pedir."})
                table = sess["table"]
                sup = bool(d.get("super"))
                priority = bool(d.get("priority")) or sup
                yt = title = artist = None
                dur = _parse_len(d.get("length")) or int(d.get("duration") or 0) or DEFAULT_DUR
                if d.get("yt"):
                    yt = d["yt"]; title = d.get("title", "Canción"); artist = d.get("artist", "")
                elif d.get("link"):
                    yt = yt_id(d["link"])
                    if yt:
                        title, artist = yt_title(yt)
                if not yt:
                    return self._send(400, {"error": "No pude leer el link de YouTube"})
                now = time.time()
                if in_play_or_queue(yt):
                    return self._send(400, {"error": "Esa canción ya está sonando o en la cola 🎶"})
                rb = repeat_block_reason(yt, now)
                if rb == "songs":
                    return self._send(400, {"error": "Esa canción acaba de sonar, ¡dale espacio a otra! 🙂"})
                if rb == "min":
                    return self._send(400, {"error": "Esa canción sonó hace poco, intenta más tarde."})
                if not priority:
                    fpw = int(STATE["settings"].get("free_per_window", 0))
                    fwin = int(STATE["settings"].get("free_window_min", 10)) * 60
                    if fpw > 0:
                        ft = [t for t in sess.get("free_ts", []) if t > now - fwin]
                        if len(ft) >= fpw:
                            return self._send(400, {"error": f"Máx {fpw} gratis cada {STATE['settings']['free_window_min']} min por mesa. ¡Usa prioridad ⚡!"})
                        ft.append(now); sess["free_ts"] = ft
                mode, charge, ckind = "normal", 0, "prioridad"
                if sup:
                    np = STATE["now_playing"]
                    if not np or STATE.get("jump_used_for") == np["id"]:
                        return self._send(400, {"error": "El salto al #1 ya se usó esta canción 🎵"})
                    mode = "salto"; ckind = "salto al #1"
                    charge = STATE["settings"]["price_priority"] * STATE["settings"].get("jump_multiplier", 3)
                elif priority:
                    if sess["pass_until"] > now:
                        mode = "pase"
                    elif sess["credits"] > 0:
                        sess["credits"] -= 1; mode = "credito"
                    else:
                        mode = "single"; charge = STATE["settings"]["price_priority"]
                item = {"id": nid(), "title": title, "artist": artist, "yt": yt,
                        "token": d.get("token"), "table": table, "priority": priority,
                        "super": sup, "mode": mode, "duration": dur,
                        "status": "approved" if STATE["settings"]["auto_approve"] else "pending",
                        "ts": time.time(), "charge_on_play": charge, "charged": False, "charge_kind": ckind}
                STATE["items"].append(item)
                bump_count(yt, title, artist)
                log_order(table, d.get("token"), mode, title, yt)   # analítica (free/premium)
                if sup and STATE["now_playing"]:
                    STATE["jump_used_for"] = STATE["now_playing"]["id"]
                if STATE["now_playing"] is None and item["status"] == "approved":
                    promote_next()
                return self._send(200, {"ok": True, "mode": mode,
                                        "session": {"table": table, "credits": sess["credits"],
                                                    "pass_until": sess["pass_until"]}})

            # ---- Reaccionar (positivo) ----
            if path == "/api/react":
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa."})
                emoji = d.get("emoji")
                try:
                    item_id = int(d.get("id"))
                except Exception:
                    item_id = None
                if emoji not in EMOJIS or item_id is None:
                    return self._send(400, {"error": "Reacción inválida"})
                r = STATE["reactions"].setdefault(item_id, {e: set() for e in EMOJIS})
                tok = d.get("token")
                if tok in r[emoji]:
                    r[emoji].discard(tok)
                else:
                    r[emoji].add(tok)
                counts, mine, total = react_counts(item_id, tok)
                return self._send(200, {"ok": True, "reactions": counts, "my_reacts": mine, "react_total": total})

            # ---- Impulsar: subir una canción de la cola a prioridad (pagada por quien impulsa) ----
            if path == "/api/boost":
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa."})
                target = None
                for i in STATE["items"]:
                    if i["id"] == d.get("id"):
                        target = i
                        break
                if not target:
                    return self._send(400, {"error": "Esa canción ya no está en la cola"})
                if target.get("priority"):
                    return self._send(200, {"ok": True, "already": True,
                                            "session": {"table": sess["table"], "credits": sess["credits"],
                                                        "pass_until": sess["pass_until"]}})
                now = time.time()
                target["priority"] = True
                target["charge_table"] = sess["table"]
                target["charge_kind"] = "impulso"
                if sess["pass_until"] > now:
                    target["charge_on_play"] = 0
                elif sess["credits"] > 0:
                    sess["credits"] -= 1
                    target["charge_on_play"] = 0
                else:
                    target["charge_on_play"] = STATE["settings"]["price_priority"]
                return self._send(200, {"ok": True, "session": {"table": sess["table"], "credits": sess["credits"],
                                                                "pass_until": sess["pass_until"]}})

            # ---- Saltar al #1 (premium, 1 por canción, primera mesa que lo hace) ----
            if path == "/api/jump":
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa."})
                np = STATE["now_playing"]
                if not np:
                    return self._send(400, {"error": "No hay nada sonando aún."})
                if STATE.get("jump_used_for") == np["id"]:
                    return self._send(400, {"error": "Ya alguien saltó una canción al frente. Disponible en la próxima 🎵"})
                target = None
                for i in STATE["items"]:
                    if i["id"] == d.get("id"):
                        target = i; break
                if not target:
                    return self._send(400, {"error": "Esa canción ya no está en la cola"})
                price = STATE["settings"]["price_priority"] * STATE["settings"].get("jump_multiplier", 3)
                target["super"] = True
                target["priority"] = True
                target["charge_table"] = sess["table"]
                target["charge_kind"] = "salto al #1"
                target["charge_on_play"] = price
                STATE["jump_used_for"] = np["id"]
                return self._send(200, {"ok": True, "price": price})

            # ---- Captura de email (crecimiento) ----
            if path == "/api/subscribe":
                email = (d.get("email") or "").strip()
                if "@" not in email or "." not in email or len(email) < 5:
                    return self._send(400, {"error": "Email inválido"})
                sess = get_session(d.get("token"))
                TYM["subscribers"].append({"email": email[:120],
                                           "table": sess["table"] if sess else "",
                                           "venue": CUR_VID, "ts": time.time()})
                return self._send(200, {"ok": True})

            # ---- Player reporta progreso ----
            if path == "/api/progress":
                if STATE["now_playing"]:
                    try:
                        STATE["now_playing"]["position"] = max(0, int(float(d.get("position", 0))))
                        if d.get("duration"):
                            STATE["now_playing"]["duration"] = max(1, int(float(d["duration"])))
                    except Exception:
                        pass
                return self._send(200, {"ok": True})

            if path == "/api/advance":
                # Trim aprendido: si el local salta cerca del final, aprende ese punto para esa canción
                if d.get("manual") and d.get("yt"):
                    try:
                        pos = float(d.get("position", 0)); dur = float(d.get("duration", 0))
                        if dur > 30 and dur * 0.5 <= pos < dur - 1:
                            STATE["learned_end"][d["yt"]] = int(pos)
                    except Exception:
                        pass
                return self._send(200, {"ok": True, "now_playing": promote_next()})

            # ---- Dueno ----
            if path == "/api/admin/approve":
                for i in STATE["items"]:
                    if i["id"] == d.get("id"):
                        i["status"] = "approved"
                if STATE["now_playing"] is None:
                    promote_next()
                return self._send(200, {"ok": True})

            if path in ("/api/admin/reject", "/api/admin/remove"):
                for i in STATE["items"]:
                    if i["id"] == d.get("id") and i.get("mode") == "credito":
                        sess = get_session(i.get("token"))
                        if sess:
                            sess["credits"] += 1  # reembolsa credito reservado
                STATE["items"] = [i for i in STATE["items"] if i["id"] != d.get("id")]
                return self._send(200, {"ok": True})

            if path == "/api/admin/settings":
                s = STATE["settings"]
                for k in ("venue_name", "style", "genre"):
                    if k in d:
                        s[k] = str(d[k])[:60]
                if "price_priority" in d:
                    try: s["price_priority"] = max(0, int(d["price_priority"]))
                    except Exception: pass
                if "auto_approve" in d:
                    s["auto_approve"] = bool(d["auto_approve"])
                for k in ("repeat_block_min", "repeat_block_songs", "trim_end_secs",
                          "free_per_window", "free_window_min", "jump_multiplier"):
                    if k in d:
                        try: s[k] = max(0, int(d[k]))
                        except Exception: pass
                if "venue_logo" in d:                       # logo del BAR
                    s["venue_logo"] = str(d["venue_logo"])[:700000]
                if "tym_logo" in d:                          # logo de TYM (global)
                    TYM["tym_logo"] = str(d["tym_logo"])[:700000]
                if "socials" in d and isinstance(d["socials"], dict):  # redes de TYM (global)
                    TYM["socials"] = {kk: str(vv)[:300] for kk, vv in d["socials"].items()}
                if "credit_packages" in d and isinstance(d["credit_packages"], list):
                    pk = []
                    for p in d["credit_packages"][:4]:
                        try:
                            pk.append({"qty": max(1, int(p["qty"])), "price": max(0, int(p["price"]))})
                        except Exception:
                            pass
                    if pk: s["credit_packages"] = pk
                if "time_pass" in d:
                    try:
                        s["time_pass"] = {"minutes": max(1, int(d["time_pass"]["minutes"])),
                                          "price": max(0, int(d["time_pass"]["price"]))}
                    except Exception:
                        pass
                return self._send(200, {"ok": True, "settings": s})

            if path == "/api/admin/tables":
                act = d.get("action")
                if act == "add":
                    n = len(STATE["tables"]) + 1
                    STATE["tables"].append({"name": d.get("name") or f"Mesa {n}", "pin": gen_pin()})
                elif act == "remove":
                    STATE["tables"] = [t for t in STATE["tables"] if t["name"] != d.get("name")]
                elif act == "regen":
                    for t in STATE["tables"]:
                        if t["name"] == d.get("name"):
                            t["pin"] = gen_pin()
                return self._send(200, {"ok": True, "tables": STATE["tables"]})

            if path == "/api/admin/curated":
                act = d.get("action")
                if act == "add" and d.get("yt"):
                    if not any(c["yt"] == d["yt"] for c in STATE["curated"]):
                        STATE["curated"].append({"yt": d["yt"], "title": d.get("title", "Canción"),
                                                 "artist": d.get("artist", ""),
                                                 "duration": _parse_len(d.get("length")) or DEFAULT_DUR})
                elif act == "remove":
                    STATE["curated"] = [c for c in STATE["curated"] if c["yt"] != d.get("yt")]
                return self._send(200, {"ok": True, "curated": STATE["curated"]})

            if path == "/api/admin/close_table":
                tbl = d.get("table")
                total = close_accounts(CUR_VID, tbl)   # marca cuentas cerradas (hora fin) + total
                STATE["ledger"] = [l for l in STATE["ledger"] if l["table"] != tbl]
                # libera tokens/sesiones de esa mesa: el próximo cliente entra con saldo en cero
                for t, se in list(STATE["sessions"].items()):
                    if se["table"] == tbl:
                        STATE["sessions"].pop(t, None); TOKENS.pop(t, None)
                return self._send(200, {"ok": True, "closed": tbl, "total": total})

            if path == "/api/admin/reset":
                STATE["now_playing"] = None
                STATE["items"] = []
                STATE["ledger"] = []
                STATE["history"] = []
                STATE["sessions"] = {}
                STATE["req_counts"] = {}
                STATE["reactions"] = {}
                STATE["jump_used_for"] = None
                _id[0] = 0
                FB_IDX[0] = 0
                return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})

# =================== Persistencia (archivo JSON; fácil de migrar a DB/multi-bar) ===================
DATA_FILE = os.path.join(HERE, "data.json")
PERSIST_KEYS = ("settings", "tables", "sessions", "now_playing", "items", "ledger",
                "history", "curated", "req_counts", "jump_used_for", "learned_end")

def venue_snapshot(v):
    snap = {k: v[k] for k in PERSIST_KEYS}
    snap["reactions"] = {str(k): {e: list(s) for e, s in r.items()}
                         for k, r in v["reactions"].items()}
    return snap

def save_state():
    """{version:3, tym:{...global...}, venues:{id:{...bar...}}} — multi-bar; listo para DB."""
    try:
        data = {"version": 3, "ids": {"_id": _id[0], "_fb": FB_IDX[0]},
                "tym": TYM, "venues": {vid: venue_snapshot(v) for vid, v in VENUES.items()}}
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print("save_state error:", e)

def _load_into(v, snap):
    for k in PERSIST_KEYS:
        if k not in snap:
            continue
        if k == "settings" and isinstance(snap[k], dict):
            v["settings"].update(snap[k])   # conserva defaults de claves nuevas
        else:
            v[k] = snap[k]
    v["reactions"] = {int(k): {e: set(lst) for e, lst in r.items()}
                      for k, r in snap.get("reactions", {}).items()}

def load_state():
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if "tym" in d:
            for k in ("socials", "tym_logo", "subscribers", "owners", "events", "accounts"):
                if k in d["tym"]:
                    TYM[k] = d["tym"][k]
        for vid, snap in d.get("venues", {}).items():
            tvid = "bardemo" if vid == "default" else vid          # migra venue v2 "default"
            if tvid not in VENUES:
                VENUES[tvid] = make_venue(snap.get("settings", {}).get("venue_name", tvid))
            _load_into(VENUES[tvid], snap)
            st = VENUES[tvid]["settings"]                          # migra v1/v2 (TYM en settings/venue)
            if "socials" in st: TYM["socials"] = st.pop("socials")
            if "tym_logo" in st: TYM["tym_logo"] = st.pop("tym_logo")
            if "tym" not in d and "subscribers" in snap: TYM["subscribers"] = snap["subscribers"]
        ids = d.get("ids", {})
        _id[0] = ids.get("_id", max([s.get("_id", 0) for s in d.get("venues", {}).values()] + [0]))
        FB_IDX[0] = ids.get("_fb", 0)
        TOKENS.clear()
        for vid, v in VENUES.items():
            for tok in v["sessions"]:
                TOKENS[tok] = vid
        print("Estado cargado de", DATA_FILE, "| bares:", list(VENUES.keys()))
    except Exception as e:
        print("load_state error:", e)

def autosave_loop():
    while True:
        time.sleep(3)
        with LOCK:
            save_state()

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def make_qr(url):
    try:
        import qrcode
        qrcode.make(url).save(os.path.join(STATIC, "qr.png"))
    except Exception as e:
        print("QR no generado:", e)

if __name__ == "__main__":
    load_state()
    threading.Thread(target=autosave_loop, daemon=True).start()
    ip = lan_ip()
    url = PUBLIC_URL.rstrip("/") + "/" if PUBLIC_URL else f"http://{ip}:{PORT}/"
    make_qr(url)
    base = url.rstrip("/")
    print("=" * 64)
    print("  TYM MUSIC — MVP multi-bar corriendo")
    print("=" * 64)
    print("  Bares:", {vid: VENUES[vid]["settings"]["venue_name"] for vid in VENUES})
    print(f"  Cliente (QR por bar):  {base}/?v=bardemo   (o ?v=lazona)")
    print(f"  Panel dueño / TV / Pantalla:  {base}/admin   {base}/tv   {base}/player")
    print(f"  Dashboard TYM (global):       {base}/tym")
    print("  LOGIN dueños:  bardemo/tym1234 · lazona/tym1234   |  TYM master:  tym/tymmaster")
    print("  PINs de mesa: 1111..5555")
    print("=" * 64)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
