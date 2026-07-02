#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TYM Music — MVP v2 (Python stdlib, sin dependencias).
Vistas: /  (cliente)  ·  /player (pantalla del local)  ·  /admin (dueno)
Novedades: sesion de mesa por PIN, paquetes (creditos + pase), progreso de
reproduccion, recomendadas (mas pedido / del local / populares / genero).
"""
import json, os, re, socket, threading, time, random, datetime, struct, zlib
import hashlib, hmac, secrets
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
VERSION = "0.0.5-demo"
PORT = int(os.environ.get("PORT", 8000))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
# Upstash Redis (backup remoto: evita perder datos en Render/hosts con disco efímero)
# Crea una DB gratuita en upstash.com → copia REST URL y token → ponlos como variables de entorno
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_KEY   = "tym_state"
DEFAULT_DUR = 210  # 3:30 si no se conoce la duracion
EMOJIS = ["❤️", "🔥", "👍"]  # reacciones positivas
VIBES = ["🔥 Que todo el mundo cante", "💃 Más baile", "🎸 Más suave", "✨ Así está perfecto"]
BIS_THRESHOLD = 3

BOGOTA_TZ = datetime.timezone(datetime.timedelta(hours=-5))
def _bogota_hour(ts):
    return datetime.datetime.fromtimestamp(ts, tz=BOGOTA_TZ).hour

LOCK = threading.Lock()
_id = [0]
FB_IDX = [0]   # índice para recorrer la lista sugerida en orden (nunca silencio)
def nid():
    _id[0] += 1
    return _id[0]

def gen_pin():
    return f"{secrets.randbelow(10000):04d}"

def gen_token():
    return secrets.token_hex(16)

# ---- Seguridad: contraseñas ----
def _hash_pass(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def _ensure_owner_passwords():
    """Primer arranque sin data.json: asigna contraseñas iniciales.
    Si existe TYM_OWNER_<USER>_PASS se usa ese valor (útil en CI/tests).
    En producción con data.json ya guardado, esta función no toca nada."""
    changed = False
    for uname, odata in TYM["owners"].items():
        if not odata.get("pass_hash"):
            env_key = f"TYM_OWNER_{uname.upper()}_PASS"
            p = os.environ.get(env_key, "")
            if p:
                pwd = p
            else:
                pwd = secrets.token_urlsafe(12)
                print(f"🔑 Contraseña inicial para '{uname}': {pwd}  ← guárdala y cámbiala desde el panel admin")
            odata["pass_hash"] = _hash_pass(pwd)
            changed = True
    if changed:
        save_state()

# ---- Rate limiting (por IP) ----
_RATE = {}
_RATE_LOCK = threading.Lock()
# group -> (max_requests, window_seconds)
_RATE_LIMITS = {
    "login":   (5,  60),
    "session": (10, 60),
    "request": (6,  60),
    "social":  (30, 60),
    "search":  (20, 60),
}

_LOCALHOST = {"127.0.0.1", "::1", "localhost"}

def _rate_ok(ip, group):
    if ip in _LOCALHOST:
        return True  # localhost never rate-limited (dev + tests)
    limit, window = _RATE_LIMITS[group]
    now = time.time()
    with _RATE_LOCK:
        bucket = _RATE.setdefault(ip, {}).setdefault(group, [])
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        if len(_RATE) > 2000:
            stale = [k for k, v in _RATE.items()
                     if all(now - t > window for ts in v.values() for t in ts)]
            for k in stale[:200]:
                del _RATE[k]
        return True

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
            "max_priority_queue_min": 0,   # bloquear nuevas prioridades si cola premium > N min (0=off)
            "fallback_shuffle": True,       # lista del local en orden aleatorio
            "theme": "azul",               # tema de color: azul | purpura | verde
            "blocked_keywords": [],        # palabras en título/artista que bloquean el pedido
            "allowed_keywords": [],        # si hay entradas, la canción DEBE tener al menos una
            "allow_skip_vote": False,      # permite que las mesas voten para saltar la canción
        },
        "tables": [{"name": f"Mesa {i}", "pin": str(i) * 4} for i in range(1, 6)],  # PINs 1111..5555
        "sessions": {},
        "now_playing": None,
        "items": [],
        "ledger": [],
        "history": [],
        "request_log": [],         # todos los pedidos, últimos 3 días
        "curated": [dict(s) for s in CATALOG if s["genre"] in ("reggaeton", "pop latino")][:8],
        "curated_shuffle": [],     # orden aleatorio actual del fallback
        "req_counts": {},
        "reactions": {},
        "reaction_pub": {},    # {item_id: {emoji: set(tokens)}} — reacciones marcadas como públicas
        "react_log": [],       # [{emoji, table, ts, item_id}] — log de últimas reacciones para TV
        "repeat_exceptions": set(),  # yt IDs a los que el admin permite repetir aunque estén en historial
        "jump_used_for": None,
        "learned_end": {},         # yt -> seg de corte aprendido por saltos manuales
        "assists": [],             # {id, table, ts, resolved, resolve_ts, token}
        "tv_lastseen": 0,          # timestamp del último ping de la TV activa
        "dedicas": [],             # [{id, from_table, to_table, message, ts, shown_tv}]
        "bis_votes": {},           # {yt: set(tokens)} — votos de bis por canción
        "poll": None,              # {options:[{yt,title,artist}], votes:{yt:set(tokens)}, active, created_at}
        "vibe_votes": {},          # {emoji: set(tokens)} — votos de vibe
        "skip_votes": set(),       # set de tokens que votaron para saltar la canción actual
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
        "bardemo": {"pass_hash": None, "venue": "bardemo"},
        "lazona":  {"pass_hash": None, "venue": "lazona"},
        "tym":     {"pass_hash": None, "venue": "*"},
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
    # Cualquier cobro fuerza un backup a Redis en el próximo autosave (~3s): nunca se pierde dinero
    _redis_last_save[0] = 0

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
        h = _bogota_hour(e["ts"])
        if e["ev"] == "charge":
            fact_local[e["venue"]] = fact_local.get(e["venue"], 0) + e["amount"]
            hour_rev[h] += e["amount"]; total += e["amount"]
        elif e["ev"] == "order":
            hour_ord[h] += 1
            if e.get("premium"): prem += 1
            else: free += 1
    accts = sorted(TYM["accounts"].values(), key=lambda a: -a["opened_at"])[:200]
    venues_info = {vid: {"name": VENUES[vid]["settings"]["venue_name"],
                         "tables": len(VENUES[vid]["tables"])} for vid in VENUES}
    owners_info = [{"username": k, "venue": v["venue"], "venue_name": VENUES[v["venue"]]["settings"]["venue_name"]}
                   for k, v in TYM["owners"].items() if v.get("venue") != "*" and v.get("venue") in VENUES]
    return {"facturacion_por_local": fact_local, "facturacion_total": total,
            "orders_free": free, "orders_premium": prem,
            "hora_pico_ingresos": hour_rev, "hora_pico_pedidos": hour_ord,
            "cuentas": accts, "venues": venues_info, "owners": owners_info}

def venue_analytics(vid):
    """Informe del local para el dueño: facturación, pedidos, canciones top, horas pico."""
    hour_rev = {k: 0 for k in range(24)}
    hour_ord = {k: 0 for k in range(24)}
    songs, total, week_total, free, prem = {}, 0, 0, 0, 0
    week_ago = time.time() - 7 * 86400
    for e in TYM["events"]:
        if e.get("venue") != vid:
            continue
        h = _bogota_hour(e["ts"])
        if e["ev"] == "charge":
            hour_rev[h] += e.get("amount", 0)
            total += e.get("amount", 0)
            if e["ts"] >= week_ago:
                week_total += e.get("amount", 0)
        elif e["ev"] == "order":
            hour_ord[h] += 1
            if e.get("premium"): prem += 1
            else: free += 1
            yt = e.get("yt", ""); title = e.get("title", "?")
            if yt:
                if yt not in songs: songs[yt] = {"title": title, "yt": yt, "count": 0}
                songs[yt]["count"] += 1
    top_songs = sorted(songs.values(), key=lambda x: -x["count"])[:10]
    recent_accts = sorted([a for a in TYM["accounts"].values() if a.get("venue") == vid],
                          key=lambda a: -(a.get("opened_at") or 0))[:20]
    return {"facturacion_total": total, "facturacion_semana": week_total,
            "orders_free": free, "orders_premium": prem,
            "hora_pico_ingresos": hour_rev, "hora_pico_pedidos": hour_ord,
            "canciones_top": top_songs, "cuentas_recientes": recent_accts}

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

# ---- PWA: generador de ícono PNG sin dependencias externas ----
def _make_icon_png(size):
    """Genera un PNG sólido color TYM (#0e1320) con franja dorada central."""
    W = size
    bg = (14, 19, 32)    # #0e1320
    gold = (245, 179, 1) # #f5b301
    # franja dorada: ocupa el 60% central del ícono
    stripe_top = int(W * 0.20)
    stripe_bot = int(W * 0.80)
    stripe_l   = int(W * 0.15)
    stripe_r   = int(W * 0.85)
    rows = []
    for y in range(W):
        row = bytearray()
        for x in range(W):
            if stripe_top <= y < stripe_bot and stripe_l <= x < stripe_r:
                row += bytes(gold)
            else:
                row += bytes(bg)
        rows.append(bytes(row))
    raw = b''.join(b'\x00' + r for r in rows)
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', W, W, 8, 2, 0, 0, 0))
    idat = chunk(b'IDAT', zlib.compress(raw, 6))
    iend = chunk(b'IEND', b'')
    return b'\x89PNG\r\n\x1a\n' + ihdr + idat + iend

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

def promote_next(manual=False):
    old = STATE["now_playing"]
    if old:
        if old.get("fallback"):
            # Canción del local: siempre a historial sin re-encolar
            STATE["history"].insert(0, old); STATE["history"] = STATE["history"][:30]
        elif manual or old.get("played_enough") or old.get("requeue_count", 0) >= 2:
            # Sonó suficiente, admin la saltó, o ya re-encolamos 2 veces: finalizar
            old["play_status"] = "skipped" if (manual and not old.get("played_enough")) else "played"
            STATE["history"].insert(0, old); STATE["history"] = STATE["history"][:30]
        else:
            # No sonó suficiente (< 50%) y no fue salto manual: re-encolar
            old["requeue_count"] = old.get("requeue_count", 0) + 1
            old["play_status"] = "requeued"
            old["position"] = 0; old["played_at"] = None; old["played_enough"] = False
            STATE["items"].append(old)   # vuelve a la cola manteniendo su prioridad original
    now = time.time()
    q = queue_view()
    if q:
        nxt = q[0]
        STATE["items"] = [i for i in STATE["items"] if i["id"] != nxt["id"]]
        if nxt.get("charge_on_play", 0) > 0 and not nxt.get("charged"):
            tok = nxt.get("token")
            # Solo cobrar si el token sigue con sesión activa (cuenta no cerrada antes de sonar)
            if tok and tok in STATE.get("sessions", {}):
                log_charge(nxt.get("charge_table", nxt["table"]), tok,
                           nxt["charge_on_play"], nxt.get("charge_kind", "prioridad"), nxt["title"])
            nxt["charged"] = True  # marcar siempre para no reintentar
        nxt["position"] = 0
        nxt["played_at"] = now
        STATE["skip_votes"] = set()  # reiniciar votos de skip al cambiar de canción
        nxt["played_enough"] = False
        nxt["play_status"] = "playing"
        STATE["now_playing"] = nxt
    else:
        # Nunca silencio: fallback de la lista del local (shuffle o secuencial)
        base_pool = STATE["curated"] if STATE["curated"] else CATALOG
        shuffle = STATE["settings"].get("fallback_shuffle", True)
        if shuffle:
            # Usa una copia barajada; cuando se agota, baraja de nuevo
            if not STATE.get("curated_shuffle"):
                import random
                STATE["curated_shuffle"] = list(base_pool)
                random.shuffle(STATE["curated_shuffle"])
            pool = STATE["curated_shuffle"]
        else:
            pool = base_pool
        s = None
        if pool:
            recent = {h["yt"] for h in STATE["history"][:STATE["settings"].get("repeat_block_songs", 3)]}
            tries = 0
            while tries < len(pool):
                if shuffle:
                    cand = pool[0]; pool.pop(0)
                    if not pool:   # se agotó la lista, mezcla de nuevo para la próxima vuelta
                        import random
                        STATE["curated_shuffle"] = list(base_pool)
                        random.shuffle(STATE["curated_shuffle"])
                else:
                    cand = pool[FB_IDX[0] % len(pool)]; FB_IDX[0] += 1
                if cand["yt"] not in recent:
                    s = cand; break
                tries += 1
            if s is None:
                s = base_pool[0] if base_pool else None
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
    if yt in STATE.get("repeat_exceptions", set()):
        return None
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
            "play_status": it.get("play_status", "pending"),
            "requeue_count": it.get("requeue_count", 0),
            "reactions": counts, "my_reacts": mine, "react_total": total}

def public_state(token=None, admin=False, mark_dedica=None):
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
                  "message": (np.get("message") or ""),
                  "reactions": ncounts, "my_reacts": nmine, "react_total": ntotal}
        now_ts = time.time()
        np_pub["recent_reacts"] = [
            {"emoji": e["emoji"], "table": e.get("table"), "ts": e["ts"]}
            for e in STATE.get("react_log", [])
            if now_ts - e["ts"] < 30 and e.get("item_id") == np["id"]
        ]
    history = [public_item(h, token) for h in STATE["history"][:8]]
    # Ranking "lo más querido de la noche" (agrega reacciones por canción)
    agg = {}
    for it in (([np] if np else []) + STATE["items"] + STATE["history"]):
        _, _, tot = react_counts(it["id"])
        if tot > 0:
            rp = STATE.get("reaction_pub", {}).get(it["id"], {})
            pub_tables = set()
            for e in EMOJIS:
                for tok in rp.get(e, ()):
                    tok_sess = get_session(tok)
                    if tok_sess:
                        pub_tables.add(tok_sess["table"])
            a = agg.setdefault(it["yt"], {"yt": it["yt"], "title": it["title"],
                                          "artist": it.get("artist", ""), "total": 0, "tables": []})
            a["total"] += tot
            for t in pub_tables:
                if t not in a["tables"]:
                    a["tables"].append(t)
    top_loved = sorted(agg.values(), key=lambda x: -x["total"])[:5]
    req_songs = {}
    for e in STATE.get("request_log", []):
        yt = e.get("yt", "")
        if not yt:
            continue
        if yt not in req_songs:
            req_songs[yt] = {"yt": yt, "title": e.get("title", "?"), "artist": e.get("artist", ""), "count": 0}
        req_songs[yt]["count"] += 1
    top_requested = sorted(req_songs.values(), key=lambda x: -x["count"])[:10]

    # ---- Top mesas de la noche ----
    _skip_tables = ("Lista del local", "Admin", "Bis ↩️", "Votación 🗳️")
    table_stats = {}
    for entry in STATE.get("request_log", []):
        tbl = entry.get("table", "")
        if tbl and tbl not in _skip_tables:
            if tbl not in table_stats:
                table_stats[tbl] = {"table": tbl, "songs_requested": 0, "likes_received": 0}
            table_stats[tbl]["songs_requested"] += 1
    for it in (([np] if np else []) + STATE["items"] + STATE["history"]):
        tbl = it.get("table", "")
        if tbl and tbl not in _skip_tables:
            _, _, tot = react_counts(it["id"])
            if tot > 0:
                if tbl not in table_stats:
                    table_stats[tbl] = {"table": tbl, "songs_requested": 0, "likes_received": 0}
                table_stats[tbl]["likes_received"] += tot
    top_tables = sorted(table_stats.values(), key=lambda x: -(x["songs_requested"] + x["likes_received"]))[:5]

    # ---- Dedica pending (para TV) ----
    if mark_dedica:
        try:
            mid = int(mark_dedica)
            for ded in STATE.get("dedicas", []):
                if ded["id"] == mid:
                    ded["shown_tv"] = True
                    break
        except Exception:
            pass
    dedica_pending = None
    for ded in reversed(STATE.get("dedicas", [])):
        if not ded.get("shown_tv"):
            dedica_pending = ded
            break

    # ---- Bis votes ----
    bis_votes_pub = {yt: len(v) for yt, v in STATE.get("bis_votes", {}).items()}
    my_bis_votes = []
    if token:
        for yt, voters in STATE.get("bis_votes", {}).items():
            if token in voters:
                my_bis_votes.append(yt)

    # ---- Poll (votación de próxima canción) ----
    poll_state = None
    _p = STATE.get("poll")
    if _p:
        my_vote_poll = None
        if token:
            for _yt, _voters in _p.get("votes", {}).items():
                if token in _voters:
                    my_vote_poll = _yt
                    break
        poll_state = {
            "options": _p.get("options", []),
            "votes": {_yt: len(_v) for _yt, _v in _p.get("votes", {}).items()},
            "my_vote": my_vote_poll,
            "active": _p.get("active", False),
        }

    # ---- Vibe votes ----
    vibe_votes_state = STATE.get("vibe_votes", {})
    my_vibe = None
    if token:
        for _vemoji, _vvoters in vibe_votes_state.items():
            if token in _vvoters:
                my_vibe = _vemoji
                break
    vibe_state = {v: len(vibe_votes_state.get(v, set())) for v in VIBES}
    vibe_state["my_vote"] = my_vibe

    out = {
        "settings": dict({k: s.get(k) for k in ("venue_name", "price_priority", "style", "auto_approve",
                                       "genre", "credit_packages", "time_pass",
                                       "repeat_block_min", "repeat_block_songs", "trim_end_secs",
                                       "free_per_window", "free_window_min", "jump_multiplier",
                                       "venue_logo", "max_priority_queue_min", "fallback_shuffle",
                                       "theme", "blocked_keywords", "allowed_keywords",
                                       "allow_skip_vote")},
                                       socials=TYM["socials"], tym_logo=TYM["tym_logo"]),
        "tables": [t["name"] for t in STATE["tables"]],
        "now_playing": np_pub,
        "jump_available": bool(np) and STATE.get("jump_used_for") != np["id"],
        "jump_price": s["price_priority"] * s.get("jump_multiplier", 3),
        "priority_queue_min": int(sum(i.get("duration", DEFAULT_DUR)
                                      for i in queue_view() if i.get("priority")) // 60),
        "tv_active": (time.time() - STATE.get("tv_lastseen", 0)) < 15,
        "active_sessions": len({se["table"] for se in STATE.get("sessions", {}).values()
                               if time.time() - se.get("created", 0) < 7200}),
        "assists": [a for a in STATE.get("assists", []) if not a.get("resolved")],
        "queue": qout,
        "queue_count": len(q),
        "queue_total_secs": int(acc),
        "now_remaining_secs": int(rem),
        "my_pos": my_pos,
        "my_wait_secs": my_wait,
        "history": history,
        "top_loved": top_loved,
        "top_requested": top_requested,
        "top_tables": top_tables,
        "dedica_pending": dedica_pending,
        "bis_votes": bis_votes_pub,
        "my_bis_votes": my_bis_votes,
        "bis_threshold": BIS_THRESHOLD,
        "poll": poll_state,
        "vibe": vibe_state,
        "skip_votes_count": len(STATE.get("skip_votes", set())),
        "skip_threshold": max(2, -(-len({se["table"] for se in STATE.get("sessions", {}).values()
                                         if time.time() - se.get("created", 0) < 7200}) // 2)),
        "my_skip_vote": bool(token and token in STATE.get("skip_votes", set())),
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
        # canciones que este usuario ha reaccionado (para "Mis likes" en social)
        my_liked = []
        for it in (([np] if np else []) + STATE["items"] + STATE["history"]):
            _, my_r, _ = react_counts(it["id"], token)
            if my_r:
                my_liked.append({"id": it["id"], "yt": it["yt"], "title": it["title"],
                                 "artist": it.get("artist", ""), "my_reacts": my_r})
        out["my_liked"] = my_liked[:20]
    if admin:
        out["pending"] = [public_item(i, token) for i in pending_view()]
        out["ledger"] = list(reversed(STATE["ledger"]))[:40]
        out["ledger_total"] = sum(l["amount"] for l in STATE["ledger"])
        out["curated"] = STATE["curated"]
        out["subscribers"] = list(reversed(TYM["subscribers"]))[:100]
        out["history"] = [{"id": h["id"], "yt": h["yt"], "title": h["title"],
                           "artist": h.get("artist", "")} for h in STATE["history"][:10]]
        out["repeat_exceptions"] = list(STATE.get("repeat_exceptions", set()))
        out["all_dedicas"] = list(reversed(STATE.get("dedicas", [])))[:30]
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
    # Populares: canciones del historial del local (lo que ha sonado aquí)
    seen = set()
    populares = []
    for h in STATE.get("history", []):
        if not h.get("fallback") and h.get("yt") and h["yt"] not in seen:
            seen.add(h["yt"])
            populares.append({"yt": h["yt"], "title": h["title"], "artist": h.get("artist", "")})
    # Complementar con artistas más pedidos si hay pocas canciones en historial
    if len(populares) < 6:
        top_artists = list({c["artist"] for c in counts if c.get("artist")})[:2]
        for art in top_artists:
            for r in yt_search(art, 5):
                if r["yt"] not in seen:
                    seen.add(r["yt"])
                    populares.append({"yt": r["yt"], "title": r["title"], "artist": r["artist"]})
    if not populares:
        populares = [{"yt": r["yt"], "title": r["title"], "artist": r["artist"]} for r in yt_search("musica popular colombia", 12)]
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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
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
        if path == "/api/qr":
            vid = self._q("v", DEFAULT_VID)
            base = PUBLIC_URL.rstrip("/") if PUBLIC_URL else f"http://{lan_ip()}:{PORT}"
            url = f"{base}/?v={vid}"
            try:
                import qrcode, io
                buf = io.BytesIO()
                qrcode.make(url).save(buf, "PNG")
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send(500, {"error": str(e)})
            return
        if path == "/api/catalog":
            return self._send(200, CATALOG)
        if path == "/api/search":
            ip = self.client_address[0]
            if not _rate_ok(ip, "search"):
                return self._send(429, {"error": "Demasiadas búsquedas. Espera un momento."})
            q = self._q("q")[:150]
            return self._send(200, yt_search(q))
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
                return self._send(200, public_state(self._q("token") or None, admin,
                                                       mark_dedica=self._q("mark_dedica") or None))
        if path == "/api/admin/tables":
            av = self.authed_venue()
            if not av or av not in VENUES:
                return self._send(401, {"error": "no auth"})
            with LOCK:
                self.set_venue(av)
                return self._send(200, STATE["tables"])
        if path == "/api/admin/analytics":
            av = self.authed_venue()
            if not av or av not in VENUES:
                return self._send(401, {"error": "no auth"})
            with LOCK:
                return self._send(200, venue_analytics(av))
        if path == "/api/admin/request_log":
            av = self.authed_venue()
            if not av or av not in VENUES:
                return self._send(401, {"error": "no auth"})
            with LOCK:
                days = min(int(self._q("days") or 3), 3)
                cutoff = time.time() - days * 24 * 3600
                log = [e for e in VENUES[av].get("request_log", []) if e["ts"] >= cutoff]
                return self._send(200, {"log": list(reversed(log))})
        if path == "/api/tym/request_log":
            if self.authed_venue() != "*":
                return self._send(403, {"error": "Solo TYM master"})
            with LOCK:
                vid_filter = self._q("v")
                days = min(int(self._q("days") or 3), 3)
                cutoff = time.time() - days * 24 * 3600
                result = {}
                for vid, venue in VENUES.items():
                    if vid_filter and vid != vid_filter:
                        continue
                    log = [dict(e, venue=vid, venue_name=venue["settings"]["venue_name"])
                           for e in venue.get("request_log", []) if e["ts"] >= cutoff]
                    result[vid] = {"name": venue["settings"]["venue_name"], "log": list(reversed(log))}
                return self._send(200, result)
        if path == "/manifest.json":
            v = self._q("v") or DEFAULT_VID
            name = VENUES.get(v, VENUES[DEFAULT_VID])["settings"]["venue_name"]
            m = {"name": f"TYM Music — {name}", "short_name": "TYM Music",
                 "description": "Pon tu música en el local",
                 "start_url": f"/?v={v}", "display": "standalone",
                 "background_color": "#0e1320", "theme_color": "#0e1320",
                 "icons": [
                     {"src": "/icon.svg", "type": "image/svg+xml", "sizes": "any", "purpose": "any maskable"},
                     {"src": "/icon-192.png", "type": "image/png", "sizes": "192x192", "purpose": "any maskable"},
                     {"src": "/icon-512.png", "type": "image/png", "sizes": "512x512", "purpose": "any maskable"},
                 ]}
            return self._send(200, m, "application/manifest+json; charset=utf-8")
        if path == "/icon.svg":
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
                   '<rect width="100" height="100" rx="20" fill="#0e1320"/>'
                   '<text x="50" y="63" font-family="Arial Black,Arial" font-weight="900" '
                   'font-size="34" fill="#f5b301" text-anchor="middle">TYM</text></svg>')
            return self._send(200, svg.encode(), "image/svg+xml")
        if path in ("/icon-192.png", "/icon-512.png"):
            size = 192 if "192" in path else 512
            return self._send(200, _make_icon_png(size), "image/png")
        if path == "/offline.html":
            return self._file("offline.html", "text/html; charset=utf-8")
        if path == "/sw.js":
            return self._file("sw.js", "application/javascript; charset=utf-8")
        if path == "/version.js":
            js = (
                f'const TYM_VERSION="{VERSION}";\n'
                'document.addEventListener("DOMContentLoaded",()=>{'
                'document.querySelectorAll("[data-tym-version]")'
                '.forEach(e=>e.textContent="v"+TYM_VERSION);});\n'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(js)))
            self.end_headers(); self.wfile.write(js); return
        return self._send(404, {"error": "not found"})

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        if n > 65536:
            self.rfile.read(min(n, 131072))  # consume para no romper la conexión
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
            ip = self.client_address[0]
            if not _rate_ok(ip, "login"):
                return self._send(429, {"error": "Demasiados intentos. Espera un momento."})
            u = (d.get("user") or "").strip()
            o = TYM["owners"].get(u)
            input_hash = _hash_pass(d.get("pass") or "")
            if not o or not hmac.compare_digest(input_hash, o.get("pass_hash", "")):
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
                       "/api/admin/curated", "/api/admin/close_table", "/api/admin/reset",
                       "/api/admin/add", "/api/admin/allow_repeat", "/api/admin/move",
                       "/api/admin/poll", "/api/admin/poll/close", "/api/admin/vibe/reset",
                       "/api/admin/dedica/delete", "/api/admin/change_password")
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
                if not _rate_ok(self.client_address[0], "session"):
                    return self._send(429, {"error": "Demasiados intentos. Espera un momento."})
                table = find_table_by_pin(d.get("pin"))
                if not table:
                    return self._send(400, {"error": "Código de mesa incorrecto"})
                token = gen_token()
                STATE["sessions"][token] = {"table": table, "credits": 0, "pass_until": 0, "created": time.time()}
                TOKENS[token] = CUR_VID
                open_account(token, CUR_VID, table)   # nueva cuenta de mesa (analítica)
                # Cuenta existente de la mesa (cobros pendientes de sesiones anteriores no cerradas)
                existing_tab = sum(l["amount"] for l in STATE["ledger"] if l["table"] == table)
                if existing_tab > 0:
                    TYM["events"].append({"venue": CUR_VID, "table": table, "account": token,
                                          "ts": time.time(), "ev": "session_with_existing_tab",
                                          "existing_tab": existing_tab})
                return self._send(200, {"ok": True, "token": token,
                                        "existing_tab": existing_tab,
                                        "session": {"table": table, "credits": 0, "pass_until": 0}})

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
                if not _rate_ok(self.client_address[0], "request"):
                    return self._send(429, {"error": "Demasiadas solicitudes. Espera un momento."})
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
                # Filtro de contenido: palabras bloqueadas / permitidas
                _haystack = (title + " " + (artist or "")).lower()
                _blocked = [kw.strip().lower() for kw in STATE["settings"].get("blocked_keywords", []) if kw.strip()]
                if _blocked and any(kw in _haystack for kw in _blocked):
                    return self._send(400, {"error": "Esta canción no está disponible en este local 🎵", "content_blocked": True})
                _allowed = [kw.strip().lower() for kw in STATE["settings"].get("allowed_keywords", []) if kw.strip()]
                if _allowed and not any(kw in _haystack for kw in _allowed):
                    return self._send(400, {"error": "Esta canción no encaja con la música del local. ¡Prueba con otra! 🎶", "content_blocked": True})
                if in_play_or_queue(yt):
                    fresh = next((i for i in STATE["items"] if i["yt"] == yt and now - i.get("ts", 0) < 10), None)
                    if fresh:
                        return self._send(400, {"error": "Alguien más acaba de pedir esa canción al mismo tiempo. ¡Ya está en camino! 🎶", "race": True})
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
                        return self._send(400, {"error": "Alguien llegó primero al salto. Inténtalo en la próxima canción 🎵", "jump_conflict": True})
                    mode = "salto"; ckind = "salto al #1"
                    charge = STATE["settings"]["price_priority"] * STATE["settings"].get("jump_multiplier", 3)
                elif priority:
                    # Bloqueo de cola larga: si hay demasiados min de canciones premium, bloquear
                    mqm = int(STATE["settings"].get("max_priority_queue_min", 0))
                    if mqm > 0:
                        pq_secs = sum(i.get("duration", DEFAULT_DUR) for i in queue_view() if i.get("priority"))
                        if pq_secs // 60 >= mqm:
                            return self._send(400, {"error": f"La cola tiene más de {mqm} min de prioridades. Intenta en un rato 🎶",
                                                    "priority_queue_full": True,
                                                    "priority_queue_min": int(pq_secs // 60)})
                    if sess["pass_until"] > now:
                        mode = "pase"
                    elif sess["credits"] > 0:
                        sess["credits"] -= 1; mode = "credito"
                    else:
                        mode = "single"; charge = STATE["settings"]["price_priority"]
                req_msg = (d.get("message") or "").strip()[:80]
                item = {"id": nid(), "title": title, "artist": artist, "yt": yt,
                        "token": d.get("token"), "table": table, "priority": priority,
                        "super": sup, "mode": mode, "duration": dur,
                        "status": "approved" if STATE["settings"]["auto_approve"] else "pending",
                        "play_status": "pending", "played_enough": False, "requeue_count": 0,
                        "ts": time.time(), "charge_on_play": charge, "charged": False, "charge_kind": ckind,
                        "message": req_msg}
                STATE["items"].append(item)
                bump_count(yt, title, artist)
                log_order(table, d.get("token"), mode, title, yt)   # analítica (free/premium)
                # Log de pedidos (3 días)
                _now = time.time()
                STATE["request_log"].append({
                    "ts": _now, "title": title, "artist": artist or "",
                    "yt": yt, "table": table, "mode": mode,
                    "priority": priority, "charge": charge,
                })
                _cutoff = _now - 3 * 24 * 3600
                if len(STATE["request_log"]) > 5000 or (STATE["request_log"] and STATE["request_log"][0]["ts"] < _cutoff):
                    STATE["request_log"] = [e for e in STATE["request_log"] if e["ts"] >= _cutoff]
                if sup and STATE["now_playing"]:
                    STATE["jump_used_for"] = STATE["now_playing"]["id"]
                if STATE["now_playing"] is None and item["status"] == "approved":
                    promote_next()
                return self._send(200, {"ok": True, "mode": mode,
                                        "session": {"table": table, "credits": sess["credits"],
                                                    "pass_until": sess["pass_until"]}})

            # ---- Reaccionar (positivo) ----
            if path == "/api/react":
                if not _rate_ok(self.client_address[0], "social"):
                    return self._send(429, {"error": "Demasiadas reacciones. Espera un momento."})
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
                rp = STATE["reaction_pub"].setdefault(item_id, {e: set() for e in EMOJIS})
                tok = d.get("token")
                pub = bool(d.get("public", True))
                if tok in r[emoji]:
                    r[emoji].discard(tok)
                    rp[emoji].discard(tok)
                else:
                    r[emoji].add(tok)
                    if pub:
                        rp[emoji].add(tok)
                    else:
                        rp[emoji].discard(tok)
                    now_ts = time.time()
                    log = STATE.setdefault("react_log", [])
                    log.append({"emoji": emoji, "table": sess["table"] if pub else None,
                                "ts": now_ts, "item_id": item_id})
                    STATE["react_log"] = [e for e in log if now_ts - e["ts"] < 60]
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
                    return self._send(400, {"error": "Alguien llegó primero al salto. Disponible en la próxima canción 🎵", "jump_conflict": True})
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
            if path == "/api/feature_interest":
                feature = (d.get("feature") or "")[:64]
                sess_fi = get_session(d.get("token"))
                TYM.setdefault("feature_interest", []).append({
                    "feature": feature, "venue": CUR_VID,
                    "table": sess_fi["table"] if sess_fi else "",
                    "ts": time.time()
                })
                return self._send(200, {"ok": True})

            if path == "/api/subscribe":
                email = (d.get("email") or "").strip()
                if "@" not in email or "." not in email or len(email) < 5:
                    return self._send(400, {"error": "Email inválido"})
                sess = get_session(d.get("token"))
                TYM["subscribers"].append({"email": email[:120],
                                           "table": sess["table"] if sess else "",
                                           "venue": CUR_VID, "ts": time.time()})
                return self._send(200, {"ok": True})

            # ---- Solicitud de asistencia en mesa ----
            if path == "/api/assist":
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Sesión no válida"})
                tok = d.get("token")
                if d.get("cancel"):
                    STATE["assists"] = [a for a in STATE.get("assists", []) if a.get("token") != tok]
                    return self._send(200, {"ok": True})
                # Buzz en asistencia existente (con cooldown)
                def _do_buzz(a):
                    now_t = time.time()
                    since_created = now_t - a.get("ts", now_t)
                    since_buzzed = now_t - a.get("buzzed_at", 0)
                    # Primer minuto: no se permite buzz (dar tiempo al personal de verlo)
                    if since_created < 60 and not a.get("buzzed_at"):
                        wait = int(60 - since_created)
                        return self._send(400, {"error": "Espera antes de volver a llamar", "wait": wait})
                    if since_buzzed < 30:
                        return self._send(400, {"error": "Espera antes de volver a llamar", "wait": int(30 - since_buzzed)})
                    a["buzzed_at"] = now_t
                    a["buzz_count"] = a.get("buzz_count", 1) + 1
                    return self._send(200, {"ok": True, "buzzed": True, "id": a["id"]})
                if d.get("buzz"):
                    for a in STATE.get("assists", []):
                        if a.get("token") == tok and not a.get("resolved"):
                            return _do_buzz(a)
                    return self._send(400, {"error": "No tienes una asistencia activa"})
                # Nueva solicitud — si ya hay una activa del mismo token, tratar como buzz
                existing = next((a for a in STATE.get("assists", [])
                                 if a.get("token") == tok and not a.get("resolved")), None)
                if existing:
                    return _do_buzz(existing)
                aid = nid()
                STATE["assists"].append({"id": aid, "table": sess["table"],
                                          "ts": time.time(), "resolved": False,
                                          "resolve_ts": None, "token": tok,
                                          "buzz_count": 1})
                TYM["events"].append({"venue": CUR_VID, "table": sess["table"],
                                       "account": tok, "ts": time.time(),
                                       "ev": "assist_requested"})
                return self._send(200, {"ok": True, "id": aid})

            if path == "/api/admin/assist_resolve":
                aid = d.get("id")
                for a in STATE.get("assists", []):
                    if a["id"] == aid:
                        a["resolved"] = True; a["resolve_ts"] = time.time(); break
                return self._send(200, {"ok": True})

            # ---- TV ping (marca TV como activa) ----
            if path == "/api/tv_ping":
                STATE["tv_lastseen"] = time.time()
                return self._send(200, {"ok": True})

            # ---- Player reporta progreso ----
            if path == "/api/progress":
                if STATE["now_playing"]:
                    try:
                        pos = max(0, int(float(d.get("position", 0))))
                        STATE["now_playing"]["position"] = pos
                        if d.get("duration"):
                            STATE["now_playing"]["duration"] = max(1, int(float(d["duration"])))
                        dur = STATE["now_playing"].get("duration") or DEFAULT_DUR
                        if pos >= dur * 0.5 and not STATE["now_playing"].get("played_enough"):
                            STATE["now_playing"]["played_enough"] = True
                    except Exception:
                        pass
                return self._send(200, {"ok": True})

            if path == "/api/advance":
                # Idempotencia: el cliente envía from_id (el id de la canción que cree que suena).
                # Si ya avanzamos (otra pestaña/llamada duplicada), devolvemos el estado actual sin cambios.
                from_id = d.get("from_id")  # None = no enviado (cliente viejo); "" = nada sonando
                if from_id is not None:
                    cur = STATE.get("now_playing")
                    cur_id = cur.get("id") if cur else ""
                    if cur_id != from_id:
                        return self._send(200, {"ok": True, "now_playing": cur, "noop": True})
                # Trim aprendido: si el local salta cerca del final, aprende ese punto para esa canción
                if d.get("manual") and d.get("yt"):
                    try:
                        pos = float(d.get("position", 0)); dur = float(d.get("duration", 0))
                        if dur > 30 and dur * 0.5 <= pos < dur - 1:
                            STATE["learned_end"][d["yt"]] = int(pos)
                    except Exception:
                        pass
                return self._send(200, {"ok": True, "now_playing": promote_next(manual=bool(d.get("manual")))})

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
                          "free_per_window", "free_window_min", "jump_multiplier",
                          "max_priority_queue_min"):
                    if k in d:
                        try: s[k] = max(0, int(d[k]))
                        except Exception: pass
                for k in ("fallback_shuffle",):
                    if k in d:
                        s[k] = bool(d[k])
                if "theme" in d and d["theme"] in ("azul", "purpura", "verde"):
                    s["theme"] = d["theme"]
                for k in ("blocked_keywords", "allowed_keywords"):
                    if k in d and isinstance(d[k], list):
                        s[k] = [str(w).strip().lower()[:50] for w in d[k] if str(w).strip()][:30]
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

            if path == "/api/admin/add":
                yt = (d.get("yt") or "").strip()
                title = str(d.get("title") or "Canción")[:120]
                artist = str(d.get("artist") or "")[:80]
                dur = _parse_len(d.get("length")) or DEFAULT_DUR
                if not yt:
                    return self._send(400, {"error": "Falta yt"})
                pos = d.get("position")
                item = {"id": nid(), "title": title, "artist": artist, "yt": yt,
                        "token": None, "table": "Admin", "priority": False, "super": False,
                        "mode": "normal", "duration": dur, "status": "approved",
                        "play_status": "pending", "played_enough": False, "requeue_count": 0,
                        "ts": time.time(), "charge_on_play": 0, "charged": False, "charge_kind": ""}
                q = [i for i in STATE["items"] if i["status"] == "approved"]
                pending = [i for i in STATE["items"] if i["status"] != "approved"]
                if pos is None:
                    q.append(item)
                else:
                    idx = max(0, min(int(pos) - 1, len(q)))
                    q.insert(idx, item)
                STATE["items"] = q + pending
                return self._send(200, {"ok": True})

            if path == "/api/admin/allow_repeat":
                yt = (d.get("yt") or "").strip()
                if not yt:
                    return self._send(400, {"error": "Falta yt"})
                exc = STATE.setdefault("repeat_exceptions", set())
                if yt in exc:
                    exc.discard(yt)
                    return self._send(200, {"ok": True, "allowed": False})
                exc.add(yt)
                return self._send(200, {"ok": True, "allowed": True})

            if path == "/api/admin/move":
                item_id = d.get("id")
                direction = d.get("dir")  # "up" o "down"
                q = queue_view()   # sorted order — this is what we're reordering
                idx = next((i for i, it in enumerate(q) if it["id"] == item_id), None)
                if idx is None:
                    return self._send(400, {"error": "No encontrado"})
                if direction == "up" and idx > 0:
                    # Swap ts so queue_view sort preserves new order
                    q[idx]["ts"], q[idx - 1]["ts"] = q[idx - 1]["ts"], q[idx]["ts"]
                elif direction == "down" and idx < len(q) - 1:
                    q[idx]["ts"], q[idx + 1]["ts"] = q[idx + 1]["ts"], q[idx]["ts"]
                return self._send(200, {"ok": True})

            if path == "/api/admin/close_table":
                tbl = d.get("table")
                total = close_accounts(CUR_VID, tbl)   # marca cuentas cerradas (hora fin) + total
                STATE["ledger"] = [l for l in STATE["ledger"] if l["table"] != tbl]
                # Cancela canciones pendientes de esa mesa: si aún no sonaron, no se cobran
                STATE["items"] = [i for i in STATE["items"] if i.get("table") != tbl]
                # Si la canción que suena ahora es de esa mesa y tiene cobro pendiente, cancelar
                np = STATE.get("now_playing")
                if np and np.get("table") == tbl and np.get("charge_on_play", 0) > 0 and not np.get("charged"):
                    np["charge_on_play"] = 0
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
                STATE["reaction_pub"] = {}
                STATE["react_log"] = []
                STATE["repeat_exceptions"] = set()
                STATE["assists"] = []
                STATE["jump_used_for"] = None
                STATE["dedicas"] = []
                STATE["bis_votes"] = {}
                STATE["poll"] = None
                STATE["vibe_votes"] = {}
                STATE["skip_votes"] = set()
                _id[0] = 0
                FB_IDX[0] = 0
                return self._send(200, {"ok": True})

            # ---- Dedicatorias ----
            if path == "/api/dedica":
                if not _rate_ok(self.client_address[0], "social"):
                    return self._send(429, {"error": "Demasiadas solicitudes. Espera un momento."})
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa primero."})
                to_table = (d.get("to_table") or "").strip()
                message = (d.get("message") or "").strip()
                valid_tables = [t["name"] for t in STATE["tables"]]
                if to_table not in valid_tables:
                    return self._send(400, {"error": "Mesa de destino inválida."})
                if not message:
                    return self._send(400, {"error": "El mensaje no puede estar vacío."})
                if len(message) > 80:
                    return self._send(400, {"error": "El mensaje no puede tener más de 80 caracteres."})
                ded = {"id": nid(), "from_table": sess["table"], "to_table": to_table,
                       "message": message, "ts": time.time(), "shown_tv": False}
                STATE.setdefault("dedicas", []).append(ded)
                if len(STATE["dedicas"]) > 50:
                    STATE["dedicas"] = STATE["dedicas"][-50:]
                return self._send(200, {"ok": True})

            # ---- Solicitar bis ----
            if path == "/api/bis":
                if not _rate_ok(self.client_address[0], "social"):
                    return self._send(429, {"error": "Demasiadas solicitudes. Espera un momento."})
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa primero."})
                yt = (d.get("yt") or "").strip()
                if not yt:
                    return self._send(400, {"error": "Falta el ID de la canción."})
                tok = d.get("token")
                bis_votes = STATE.setdefault("bis_votes", {})
                if yt not in bis_votes:
                    bis_votes[yt] = set()
                voters = bis_votes[yt]
                if tok in voters:
                    voters.discard(tok)
                    count = len(voters)
                    return self._send(200, {"ok": True, "voted": False, "count": count})
                voters.add(tok)
                count = len(voters)
                if count >= BIS_THRESHOLD:
                    song = next((h for h in STATE.get("history", []) if h.get("yt") == yt), None)
                    if song:
                        bis_item = {"id": nid(), "title": song["title"], "artist": song.get("artist", ""),
                                    "yt": yt, "token": None, "table": "Bis ↩️", "priority": False,
                                    "super": False, "mode": "normal", "duration": song.get("duration", DEFAULT_DUR),
                                    "status": "approved", "play_status": "pending", "played_enough": False,
                                    "requeue_count": 0, "ts": time.time(), "charge_on_play": 0,
                                    "charged": False, "charge_kind": "", "repeat_exception": True, "message": ""}
                        STATE["items"].append(bis_item)
                        STATE.setdefault("repeat_exceptions", set()).add(yt)
                    bis_votes.pop(yt, None)
                    if STATE.get("now_playing") is None:
                        promote_next()
                    return self._send(200, {"ok": True, "voted": True, "count": count, "queued": True})
                return self._send(200, {"ok": True, "voted": True, "count": count})

            # ---- Voto en votación ----
            if path == "/api/poll/vote":
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa primero."})
                _p = STATE.get("poll")
                if not _p or not _p.get("active"):
                    return self._send(400, {"error": "No hay votación activa."})
                yt = (d.get("yt") or "").strip()
                valid_yts = [opt["yt"] for opt in _p.get("options", [])]
                if yt not in valid_yts:
                    return self._send(400, {"error": "Opción inválida."})
                tok = d.get("token")
                votes = _p.setdefault("votes", {})
                already_voted = next((v_yt for v_yt, v_set in votes.items() if tok in v_set), None)
                if already_voted == yt:
                    votes[yt].discard(tok)
                    my_vote = None
                else:
                    if already_voted:
                        votes[already_voted].discard(tok)
                    votes.setdefault(yt, set()).add(tok)
                    my_vote = yt
                return self._send(200, {"ok": True, "votes": {k: len(v) for k, v in votes.items()},
                                        "my_vote": my_vote})

            # ---- Vibe ----
            if path == "/api/skip_vote":
                if not _rate_ok(self.client_address[0], "social"):
                    return self._send(429, {"error": "Demasiadas solicitudes. Espera un momento."})
                if not STATE["settings"].get("allow_skip_vote"):
                    return self._send(400, {"error": "El skip por votación no está activado en este local."})
                np_sv = STATE.get("now_playing")
                if not np_sv or np_sv.get("fallback"):
                    return self._send(400, {"error": "No hay canción de cliente sonando ahora."})
                sess_sv = get_session(d.get("token"))
                if not sess_sv:
                    return self._send(400, {"error": "Ingresa el código de tu mesa primero."})
                tok = d.get("token")
                sv = STATE.setdefault("skip_votes", set())
                if tok in sv:
                    sv.discard(tok)
                    my_vote = False
                else:
                    sv.add(tok)
                    my_vote = True
                active_tables = len({se["table"] for se in STATE.get("sessions", {}).values()
                                     if time.time() - se.get("created", 0) < 7200})
                threshold = max(2, -(-active_tables // 2))
                skipped = False
                if len(sv) >= threshold:
                    promote_next(manual=True)
                    skipped = True
                return self._send(200, {"ok": True, "my_vote": my_vote,
                                        "count": len(STATE.get("skip_votes", set())),
                                        "threshold": threshold, "skipped": skipped})

            if path == "/api/vibe":
                if not _rate_ok(self.client_address[0], "social"):
                    return self._send(429, {"error": "Demasiadas solicitudes. Espera un momento."})
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa primero."})
                emoji = d.get("emoji", "")
                if emoji not in VIBES:
                    return self._send(400, {"error": "Vibe inválido."})
                tok = d.get("token")
                vibe_votes = STATE.setdefault("vibe_votes", {})
                already_this = tok in vibe_votes.get(emoji, set())
                for v in VIBES:
                    if v in vibe_votes:
                        vibe_votes[v].discard(tok)
                if not already_this:
                    vibe_votes.setdefault(emoji, set()).add(tok)
                my_vibe = None if already_this else emoji
                counts = {v: len(vibe_votes.get(v, set())) for v in VIBES}
                return self._send(200, {"ok": True, "vibe": counts, "my_vote": my_vibe})

            # ---- Admin: crear votación ----
            if path == "/api/admin/poll":
                options = d.get("options", [])
                valid_opts = [{"yt": (o.get("yt") or "").strip(),
                               "title": str(o.get("title", "Canción"))[:120],
                               "artist": str(o.get("artist", ""))[:80]}
                              for o in options if (o.get("yt") or "").strip()]
                if len(valid_opts) < 2:
                    return self._send(400, {"error": "Se necesitan al menos 2 opciones válidas."})
                if len(valid_opts) > 3:
                    valid_opts = valid_opts[:3]
                STATE["poll"] = {"options": valid_opts,
                                 "votes": {o["yt"]: set() for o in valid_opts},
                                 "active": True, "created_at": time.time()}
                return self._send(200, {"ok": True})

            # ---- Admin: cerrar votación ----
            if path == "/api/admin/poll/close":
                _p = STATE.get("poll")
                if not _p:
                    return self._send(400, {"error": "No hay votación activa."})
                _p["active"] = False
                votes = _p.get("votes", {})
                if votes:
                    winner_yt = max(votes.keys(), key=lambda _y: len(votes[_y]))
                    if len(votes[winner_yt]) > 0:
                        winner_opt = next((o for o in _p.get("options", []) if o["yt"] == winner_yt), None)
                        if winner_opt:
                            poll_item = {"id": nid(), "title": winner_opt["title"],
                                         "artist": winner_opt.get("artist", ""), "yt": winner_yt,
                                         "token": None, "table": "Votación 🗳️", "priority": False,
                                         "super": False, "mode": "normal", "duration": DEFAULT_DUR,
                                         "status": "approved", "play_status": "pending", "played_enough": False,
                                         "requeue_count": 0, "ts": time.time(), "charge_on_play": 0,
                                         "charged": False, "charge_kind": "", "repeat_exception": True, "message": ""}
                            STATE["items"].append(poll_item)
                            STATE.setdefault("repeat_exceptions", set()).add(winner_yt)
                            if STATE.get("now_playing") is None:
                                promote_next()
                return self._send(200, {"ok": True})

            # ---- Admin: reiniciar vibe ----
            if path == "/api/admin/vibe/reset":
                STATE["vibe_votes"] = {}
                return self._send(200, {"ok": True})

            # ---- Admin: cambiar contraseña propia ----
            if path == "/api/admin/change_password":
                av = self.authed_venue()
                new_pass = (d.get("new_pass") or "").strip()
                if len(new_pass) < 8:
                    return self._send(400, {"error": "La contraseña debe tener al menos 8 caracteres."})
                updated = False
                for uname, odata in TYM["owners"].items():
                    if odata.get("venue") == av:
                        odata["pass_hash"] = _hash_pass(new_pass)
                        updated = True
                        break
                if not updated:
                    return self._send(400, {"error": "No se encontró el usuario."})
                save_state()
                return self._send(200, {"ok": True})

            # ---- Admin: eliminar dedicatoria ----
            if path == "/api/admin/dedica/delete":
                ded_id = d.get("id")
                if ded_id is None:
                    return self._send(400, {"error": "Falta el id."})
                STATE["dedicas"] = [dd for dd in STATE.get("dedicas", []) if dd["id"] != ded_id]
                return self._send(200, {"ok": True})

        # ---- TYM Master: gestión de locales ----
        if path == "/api/tym/create_venue":
            if self.authed_venue() != "*":
                return self._send(403, {"error": "Solo TYM master"})
            vid = re.sub(r"[^a-z0-9_-]", "", (d.get("vid") or "").strip().lower())[:24]
            name = str(d.get("name") or "").strip()[:60]
            username = re.sub(r"\s+", "", str(d.get("username") or "").strip())[:32]
            password = str(d.get("password") or "").strip()[:64]
            if not vid or not name or not username or not password:
                return self._send(400, {"error": "Completa todos los campos"})
            with LOCK:
                if vid in VENUES:
                    return self._send(400, {"error": f"El ID '{vid}' ya existe"})
                if username in TYM["owners"]:
                    return self._send(400, {"error": f"El usuario '{username}' ya existe"})
                VENUES[vid] = make_venue(name)
                TYM["owners"][username] = {"pass_hash": _hash_pass(password), "venue": vid}
                save_state()
            return self._send(200, {"ok": True, "vid": vid, "name": name})

        return self._send(404, {"error": "not found"})

# =================== Persistencia (archivo JSON; fácil de migrar a DB/multi-bar) ===================
DATA_FILE = os.path.join(HERE, "data.json")
PERSIST_KEYS = ("settings", "tables", "sessions", "now_playing", "items", "ledger",
                "history", "curated", "req_counts", "jump_used_for", "learned_end", "assists",
                "request_log", "dedicas")

def venue_snapshot(v):
    snap = {k: v[k] for k in PERSIST_KEYS}
    snap["reactions"] = {str(k): {e: list(s) for e, s in r.items()}
                         for k, r in v["reactions"].items()}
    snap["reaction_pub"] = {str(k): {e: list(s) for e, s in r.items()}
                            for k, r in v.get("reaction_pub", {}).items()}
    snap["repeat_exceptions"] = list(v.get("repeat_exceptions", set()))
    snap["bis_votes"] = {yt: list(tokens) for yt, tokens in v.get("bis_votes", {}).items()}
    snap["vibe_votes"] = {emoji: list(tokens) for emoji, tokens in v.get("vibe_votes", {}).items()}
    snap["skip_votes"] = list(v.get("skip_votes", set()))
    _poll = v.get("poll")
    if _poll:
        _pc = dict(_poll)
        _pc["votes"] = {yt: list(tokens) for yt, tokens in _pc.get("votes", {}).items()}
        snap["poll"] = _pc
    else:
        snap["poll"] = None
    return snap

def _redis_strip_logos(data):
    """Copia del estado sin logos base64 (pueden ser muy pesados para Redis free)."""
    import copy
    d = copy.deepcopy(data)
    d.get("tym", {}).pop("tym_logo", None)
    for v in d.get("venues", {}).values():
        v.get("settings", {}).pop("venue_logo", None)
    return d

def redis_save(data):
    """Guarda en Upstash Redis vía REST API (sin dependencias extra)."""
    if not REDIS_URL or not REDIS_TOKEN:
        return
    try:
        payload = json.dumps(["SET", REDIS_KEY, json.dumps(data, ensure_ascii=False)]).encode("utf-8")
        req = urllib.request.Request(REDIS_URL,
            data=payload,
            headers={"Authorization": f"Bearer {REDIS_TOKEN}",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("redis_save error:", e)

def redis_load():
    """Recupera estado desde Upstash Redis."""
    if not REDIS_URL or not REDIS_TOKEN:
        return None
    try:
        req = urllib.request.Request(f"{REDIS_URL}/get/{REDIS_KEY}",
            headers={"Authorization": f"Bearer {REDIS_TOKEN}"})
        result = json.loads(urllib.request.urlopen(req, timeout=10).read())
        val = result.get("result")
        if val:
            return json.loads(val)
    except Exception as e:
        print("redis_load error:", e)
    return None

_redis_last_save = [0]

def save_state():
    """{version:3, tym:{...global...}, venues:{id:{...bar...}}} — multi-bar; listo para DB."""
    try:
        data = {"version": 3, "ids": {"_id": _id[0], "_fb": FB_IDX[0]},
                "tym": TYM, "venues": {vid: venue_snapshot(v) for vid, v in VENUES.items()}}
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
        # Backup a Redis: máx una vez por minuto, en hilo separado para no bloquear
        now = time.time()
        if REDIS_URL and REDIS_TOKEN and now - _redis_last_save[0] > 60:
            _redis_last_save[0] = now
            threading.Thread(target=redis_save, args=(_redis_strip_logos(data),), daemon=True).start()
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
    v["reaction_pub"] = {int(k): {e: set(lst) for e, lst in r.items()}
                         for k, r in snap.get("reaction_pub", {}).items()}
    v["repeat_exceptions"] = set(snap.get("repeat_exceptions", []))
    v["bis_votes"] = {yt: set(tokens) for yt, tokens in snap.get("bis_votes", {}).items()}
    v["vibe_votes"] = {emoji: set(tokens) for emoji, tokens in snap.get("vibe_votes", {}).items()}
    v["skip_votes"] = set(snap.get("skip_votes", []))
    _ps = snap.get("poll")
    if _ps:
        _pc = dict(_ps)
        _pc["votes"] = {yt: set(tokens) for yt, tokens in _pc.get("votes", {}).items()}
        v["poll"] = _pc
    else:
        v["poll"] = None

def load_state():
    d = None
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
            print("Estado cargado de archivo local")
        except Exception as e:
            print("load_state (archivo) error:", e)
    if d is None:
        print("No hay data.json local — buscando backup en Redis…")
        d = redis_load()
        if d:
            print("Estado recuperado de Redis ✓")
        else:
            print("Sin backup en Redis. Empezando desde cero.")
    if not d:
        return
    try:
        if "tym" in d:
            for k in ("socials", "tym_logo", "subscribers", "events", "accounts"):
                if k in d["tym"]:
                    TYM[k] = d["tym"][k]
            # Carga todos los owners desde la DB (iniciales + creados dinámicamente)
            for uname, odata in d["tym"].get("owners", {}).items():
                od = dict(odata)
                # Migra contraseñas en texto plano de versiones antiguas del código
                if "pass" in od and "pass_hash" not in od:
                    od["pass_hash"] = _hash_pass(od.pop("pass"))
                od.pop("pass", None)
                TYM["owners"][uname] = od
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
        print("Bares activos:", list(VENUES.keys()))
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
    _ensure_owner_passwords()
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
    print("  Contraseñas: ver logs de primer arranque o cambiar en panel admin → Ajustes")
    print("  PINs de mesa: 1111..5555")
    print("=" * 64)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
