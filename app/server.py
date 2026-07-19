#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TYM Music — MVP v2 (Python stdlib, sin dependencias).
Vistas: /  (cliente)  ·  /player (pantalla del local)  ·  /admin (dueno)
Novedades: sesion de mesa por PIN, paquetes (creditos + pase), progreso de
reproduccion, recomendadas (mas pedido / del local / populares / genero).
"""
import json, os, re, socket, threading, time, random, datetime, struct, zlib
import hashlib, hmac, secrets, smtplib
from email.mime.text import MIMEText
import urllib.request, urllib.parse
from zoneinfo import ZoneInfo, available_timezones
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FuturesTimeoutError
try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid
    _WEBPUSH_OK = True
except ImportError:
    _WEBPUSH_OK = False

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
VERSION = "0.0.9-demo"
PORT = int(os.environ.get("PORT", 8000))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
# Upstash Redis (backup remoto: evita perder datos en Render/hosts con disco efímero)
# Crea una DB gratuita en upstash.com → copia REST URL y token → ponlos como variables de entorno
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_KEY   = "tym_state"
DEFAULT_DUR = 210  # 3:30 si no se conoce la duracion
TV_OWNER_TIMEOUT = 8  # seg sin ping del dueño actual de la TV -> se libera (4x el intervalo de ping de 2s)
EMOJIS = ["❤️", "🔥", "👍"]  # reacciones positivas
VIBES = ["🔥 Que todo el mundo cante", "💃 Más baile", "🎸 Más suave", "✨ Así está perfecto"]
BIS_THRESHOLD = 3

DEFAULT_TZ = "America/Bogota"
_TZ_CACHE = {}
def _tz(name=None):
    """ZoneInfo del nombre IANA dado (con cache); si es inválido o falta, usa DEFAULT_TZ."""
    name = name or DEFAULT_TZ
    z = _TZ_CACHE.get(name)
    if z is None:
        try:
            z = ZoneInfo(name)
        except Exception:
            z = ZoneInfo(DEFAULT_TZ)
        _TZ_CACHE[name] = z
    return z

def _venue_hour(ts, vid=None):
    """Hora local (0-23) de un timestamp según la zona horaria configurada del venue."""
    v = VENUES.get(vid or CUR_VID, {})
    tzname = v.get("settings", {}).get("timezone")
    return datetime.datetime.fromtimestamp(ts, tz=_tz(tzname)).hour

def _scheduled_genre():
    """Devuelve el género activo según el horario programado del venue actual (su zona horaria)."""
    s = STATE["settings"]
    schedule = s.get("schedule", [])
    if not schedule:
        return s.get("genre", "reggaeton")
    now = datetime.datetime.now(tz=_tz(s.get("timezone")))
    cur = now.hour * 60 + now.minute
    for slot in schedule:
        try:
            fh, fm = map(int, slot["from"].split(":"))
            th, tm = map(int, slot["to"].split(":"))
            lo, hi = fh * 60 + fm, th * 60 + tm
            hit = (cur >= lo and cur < hi) if hi > lo else (cur >= lo or cur < hi)
            if hit:
                return slot.get("genre") or s.get("genre", "reggaeton")
        except Exception:
            pass
    return s.get("genre", "reggaeton")

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
PBKDF2_ITERS = 200_000

def _hash_pass(p):
    """SHA-256 sin salt — formato LEGACY, solo para verificar hashes viejos. No usar para crear nuevos."""
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def hash_password(p):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", p.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERS).hex()
    return f"pbkdf2${salt}${h}"

def verify_password(p, stored):
    """Devuelve (ok, needs_rehash). needs_rehash=True si el hash es del formato legacy
    (sha256 sin salt, de antes de esta migración) y debe re-guardarse en formato pbkdf2."""
    if not stored:
        return False, False
    if stored.startswith("pbkdf2$"):
        try:
            _, salt, h = stored.split("$", 2)
            calc = hashlib.pbkdf2_hmac("sha256", p.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERS).hex()
        except ValueError:
            return False, False
        return hmac.compare_digest(calc, h), False
    return hmac.compare_digest(_hash_pass(p), stored), True

_DUMMY_PASS_HASH = hash_password(secrets.token_hex(8))  # costo decoy para usuarios inexistentes (evita timing leak)

# ---- Envio de correo (recuperar clave) — stdlib puro, sin dependencias nuevas ----
# Dos vías, en este orden de prioridad:
# 1) Resend (API HTTPS, vía RESEND_API_KEY) — funciona en hosts que bloquean SMTP saliente
#    (ej. Render), porque viaja como tráfico HTTPS normal, no por el puerto 465/587.
#    Sin dominio propio verificado en Resend, usa RESEND_FROM=onboarding@resend.dev (su
#    dirección de pruebas, sirve para enviar sin configurar nada más).
# 2) SMTP (SMTP_HOST/PORT/USER/PASS) — sirve en dev local o en hosts que sí permiten SMTP.
#    SMTP_USER en Gmail necesita una "contraseña de aplicación" (Cuenta Google → Seguridad →
#    Verificación en 2 pasos → Contraseñas de aplicaciones), NO la contraseña normal.
# Sin ninguna de las dos configuradas, el correo queda solo en el log del servidor en vez de
# enviarse de verdad — así el flujo completo se puede probar sin credenciales.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "TYM Music <onboarding@resend.dev>")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

def mask_email(email):
    """Oculta la mayoría del usuario del correo pero deja el dominio visible completo,
    para que el dueño confirme a cuál dirección le llegó el correo sin exponerla entera."""
    local, _, domain = email.partition("@")
    if not domain:
        return email
    keep = min(3, max(1, len(local) - 1))
    return local[:keep] + "***@" + domain

def _send_email_resend(to_addr, subject, body):
    payload = json.dumps({"from": RESEND_FROM, "to": [to_addr], "subject": subject, "text": body}).encode("utf-8")
    # User-Agent explícito: el default de urllib ("Python-urllib/3.x") lo bloquea Cloudflare
    # (delante de la API de Resend) con error 1010 al detectarlo como firma de bot — confirmado
    # en vivo probando la misma llamada desde una terminal local (2026-07-18).
    req = urllib.request.Request("https://api.resend.com/emails", data=payload, method="POST",
                                  headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                           "Content-Type": "application/json",
                                           "User-Agent": "TYM-Music-Server/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        print(f"✉️  Error enviando correo (Resend) a {to_addr}:", e, flush=True)
        return False

def send_email(to_addr, subject, body):
    if not to_addr:
        return False
    if RESEND_API_KEY:
        return _send_email_resend(to_addr, subject, body)
    if not (SMTP_USER and SMTP_PASS):
        print(f"✉️  [correo simulado — falta RESEND_API_KEY o SMTP_USER/SMTP_PASS] Para: {to_addr} | Asunto: {subject}\n{body}", flush=True)
        return True
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = to_addr
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [to_addr], msg.as_string())
        return True
    except Exception as e:
        print(f"✉️  Error enviando correo a {to_addr}:", e, flush=True)
        return False

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
            odata["pass_hash"] = hash_password(pwd)
            changed = True
    if changed:
        save_state()

def _ensure_vapid_keys():
    if not _WEBPUSH_OK:
        return
    if TYM.get("vapid", {}).get("private_key_pem"):
        return
    try:
        v = Vapid()
        v.generate_keys()
        import base64
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat, PrivateFormat, NoEncryption)
        priv_pem = v.private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()).decode()
        pub_b64 = base64.urlsafe_b64encode(
            v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        ).rstrip(b"=").decode()
        TYM["vapid"] = {"private_key_pem": priv_pem, "public_key_b64": pub_b64}
        save_state()
        print("🔔 VAPID keys generadas para Web Push")
    except Exception as exc:
        print(f"⚠️  No se pudieron generar VAPID keys: {exc}")

def _send_venue_push(vid, title, body, url="/"):
    if not _WEBPUSH_OK:
        return
    vapid = TYM.get("vapid", {})
    priv_pem = vapid.get("private_key_pem")
    if not priv_pem:
        return
    subs = TYM.get("push_subs", {}).get(vid, [])
    dead = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body, "url": url,
                                 "icon": "/icon-192.png", "tag": "tym-poll"}),
                vapid_private_key=priv_pem,
                vapid_claims={"sub": "mailto:tym@example.com"},
            )
        except WebPushException as ex:
            if ex.response and ex.response.status_code in (404, 410):
                dead.append(sub)
        except Exception:
            pass
    if dead:
        TYM["push_subs"][vid] = [s for s in subs if s not in dead]

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
    "forgot":  (3,  300),
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
            "max_song_duration_min": 0,    # rechazar pedidos de canciones más largas que N min (0=off)
            "music_only": False,           # rechazar pedidos que la IA clasifique como no-música
            "dedica_moderation": False,    # moderar dedicatorias (mesa a mesa) antes de mostrarlas en TV (off=pasan directo)
            "song_message_moderation": False,  # moderar el mensaje al pedir canción (separado de dedica_moderation)
            "dedica_price": 0,             # cargo extra si el pedido incluye mensaje (0=gratis, se suma al precio de la canción)
            "fallback_shuffle": True,       # lista del local en orden aleatorio
            "theme": "azul",               # tema de color: azul | purpura | verde | rojo | dorado | rosa
            "blocked_keywords": [],        # palabras en título/artista que bloquean el pedido
            "allowed_keywords": [],        # si hay entradas, la canción DEBE tener al menos una
            "allow_skip_vote": False,      # permite que las mesas voten para saltar la canción
            "poll_duration_secs": 120,     # duración del timer de la votación (segundos)
            "duelo_duration_secs": 60,     # duración del timer del duelo (segundos)
            "schedule": [],                # [{from:"HH:MM", to:"HH:MM", genre:str, label:str}]
            "timezone": DEFAULT_TZ,         # zona horaria IANA del local (ej: "America/Bogota")
            "prepaid_mode": False,     # ON: saldo prepago por cliente en vez de cuenta de mesa
            "min_direct_pay": 700,     # piso para pago directo sin wallet (evita que la pasarela se coma el monto)
            "show_tym_brand": False,   # mostrar el logo/texto "TYM Music" en /tv (off por defecto, tema legal)
        },
        "tables": [{"name": f"Mesa {i}", "pin": str(i) * 4} for i in range(1, 6)],  # PINs 1111..5555
        "stations": [],   # ["Caja 1","Silla 2",...] — opcional, sin PIN; vacío = no aplica (modo prepago)
        "customers": {},  # celular normalizado -> {name, email, phone, balance, wallet_history}
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
        "tv_owner": None,          # {"id": device_id, "last_seen": ts} — dueño actual de la TV (anti 2 TVs a la vez)
        "qr_force_until": 0,       # timestamp: si es futuro, /tv fuerza el QR visible (botón "Mostrar QR ya")
        "dedicas": [],             # [{id, from_table, to_table, message, ts, shown_tv}]
        "bis_votes": {},           # {yt: set(tokens)} — votos de bis por canción
        "poll": None,              # {options, votes, active, created_at, ends_at, triggered_by_np_id, auto}
        "poll_launched_for_id": None,  # np.id para el que se lanzó/cerró el último poll
        "duelo": None,             # {teams:[{yt,title,artist,label},...], votes:{yt:set()}, active, ends_at, created_at}
        "announcements": [],       # [{id, text, color, created_at, active}]
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
        "bardemo": {"pass_hash": None, "venue": "bardemo", "email": "jhonyt37@gmail.com", "blocked": False},
        "lazona":  {"pass_hash": None, "venue": "lazona", "email": "jhonyt37@gmail.com", "blocked": False},
        "tym":     {"pass_hash": None, "venue": "*", "email": "jhonyt37@gmail.com", "blocked": False},
    },
    "events": [],              # analítica: {venue, table, account, ts, ev, ...}
    "accounts": {},            # token -> {id,venue,table,opened_at,closed_at,total,orders_free,orders_premium}
    "vapid": {},               # {private_key_pem, public_key_b64}
    "push_subs": {},           # venue_id -> [{endpoint, keys:{p256dh,auth}}]
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

def log_wallet_revenue(customer, sess, amount, kind, title, event_type):
    """Dinero real que ENTRA en modo prepago (recarga o pago directo). No toca STATE['ledger']
    (no es deuda de mesa) — pero sí TYM['events'], de donde sale toda la analítica de facturación.
    Vive en el registro DURABLE del cliente (por celular), no en la sesión anónima del navegador."""
    TYM["events"].append({"venue": CUR_VID, "table": sess["table"], "account": customer["phone"], "ts": time.time(),
                          "ev": "charge", "amount": amount, "kind": kind, "title": title, "via": event_type})
    hist = customer.setdefault("wallet_history", [])
    hist.insert(0, {"title": title, "amount": amount, "kind": kind, "ts": time.time(), "type": event_type})
    customer["wallet_history"] = hist[:20]
    _redis_last_save[0] = 0

def wallet_spend(customer, amount, kind, title):
    """Descuenta saldo ya recargado. No es ingreso nuevo (ya se contó al recargar)."""
    customer["balance"] = customer.get("balance", 0) - amount
    hist = customer.setdefault("wallet_history", [])
    hist.insert(0, {"title": title, "amount": -amount, "kind": kind, "ts": time.time(), "type": "spend"})
    customer["wallet_history"] = hist[:20]

def try_charge_prepaid(sess, amount, kind, title, pay_method, min_direct_pay):
    """Unico punto de gateo para buy/request/boost/jump en modo prepago.
    Devuelve (ok, via, error_dict); no muta nada si ok=False."""
    if amount <= 0:
        return True, None, None
    customer = get_customer(sess)
    if not customer:
        return False, None, {"error": "Este local cambió a modo prepago. Regístrate para continuar.",
                              "needs_registration": True}
    if pay_method == "direct":
        if amount < min_direct_pay:
            return False, None, {"error": f"Pago directo solo desde ${min_direct_pay}. Recarga tu saldo para este monto."}
        log_wallet_revenue(customer, sess, amount, kind, title, "direct")
        return True, "direct", None
    bal = customer.get("balance", 0)
    if bal < amount:
        return False, None, {"error": "Saldo insuficiente", "insufficient_balance": True,
                              "need": amount, "balance": bal, "min_direct_pay": min_direct_pay}
    wallet_spend(customer, amount, kind, title)
    return True, "wallet", None

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
        h = _venue_hour(e["ts"], e.get("venue"))
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
    owners_info = [{"username": k, "venue": v["venue"], "venue_name": VENUES[v["venue"]]["settings"]["venue_name"],
                     "blocked": v.get("blocked", False)}
                   for k, v in TYM["owners"].items() if v.get("venue") != "*" and v.get("venue") in VENUES]
    return {"facturacion_por_local": fact_local, "facturacion_total": total,
            "orders_free": free, "orders_premium": prem,
            "hora_pico_ingresos": hour_rev, "hora_pico_pedidos": hour_ord,
            "cuentas": accts, "venues": venues_info, "owners": owners_info}

def venue_analytics(vid, days=None):
    """Informe del local para el dueño: facturación, pedidos, canciones top, horas pico.
    days=None -> todo el historico; days=N -> solo eventos de los ultimos N dias
    (facturacion_total/canciones_top/horas pico quedan acotados al rango elegido)."""
    hour_rev = {k: 0 for k in range(24)}
    hour_ord = {k: 0 for k in range(24)}
    songs, total, week_total, free, prem = {}, 0, 0, 0, 0
    week_ago = time.time() - 7 * 86400
    range_since = (time.time() - days * 86400) if days else None
    for e in TYM["events"]:
        if e.get("venue") != vid:
            continue
        if range_since is not None and e.get("ts", 0) < range_since:
            continue
        h = _venue_hour(e["ts"], vid)
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

    # ---- Dashboard transaccional: resumen de HOY (medianoche hora del venue) ----
    v = VENUES.get(vid, {})
    venue_tz = _tz(v.get("settings", {}).get("timezone"))
    now_local = datetime.datetime.now(tz=venue_tz)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    ledger = v.get("ledger", [])
    tonight_entries = [l for l in ledger if l.get("ts", 0) >= midnight_local]
    tonight_total = sum(l.get("amount", 0) for l in tonight_entries)
    tonight_songs_priority = sum(1 for l in tonight_entries if l.get("kind") not in ("pase", ""))
    tonight_songs_free = sum(1 for e in TYM["events"]
                             if e.get("venue") == vid and e["ev"] == "order"
                             and not e.get("premium") and e.get("ts", 0) >= midnight_local)
    # Por mesa: total y canciones
    per_table: dict = {}
    for l in tonight_entries:
        tbl = l.get("table", "?")
        rec = per_table.setdefault(tbl, {"table": tbl, "total": 0, "songs": 0})
        rec["total"] += l.get("amount", 0)
        rec["songs"] += 1
    tonight_per_table = sorted(per_table.values(), key=lambda x: -x["total"])

    return {"facturacion_total": total, "facturacion_semana": week_total,
            "orders_free": free, "orders_premium": prem,
            "hora_pico_ingresos": hour_rev, "hora_pico_pedidos": hour_ord,
            "canciones_top": top_songs, "cuentas_recientes": recent_accts,
            "tonight_total": tonight_total,
            "tonight_songs_priority": tonight_songs_priority,
            "tonight_songs_free": tonight_songs_free,
            "tonight_per_table": tonight_per_table}

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

YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

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

# ---- Limpieza del titulo para MOSTRAR (distinto de _clean_for_lookup, mas abajo,
# que es agresivo porque solo alimenta una busqueda; aqui hay que conservar info util
# como "(Remix)"/"(Live)"/"(feat. X)" y solo quitar basura de subida tipo "Video Oficial"/"Lyrics"). ----
_JUNK_PHRASES = [
    "official music video", "official video", "official audio",
    "video oficial", "vídeo oficial", "audio oficial",
    "video lirico", "video lírico", "vídeo lírico", "lirico", "lírico",
    "lyric video", "lyrics video", "video lyrics", "lyrics", "letra oficial",
    "letra completa", "letra", "visualizer", "karaoke",
    "video clip oficial", "videoclip oficial", "videoclip",
]
_BRACKET_RE = re.compile(r"[\(\[\{]([^()\[\]{}]*)[\)\]\}]")
_JUNK_WORD_RE = re.compile(r"\b(hd|4k|hq|official|oficial)\b", re.IGNORECASE)
_JUNK_TRAILING_RE = re.compile(
    r"\s*[-|]\s*(?:" + "|".join(re.escape(p) for p in _JUNK_PHRASES) + r")\s*$",
    re.IGNORECASE)

def _clean_title_display(t):
    original = (t or "").strip()
    def strip_group(m):
        inner = m.group(1)
        low = inner.lower().strip()
        if any(p in low for p in _JUNK_PHRASES):
            return " "
        bare = _JUNK_WORD_RE.sub("", low)
        bare = re.sub(r"[\d\s]+", "", bare)
        if not bare:  # solo quedaban numeros/HD/4K/"official" sueltos, ej. "(HD)"/"(2023)"
            return " "
        return m.group(0)  # conserva contenido real: (Remix), (Live), (feat. X)...
    t = _BRACKET_RE.sub(strip_group, original)
    t = _JUNK_TRAILING_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" -|")
    return t if t else original

# ---- Cruce con iTunes para corregir el "artista" (hoy es el canal de YouTube, que
# a menudo es un canal de lyrics/mixes ajeno, ej. "XRangerFK" en vez de "Plan B") ----
_artist_cache = {}
ARTIST_TTL = 7 * 24 * 3600

def _norm_words(s):
    s = re.sub(r"[^a-z0-9áéíóúñ\s]", " ", (s or "").lower())
    return set(w for w in s.split() if len(w) > 2)

def _match_score(a, b):
    wa, wb = _norm_words(a), _norm_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(1, min(len(wa), len(wb)))

# Titulos de covers/instrumentales quedan CASI siempre repletos de las mismas
# palabras clave que el original (para SEO), asi que ganan el match por overlap
# de palabras aunque el artista sea un canal de karaoke/instrumentales ajeno
# (encontrado probando "BAD BUNNY x JHAY CORTEZ - DAKITI": el primer resultado
# que matcheaba por palabras era un instrumental de "Vox Freaks", no el original).
_COVER_MARKERS = (
    "instrumental", "karaoke", "tribute", "made famous by",
    "in the style of", "originally performed", "cover version",
)

def _lookup_real_artist(clean_title):
    if not clean_title:
        return None
    best, best_score = None, 0.0
    for r in _itunes_query(clean_title, "song"):
        track = r.get("trackName", "") or ""
        if any(m in track.lower() for m in _COVER_MARKERS):
            continue
        score = _match_score(clean_title, track)
        if score > best_score:
            best, best_score = r, score
    return best.get("artistName") if best and best_score >= 0.5 else None

def enrich_artists(items):
    """Reemplaza el 'artista' (canal de YouTube) por el artista real de iTunes cuando
    hay match confiable. Best-effort: en paralelo (cada request ya corre en su propio
    hilo, ThreadingHTTPServer) y cacheado por yt id, asi solo paga la latencia una vez."""
    now = time.time()
    todo = []
    for it in items:
        hit = _artist_cache.get(it["yt"])
        if hit and now - hit[0] < ARTIST_TTL:
            if hit[1]:
                it["artist"] = hit[1]
            continue
        todo.append(it)
    if not todo:
        return items
    ex = ThreadPoolExecutor(max_workers=6)
    try:
        futs = {ex.submit(_lookup_real_artist, _clean_for_lookup(it["title"])): it for it in todo}
        try:
            for fut in as_completed(futs, timeout=6):
                it = futs[fut]
                try:
                    artist = fut.result()
                except Exception:
                    artist = None
                _artist_cache[it["yt"]] = (now, artist)
                if artist:
                    it["artist"] = artist
        except _FuturesTimeoutError:
            pass  # se agoto el tiempo total; lo que no alcanzo a resolver se queda con el artista original (canal de YouTube)
    finally:
        # wait=False: si algun hilo quedo colgado en la request a iTunes, no bloquear
        # la respuesta esperandolo (with-block habria esperado a shutdown(wait=True)).
        ex.shutdown(wait=False)
    if len(_artist_cache) > 5000:
        stale = [k for k, v in _artist_cache.items() if now - v[0] > ARTIST_TTL]
        for k in stale:
            del _artist_cache[k]
    return items

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
                    title = _clean_title_display("".join(r.get("text", "") for r in v["title"]["runs"]))
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
    enrich_artists(res)
    _search_cache[key] = (now, res)
    return res

# ---- Genero real de la cancion sonando (iTunes Search API: gratis, sin API key) ----
# Se usa para sugerir "mas de este genero" con el genero REAL de la cancion que suena,
# en vez de depender del genero fijo configurado en el local (settings.genre).
_genre_cache = {}
GENRE_TTL = 6 * 3600

def _clean_for_lookup(t):
    """Quita '(Video Oficial)'/'[Lyrics]'/etc y todo tras 'ft./feat.' — sin esto iTunes
    no encuentra nada (probado: con parentesis da 0 resultados, limpio si matchea)."""
    t = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", t or "")
    t = re.sub(r"\b(ft|feat|featuring)\b.*$", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()

def _itunes_query(term, entity):
    if not term:
        return []
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": entity, "limit": 3, "country": "CO"})
    try:
        d = json.loads(urllib.request.urlopen(url, timeout=6).read())
        return d.get("results", [])
    except Exception:
        return []

# iTunes clasifica géneros con etiquetas propias que no siempre coinciden con el término
# coloquial que un dueño de bar en Colombia escribiría en el filtro (ej. "reggaeton" nunca
# aparece literal — iTunes usa "Urbano latino", confirmado en vivo probando Daddy Yankee, J
# Balvin, Karol G). Sin este mapeo, bloquear/permitir por género quedaría roto en la práctica
# para los términos más obvios que alguien realmente escribiría.
_GENRE_ALIASES = {
    "reggaeton": "urbano latino", "reggaetón": "urbano latino", "urbano": "urbano latino",
    "perreo": "urbano latino", "trap": "hip-hop/rap",
}

def _kw_matches(kw, haystack, genre):
    if kw in haystack:
        return True
    alias = _GENRE_ALIASES.get(kw)
    return bool(alias and genre and alias in genre)

def itunes_genre(artist, title):
    artist = (artist or "").strip()[:80]
    title = (title or "").strip()[:150]
    if not artist and not title:
        return None
    key = (artist.lower(), title.lower())
    now = time.time()
    hit = _genre_cache.get(key)
    if hit and now - hit[0] < GENRE_TTL:
        return hit[1]
    genre = None
    clean_title = _clean_for_lookup(title)
    results = _itunes_query(f"{artist} {clean_title}".strip(), "song")
    al = artist.lower()
    match = next((r for r in results if al and (al in (r.get("artistName") or "").lower()
                  or (r.get("artistName") or "").lower() in al)), None)
    genre = (match or (results[0] if results else {})).get("primaryGenreName")
    if not genre and artist:
        results = _itunes_query(artist, "musicArtist")
        if results:
            genre = results[0].get("primaryGenreName")
    _genre_cache[key] = (now, genre)
    if len(_genre_cache) > 3000:
        stale = [k for k, v in _genre_cache.items() if now - v[0] > GENRE_TTL]
        for k in stale:
            del _genre_cache[k]
    return genre

# ---- Moderación de dedicatorias — heurística de palabras clave (sin IA, sin costo, sin
# dependencias externas). Menos precisa que un modelo de lenguaje (no entiende contexto,
# sarcasmo, ni faltas de ortografía intencionales), pero es gratis, instantánea, y no depende
# de un servicio externo. Cualquier coincidencia deja el mensaje pendiente de revisión manual
# del admin — el heurístico nunca rechaza automáticamente, solo decide qué necesita ojo humano.
_DEDICA_BLOCKLIST = [
    "puta", "puto", "hijueputa", "gonorrea", "malparid", "maric", "pendej", "cabron", "cabrón",
    "perra", "zorra", "imbecil", "imbécil", "estupid", "estúpid", "idiota", "verga", "coño",
    "mierda", "culiad", "gilipollas", "bastard",
    "fuck", "bitch", "asshole", "whore", "slut",
    "sexo", "porno", "xxx", "onlyfans", "nudes", "desnud",
    "te voy a matar", "te mato",
    "whatsapp", "wasap", "sigueme", "síguem",
]
_PHONE_RE = re.compile(r"\d{7,}")
_URL_RE = re.compile(r"https?://|www\.")

def _moderate_message(text):
    """Heurística de palabras clave para dedicatorias — reemplaza la moderación por IA de una
    sesión anterior (tenía costo por mensaje y quedaba inservible sin API key configurada).
    Se llama dentro del LOCK: es una comparación de texto en memoria, no I/O de red, así que
    no hace falta resolverla antes de tomar el lock como sí hacía falta con la llamada a IA."""
    t = text.lower()
    hit = next((w for w in _DEDICA_BLOCKLIST if w in t), None)
    if hit:
        return False, "Contiene una palabra o frase marcada para revisión."
    if _PHONE_RE.search(text):
        return False, "Parece contener un número de teléfono — revisar por spam."
    if _URL_RE.search(t):
        return False, "Parece contener un link — revisar por spam."
    return True, ""

# ---- "Solo música" — heurística de palabras clave + duración (sin IA, sin costo). Bloquea los
# casos obvios de contenido no-musical (podcasts, tutoriales, gameplay, etc.); no entiende
# títulos ambiguos o en otros idiomas tan bien como un modelo de lenguaje, pero es gratis e
# instantánea — no hace falta resolverla antes del LOCK, es una comparación en memoria.
_NOT_MUSIC_KEYWORDS = [
    "podcast", "tutorial", "vlog", "gameplay", "let's play", "lets play", "walkthrough",
    "reaction", "unboxing", "noticias", "entrevista", "documental", "documentary",
    "receta", "cómo hacer", "como hacer", "curso de", "clase de", "review", "reseña",
    "trailer", "full episode", "capítulo completo", "capitulo completo", "resumen",
    "highlights", "compilation", "asmr", "meditación", "meditacion", "audiolibro",
    "conferencia", "webinar", "sermón", "sermon", "predica",
]
_MUSIC_MAX_DURATION_SECS = 20 * 60  # 20 min — por encima y sin más indicios, sospechoso

def is_music_content(title, artist, duration):
    haystack = f"{title} {artist or ''}".lower()
    if any(kw in haystack for kw in _NOT_MUSIC_KEYWORDS):
        return False
    if duration and duration > _MUSIC_MAX_DURATION_SECS:
        return False
    return True

def get_session(token):
    return STATE["sessions"].get(token) if token else None

def active_persons_count():
    """Cuenta MESAS DISTINTAS con sesión activa en las últimas 2h. Usado para el umbral de
    skip-vote; debe ser la única fuente de verdad para que el número mostrado al cliente
    coincida con el que realmente decide el salto. Antes contaba cada sesión/token sin
    deduplicar por mesa: volver a entrar el PIN (batería, refresh, cambio de dispositivo —
    incluyendo el bug del gate trabado corregido esta sesión) inflaba el conteo sin límite,
    un bar de 13 mesas llegó a mostrar 49 "personas activas" — bug reportado en vivo. Ahora
    coincide con "active_sessions" (mismo cálculo, una sola fuente de verdad)."""
    now = time.time()
    return len({se["table"] for se in STATE.get("sessions", {}).values()
                if now - se.get("created", 0) < 7200})

def get_customer(sess):
    phone = sess and sess.get("phone")
    return STATE["customers"].get(phone) if phone else None

def session_public(sess):
    """Vista de la sesión que viaja al cliente. 'registered' indica si tiene celular
    asociado (modo prepago) — el cliente lo usa para saber si debe mostrar el gate de
    registro en vez de asumir que cualquier sesión existente ya puede pagar."""
    return {"table": sess["table"], "credits": sess["credits"], "pass_until": sess["pass_until"],
            "registered": bool(sess.get("phone"))}

def customer_label(name, station):
    return f"{name} · {station}" if station else name

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

# ---- Anti-abuso de prioridad ----
# Mesas impulsivas que compran prioridad/salto al #1 una y otra vez acaparan la cola y
# aburren a las demás mesas. En vez de bloquearlas (frustra a quien sí quiere pagar más),
# cada compra pagada (salto o prioridad de un solo uso) de la MISMA mesa dentro de la
# última hora encarece la siguiente: 1x la primera, 2x la segunda, 3x de ahí en adelante.
# Pase de tiempo y créditos ya prepagados no cuentan — solo compras con cobro nuevo.
PRIORITY_ABUSE_WINDOW_SECS = 3600

def priority_abuse_multiplier(table, now):
    hist = STATE.setdefault("priority_abuse", {})
    times = [t for t in hist.get(table, []) if now - t < PRIORITY_ABUSE_WINDOW_SECS]
    hist[table] = times
    n = len(times)
    return 1 if n == 0 else (2 if n == 1 else 3)

def record_priority_purchase(table, now):
    hist = STATE.setdefault("priority_abuse", {})
    hist.setdefault(table, []).append(now)

# ---- Gate + duración dinámica para votación/duelo (llamar bajo LOCK) ----
# El ganador siempre se marca "super" para sonar inmediatamente después de la actual (ver
# _close_poll_winner/_close_duelo_winner) — por eso el lanzamiento se bloquea si la canción
# ya va en >=50% (played_enough, ya trackeado por /api/progress) o si el siguiente puesto de
# la cola ya es un salto pagado (nadie puede colarse antes de quien pagó por sonar de primera).
POLL_CLOSE_BUFFER_SECS = 8
POLL_MIN_DURATION_SECS = 20

def _poll_gate_error():
    np = STATE.get("now_playing")
    if not np:
        return "No hay ninguna canción sonando ahora mismo."
    # Calculado directo del ratio posición/duración (no del flag played_enough — ese es
    # de una sola vía por diseño, para otro propósito, y en teoría podría quedar "pegado"
    # en True si la posición bajara; en uso real la posición del video solo avanza, pero
    # calcularlo directo evita depender de ese acoplamiento).
    dur = np.get("duration") or DEFAULT_DUR
    pos = np.get("position") or 0
    if dur > 0 and pos / dur >= 0.5:
        return "Esta canción ya va en más de la mitad — espera a la siguiente para lanzar esto."
    q = queue_view()
    if q and q[0].get("super"):
        return "La siguiente canción ya fue pagada para sonar de primera — espera a que termine."
    return None

def _poll_dynamic_duration():
    np = STATE["now_playing"]
    dur = np.get("duration") or DEFAULT_DUR
    pos = np.get("position") or 0
    remaining = dur - pos
    return max(POLL_MIN_DURATION_SECS, int(remaining - POLL_CLOSE_BUFFER_SECS))

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

def _close_poll_winner(p, vid=None):
    """Cierra el poll y encola el ganador. Llamar bajo LOCK con STATE apuntando al venue correcto."""
    p["active"] = False
    votes = p.get("votes", {})
    if not votes:
        return
    max_v = max(len(v) for v in votes.values())
    if max_v == 0:
        return
    winners = [yt for yt, v in votes.items() if len(v) == max_v]
    winner_yt = random.choice(winners)
    winner_opt = next((o for o in p.get("options", []) if o["yt"] == winner_yt), None)
    if not winner_opt:
        return
    poll_item = {"id": nid(), "title": winner_opt["title"],
                 "artist": winner_opt.get("artist", ""), "yt": winner_yt,
                 "token": None, "table": "Votación 🗳️", "priority": False,
                 # super=True: suena inmediatamente después de la actual, para no perder
                 # el hype de quien votó — ya se garantizó al lanzar que no hay un salto
                 # pagado esperando en el siguiente puesto (ver _poll_gate_error).
                 "super": True, "mode": "normal", "duration": DEFAULT_DUR,
                 "status": "approved", "play_status": "pending", "played_enough": False,
                 "requeue_count": 0, "ts": time.time(), "charge_on_play": 0,
                 "charged": False, "charge_kind": "", "repeat_exception": True, "message": ""}
    STATE["items"].append(poll_item)
    STATE.setdefault("repeat_exceptions", set()).add(winner_yt)
    if STATE.get("now_playing") is None:
        promote_next()

def _close_duelo_winner(d):
    """Cierra el duelo y encola el ganador. Llamar bajo LOCK."""
    d["active"] = False
    votes = d.get("votes", {})
    if not votes:
        return
    max_v = max(len(v) for v in votes.values())
    if max_v == 0:
        return
    winners = [yt for yt, v in votes.items() if len(v) == max_v]
    winner_yt = random.choice(winners)
    winner_team = next((t for t in d.get("teams", []) if t["yt"] == winner_yt), None)
    if not winner_team:
        return
    d["winner_yt"] = winner_yt
    item = {"id": nid(), "title": winner_team["title"],
            "artist": winner_team.get("artist", ""), "yt": winner_yt,
            "token": None, "table": f"⚔️ Duelo · {winner_team.get('label','Ganador')}",
            # super=True: mismo motivo que en _close_poll_winner — suena de primera.
            "priority": False, "super": True, "mode": "normal", "duration": DEFAULT_DUR,
            "status": "approved", "play_status": "pending", "played_enough": False,
            "requeue_count": 0, "ts": time.time(), "charge_on_play": 0,
            "charged": False, "charge_kind": "", "repeat_exception": True, "message": ""}
    STATE["items"].append(item)
    STATE.setdefault("repeat_exceptions", set()).add(winner_yt)
    if STATE.get("now_playing") is None:
        promote_next()

def _auto_create_poll_bg(vid, np_id, np_yt, np_artist, history_artists, genre, played_yts):
    """Corre en hilo background: busca candidatos y crea el poll bajo LOCK."""
    candidates = []
    seen = set(played_yts)
    artists = [a for a in ([np_artist] + history_artists) if a]
    artists = list(dict.fromkeys(artists))[:4]  # únicos, orden de aparición
    for artist in artists:
        try:
            results = yt_search(artist, 6)
            for r in results:
                if r["yt"] not in seen:
                    candidates.append(r)
                    seen.add(r["yt"])
                if len(candidates) >= 9:
                    break
        except Exception:
            pass
        if len(candidates) >= 9:
            break
    # Fallback: género del local
    if len(candidates) < 2:
        try:
            for r in yt_search(f"mejores {genre}", 9):
                if r["yt"] not in seen:
                    candidates.append(r)
                    seen.add(r["yt"])
        except Exception:
            pass
    if len(candidates) < 2:
        return
    selected = random.sample(candidates[:9], min(3, len(candidates)))
    duration = 120
    with LOCK:
        v = VENUES.get(vid)
        if not v:
            return
        # Re-verificar condiciones bajo lock
        if v.get("poll_launched_for_id") == np_id:
            return
        if v.get("poll", {}) and v["poll"].get("active"):
            return
        duration = int(v["settings"].get("poll_duration_secs", 120))
        now = time.time()
        v["poll"] = {
            "options": [{"yt": s["yt"], "title": s["title"], "artist": s.get("artist", "")} for s in selected],
            "votes": {s["yt"]: set() for s in selected},
            "active": True,
            "created_at": now,
            "ends_at": now + duration,
            "auto": True,
            "triggered_by_np_id": np_id,
        }
        v["poll_launched_for_id"] = np_id
        venue_name = v["settings"].get("venue_name", "TYM Music")
        opts_titles = [s["title"] for s in selected[:3]]

    # Enviar push fuera del lock
    try:
        body = "Elige entre: " + ", ".join(opts_titles)
        _send_venue_push(vid, f"🗳️ ¡Nueva votación en {venue_name}!", body)
    except Exception:
        pass

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
            "ts": it.get("ts"), "played_at": it.get("played_at"),
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
                  "message": (np.get("message") or "") if np.get("message_status", "approved") == "approved" else "",
                  "ts": np.get("ts"), "played_at": np.get("played_at"),
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
    top_loved = sorted(agg.values(), key=lambda x: -x["total"])[:10]
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
        if not ded.get("shown_tv") and ded.get("status") == "approved":
            dedica_pending = ded
            break

    # ---- Bis votes ----
    bis_votes_pub = {yt: len(v) for yt, v in STATE.get("bis_votes", {}).items()}
    my_bis_votes = []
    if token:
        for yt, voters in STATE.get("bis_votes", {}).items():
            if token in voters:
                my_bis_votes.append(yt)

    # ---- Duelo: auto-cerrar si expiró ----
    _d = STATE.get("duelo")
    if _d and _d.get("active") and _d.get("ends_at") and time.time() >= _d["ends_at"]:
        _close_duelo_winner(_d)

    # ---- Poll: auto-cerrar si expiró ----
    _p = STATE.get("poll")
    if _p and _p.get("active") and _p.get("ends_at") and time.time() >= _p["ends_at"]:
        _close_poll_winner(_p)

    # ---- Poll: auto-lanzar si condiciones se cumplen ----
    _p = STATE.get("poll")
    if np and not np.get("fallback"):
        _queue_len = len(q)
        _no_active_poll = not (_p and _p.get("active"))
        _not_launched = STATE.get("poll_launched_for_id") != np["id"]
        if _queue_len <= 2 and _no_active_poll and _not_launched:
            STATE["poll_launched_for_id"] = np["id"]  # reservar para evitar doble disparo
            _hist_artists = [h.get("artist", "") for h in STATE.get("history", [])[:5]]
            _played = {np["yt"]} | {h["yt"] for h in STATE.get("history", [])} | {i["yt"] for i in STATE.get("items", [])}
            _vid = CUR_VID
            threading.Thread(
                target=_auto_create_poll_bg,
                args=(_vid, np["id"], np["yt"], np.get("artist", ""), _hist_artists,
                      _scheduled_genre(), _played),
                daemon=True
            ).start()

    # ---- Poll (estado público) ----
    _p = STATE.get("poll")
    poll_state = None
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
            "ends_at": _p.get("ends_at"),
            "auto": _p.get("auto", False),
        }

    # ---- Duelo (estado público) ----
    _d = STATE.get("duelo")
    duelo_state = None
    if _d:
        my_duelo_vote = None
        if token:
            for _dyt, _dv in _d.get("votes", {}).items():
                if token in _dv:
                    my_duelo_vote = _dyt
                    break
        duelo_state = {
            "teams": _d.get("teams", []),
            "votes": {_yt: len(_v) for _yt, _v in _d.get("votes", {}).items()},
            "active": _d.get("active", False),
            "ends_at": _d.get("ends_at"),
            "winner_yt": _d.get("winner_yt"),
            "my_vote": my_duelo_vote,
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

    _active_genre = _scheduled_genre()
    _active_slot = next((sl for sl in s.get("schedule", []) if sl.get("genre") == _active_genre
                         and s.get("schedule")), None)

    # ---- Por qué no se puede saltar al #1 ahora — se expone SIEMPRE el motivo (no solo un
    # booleano) para que el cliente muestre el botón deshabilitado con una explicación en vez
    # de ocultarlo sin más: evita que el cliente le pregunte al mesero por qué no aparece,
    # reduciendo la carga operativa del admin del bar.
    if not np:
        jump_reason = "Aún no hay una canción sonando."
    elif (poll_state and poll_state.get("active")) or (duelo_state and duelo_state.get("active")):
        jump_reason = "Hay una votación o duelo en curso — espera a que termine."
    elif STATE.get("jump_used_for") == np["id"]:
        jump_reason = "Ya se usó el salto para esta canción — disponible con la siguiente."
    else:
        jump_reason = None

    out = {
        "settings": dict({k: s.get(k) for k in ("venue_name", "price_priority", "style", "auto_approve",
                                       "genre", "credit_packages", "time_pass",
                                       "repeat_block_min", "repeat_block_songs", "trim_end_secs",
                                       "free_per_window", "free_window_min", "jump_multiplier",
                                       "venue_logo", "max_priority_queue_min", "max_song_duration_min", "fallback_shuffle",
                                       "theme", "blocked_keywords", "allowed_keywords",
                                       "allow_skip_vote", "poll_duration_secs", "duelo_duration_secs",
                                       "schedule", "timezone", "prepaid_mode", "min_direct_pay",
                                       "show_tym_brand", "music_only", "dedica_moderation",
                                       "song_message_moderation", "dedica_price")},
                                       socials=TYM["socials"], tym_logo=TYM["tym_logo"]),
        "active_genre": _active_genre,
        "active_slot": _active_slot,
        "announcements": [a for a in STATE.get("announcements", [])
                          if a.get("active") and time.time() < a.get("expires_at", float("inf"))],
        "tables": [t["name"] for t in STATE["tables"]],
        "stations": STATE.get("stations", []),
        "now_playing": np_pub,
        "jump_available": jump_reason is None,
        "jump_unavailable_reason": jump_reason,
        "jump_price": s["price_priority"] * s.get("jump_multiplier", 3),
        "priority_queue_min": int(sum(i.get("duration", DEFAULT_DUR)
                                      for i in queue_view() if i.get("priority")) // 60),
        "tv_active": (time.time() - STATE.get("tv_lastseen", 0)) < 15,
        "qr_force": time.time() < STATE.get("qr_force_until", 0),
        "active_sessions": active_persons_count(),
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
        "duelo": duelo_state,
        "vibe": vibe_state,
        "skip_votes_count": len(STATE.get("skip_votes", set())),
        "skip_threshold": max(2, -(-active_persons_count() // 2)),
        "my_skip_vote": bool(token and token in STATE.get("skip_votes", set())),
        "session": (session_public(sess) if sess else None),
    }
    if sess:
        tbl = sess["table"]
        mine = [l for l in STATE["ledger"] if l["table"] == tbl]
        out["my_tab"] = list(reversed(mine))[:20]
        out["my_tab_total"] = sum(l["amount"] for l in mine)
        if STATE["settings"].get("prepaid_mode"):
            customer = get_customer(sess)
            out["wallet_balance"] = customer.get("balance", 0) if customer else 0
            out["wallet_history"] = customer.get("wallet_history", [])[:20] if customer else []
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
                           "artist": h.get("artist", ""),
                           "ts": h.get("ts"), "played_at": h.get("played_at")} for h in STATE["history"][:10]]
        out["repeat_exceptions"] = list(STATE.get("repeat_exceptions", set()))
        out["all_dedicas"] = list(reversed(STATE.get("dedicas", [])))[:30]
        out["all_announcements"] = list(reversed(STATE.get("announcements", [])))
        _msg_pool = (([np] if np else []) + STATE["items"])
        out["pending_song_messages"] = [
            {"id": it["id"], "title": it["title"], "table": it.get("table", ""),
             "message": it.get("message", ""), "reason": it.get("message_mod_reason", "")}
            for it in _msg_pool if it.get("message_status") == "pending"
        ]
        out["customers"] = sorted(STATE["customers"].values(), key=lambda c: -c.get("balance", 0))[:100]
        own = next((o for o in TYM["owners"].values() if o.get("venue") == CUR_VID), None)
        out["owner_email"] = (own or {}).get("email", "")
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

def recommendations_snapshot():
    """Bajo LOCK: solo lee STATE, sin I/O de red (rápido) — el resto sigue en
    recommendations_finish(), que corre FUERA del lock global para no congelar
    a los demás locales mientras se espera la respuesta de YouTube."""
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
    top_artists = list({c["artist"] for c in counts if c.get("artist")})[:2]
    return mas, local, populares, seen, top_artists, s["genre"]

def recommendations_finish(mas, local, populares, seen, top_artists, genre):
    """Fuera del LOCK: llamadas de red a YouTube (lentas)."""
    # Complementar con artistas más pedidos si hay pocas canciones en historial
    if len(populares) < 6:
        for art in top_artists:
            for r in yt_search(art, 5):
                if r["yt"] not in seen:
                    seen.add(r["yt"])
                    populares.append({"yt": r["yt"], "title": r["title"], "artist": r["artist"]})
    if not populares:
        populares = [{"yt": r["yt"], "title": r["title"], "artist": r["artist"]} for r in yt_search("musica popular colombia", 12)]
    genero = [{"yt": r["yt"], "title": r["title"], "artist": r["artist"]} for r in yt_search(f"los mejores {genre}", 12)]
    return {"mas_pedido": _pad(mas), "del_local": local,
            "populares": _pad(populares), "genero": _pad(genero, genre=genre)}

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
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
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
        if path == "/api/qr":
            vid = self.resolve_vid()
            base = PUBLIC_URL.rstrip("/") if PUBLIC_URL else f"http://{lan_ip()}:{PORT}"
            url = f"{base}/?v={vid}"
            try:
                import qrcode, io
                buf = io.BytesIO()
                qrcode.make(url).save(buf, "PNG")
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                # NUNCA cachear: la venue se resuelve del lado del servidor (cookie de sesión
                # o ?v=), no viaja siempre en la URL — con /tv abierto sin "?v=" (el caso
                # normal, solo con login) la URL de esta petición es literalmente "/api/qr"
                # para CUALQUIER local. Con cache habilitado, el navegador de un dispositivo
                # ya usado antes con otro local servía el PNG viejo cacheado sin ni siquiera
                # volver a preguntarle al servidor — bug grave reportado en vivo: mostraba el
                # QR de un local distinto, enviando clientes/pagos al local equivocado.
                self.send_header("Cache-Control", "no-store")
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
        if path == "/api/genre":
            ip = self.client_address[0]
            if not _rate_ok(ip, "search"):
                return self._send(429, {"error": "Demasiadas búsquedas. Espera un momento."})
            genre = itunes_genre(self._q("artist"), self._q("title"))
            return self._send(200, {"genre": genre})
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
                snap = recommendations_snapshot()
            return self._send(200, recommendations_finish(*snap))
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
                # Mismo dato que ve el cliente al entrar el PIN ("cuenta pendiente en esta
                # mesa") — se expone aquí también para que el admin lo vea sin depender de que
                # el cliente se lo cuente. active_now: alguien con sesión viva en esa mesa
                # ahora mismo (mismo criterio de "activo" que active_persons_count()).
                now = time.time()
                out = []
                for t in STATE["tables"]:
                    tab_total = sum(l["amount"] for l in STATE["ledger"] if l["table"] == t["name"])
                    active_now = any(se.get("table") == t["name"] and now - se.get("created", 0) < 7200
                                     for se in STATE.get("sessions", {}).values())
                    out.append({**t, "tab_total": tab_total, "active_now": active_now})
                return self._send(200, out)
        if path == "/api/admin/analytics":
            av = self.authed_venue()
            if not av or av not in VENUES:
                return self._send(401, {"error": "no auth"})
            days_q = self._q("days")
            days = int(days_q) if days_q.isdigit() else None
            with LOCK:
                return self._send(200, venue_analytics(av, days))
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
        if path == "/api/push/vapid-public-key":
            pub = TYM.get("vapid", {}).get("public_key_b64", "")
            return self._send(200, {"public_key": pub})

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
            pwd = d.get("pass") or ""
            ok, needs_rehash = verify_password(pwd, o.get("pass_hash") if o else _DUMMY_PASS_HASH)
            if not o or not ok:
                return self._send(401, {"error": "Usuario o contraseña incorrectos"})
            if o.get("blocked"):
                return self._send(403, {"error": "Esta cuenta está bloqueada. Contacta a TYM."})
            if needs_rehash:
                o["pass_hash"] = hash_password(pwd)
                save_state()
            tk = gen_token(); AUTH[tk] = o["venue"]
            body = json.dumps({"ok": True, "venue": o["venue"]}).encode("utf-8")
            secure_flag = "; Secure" if PUBLIC_URL.startswith("https://") else ""
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"tymauth={tk}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400{secure_flag}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/api/logout":
            AUTH.pop(self.get_cookie("tymauth"), None)
            return self._send(200, {"ok": True})
        if path == "/api/forgot_password":
            ip = self.client_address[0]
            if not _rate_ok(ip, "forgot"):
                return self._send(429, {"error": "Demasiados intentos. Espera unos minutos."})
            u = (d.get("user") or "").strip()
            generic_msg = "Si el usuario existe, en unos minutos llegará un correo con instrucciones."
            email_hint = None
            with LOCK:
                o = TYM["owners"].get(u)
                if o and not o.get("blocked") and o.get("email"):
                    email_hint = mask_email(o["email"])
                    new_pass = secrets.token_urlsafe(9)
                    o["pass_hash"] = hash_password(new_pass)
                    save_state()
                    send_email(o["email"], "TYM Music — tu nueva contraseña",
                               f"Hola,\n\nSe generó una nueva contraseña para el usuario \"{u}\":\n\n"
                               f"{new_pass}\n\nInicia sesión con esta clave y cámbiala desde el panel "
                               f"(Ajustes → Cuenta y seguridad) apenas puedas.\n\n— TYM Music")
            # Mismo mensaje exista o no el usuario — evita revelar qué cuentas existen.
            # email_hint solo viaja si sí existe y tiene correo; ayuda al dueño a confirmar a
            # cuál dirección le llegó sin exponerla completa.
            return self._send(200, {"ok": True, "message": generic_msg, "email_hint": email_hint})

        # ---- Endpoints que requieren dueño logueado ----
        ADMIN_PATHS = ("/api/advance", "/api/progress", "/api/admin/approve", "/api/admin/reject",
                       "/api/admin/remove", "/api/admin/settings", "/api/admin/tables", "/api/admin/stations",
                       "/api/admin/curated", "/api/admin/close_table", "/api/admin/reset",
                       "/api/admin/add", "/api/admin/allow_repeat", "/api/admin/move", "/api/admin/reorder",
                       "/api/admin/poll", "/api/admin/poll/close", "/api/admin/vibe/reset",
                       "/api/admin/duelo", "/api/admin/duelo/close",
                       "/api/admin/announcement", "/api/admin/show_qr",
                       "/api/admin/dedica/delete", "/api/admin/dedica/approve", "/api/admin/dedica/reject",
                       "/api/admin/song_message/approve", "/api/admin/song_message/reject",
                       "/api/admin/change_password", "/api/admin/update_email")
        vid = self.resolve_vid(d)
        if path in ADMIN_PATHS:
            av = self.authed_venue()
            if not av or av not in VENUES:
                return self._send(401, {"error": "Inicia sesión"})
            vid = av
        # Resolver el link de YouTube (llamada de red bloqueante) ANTES de tomar el LOCK
        # global: si no, un oembed lento congela TODOS los locales mientras dura la
        # petición. Requiere un token de sesión ya conocido (igual que antes) para no
        # regalar la llamada de red a quien mande peticiones sin sesión válida.
        if path == "/api/request" and not d.get("yt") and d.get("link") and d.get("token") in TOKENS:
            pre_yt = yt_id(d["link"])
            if pre_yt:
                pre_title, pre_artist = yt_title(pre_yt)
                d = dict(d); d["yt"] = pre_yt; d["title"] = pre_title; d["artist"] = pre_artist or ""
        # Filtro de contenido por género: si el local configuró palabras bloqueadas/permitidas,
        # resolver el género real (iTunes, llamada de red) ANTES del LOCK — mismo motivo que la
        # resolución de link de arriba. Antes el filtro solo comparaba título+artista como texto
        # plano, así que bloquear "reggaeton" como palabra no frenaba canciones de ese género
        # cuyo título/artista no contuviera esa palabra literal (ej. "Bad Bunny - Dákiti") — bug
        # reportado en vivo. Solo se hace la llamada si el local realmente tiene el filtro
        # activo, para no pagar latencia de red en cada pedido de locales sin filtro.
        if path == "/api/request":
            _fv = VENUES.get(vid)
            if _fv and (_fv["settings"].get("blocked_keywords") or _fv["settings"].get("allowed_keywords")):
                _ftitle, _fartist = d.get("title") or "", d.get("artist") or ""
                if _ftitle or _fartist:
                    try:
                        d = dict(d); d["_genre"] = itunes_genre(_fartist, _ftitle) or ""
                    except Exception:
                        pass
        # "Solo música" y moderación de dedicatorias ahora son heurísticas en memoria (sin IA,
        # sin costo) — ver is_music_content()/_moderate_message() más arriba. Al no ser I/O de
        # red, ya no hace falta resolverlas antes del LOCK; se llaman directo dentro del lock,
        # junto a las validaciones de /api/request y /api/dedica.
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
                STATE["sessions"][token] = {"table": table, "credits": 0, "pass_until": 0,
                                            "created": time.time(), "phone": None}
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
                                        "session": session_public(STATE["sessions"][token])})

            # ---- Sesion anonima (modo prepago): deja navegar/pedir gratis sin formulario.
            # El registro por celular (nombre+telefono) solo se pide despues, en el momento
            # de pagar algo (try_charge_prepaid -> needs_registration), no de entrada. ----
            if path == "/api/anon_session":
                if not _rate_ok(self.client_address[0], "session"):
                    return self._send(429, {"error": "Demasiados intentos. Espera un momento."})
                if not STATE["settings"].get("prepaid_mode"):
                    return self._send(400, {"error": "Este local no usa registro por celular."})
                token = gen_token()
                STATE["sessions"][token] = {"table": "Invitado", "credits": 0, "pass_until": 0,
                                            "created": time.time(), "phone": None}
                TOKENS[token] = CUR_VID
                open_account(token, CUR_VID, "Invitado")
                return self._send(200, {"ok": True, "token": token,
                                        "session": session_public(STATE["sessions"][token])})

            # ---- Registro por celular (reemplaza el PIN en modo prepago) ----
            if path == "/api/register":
                if not _rate_ok(self.client_address[0], "session"):
                    return self._send(429, {"error": "Demasiados intentos. Espera un momento."})
                if not STATE["settings"].get("prepaid_mode"):
                    return self._send(400, {"error": "Este local no usa registro por celular."})
                phone = re.sub(r"\D", "", d.get("phone") or "")
                name = str(d.get("name") or "").strip()[:40]
                email = str(d.get("email") or "").strip()[:120]
                station = str(d.get("station") or "").strip()[:40]
                if len(phone) < 7 or not name:
                    return self._send(400, {"error": "Ingresa tu nombre y un celular válido."})
                stations = STATE.get("stations", [])
                if stations and station not in stations:
                    return self._send(400, {"error": "Selecciona una estación válida."})
                existing = STATE["customers"].get(phone)
                recovered = bool(existing)
                if not existing:
                    STATE["customers"][phone] = {"name": name, "email": email, "phone": phone,
                                                  "balance": 0, "wallet_history": []}
                customer = STATE["customers"][phone]
                token = gen_token()
                label = customer_label(name, station)
                STATE["sessions"][token] = {"table": label, "credits": 0, "pass_until": 0,
                                            "created": time.time(), "phone": phone}
                TOKENS[token] = CUR_VID
                open_account(token, CUR_VID, label)
                return self._send(200, {"ok": True, "token": token, "recovered": recovered,
                                        "balance": customer["balance"],
                                        "wallet_history": customer["wallet_history"][:20],
                                        "session": session_public(STATE["sessions"][token])})

            # ---- Recargar saldo (modo prepago; simulado esta fase) ----
            if path == "/api/wallet/topup":
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Sesión no válida. Regístrate para continuar."})
                if not STATE["settings"].get("prepaid_mode"):
                    return self._send(400, {"error": "Esta función no está disponible en este local."})
                customer = get_customer(sess)
                if not customer:
                    return self._send(400, {"error": "Este local cambió a modo prepago. Regístrate para continuar.",
                                            "needs_registration": True})
                try:
                    amount = int(d.get("amount") or 0)
                except Exception:
                    amount = 0
                if amount < 100 or amount > 500000:
                    return self._send(400, {"error": "Monto inválido"})
                customer["balance"] = customer.get("balance", 0) + amount
                log_wallet_revenue(customer, sess, amount, "recarga",
                                    "Recarga de saldo (simulado · Wompi)", "topup")
                return self._send(200, {"ok": True, "balance": customer["balance"],
                                        "wallet_history": customer["wallet_history"][:20]})

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
                    price, kind, title = pkg["price"], "paquete", f"Paquete {pkg['qty']} prioridad(es)"
                elif d.get("kind") == "pass":
                    tp = s["time_pass"]
                    price, kind, title = tp["price"], "pase", f"Pase {tp['minutes']} min"
                else:
                    return self._send(400, {"error": "Tipo inválido"})
                if s.get("prepaid_mode"):
                    ok, via, err = try_charge_prepaid(sess, price, kind, title,
                                                       d.get("pay_method", "wallet"), s.get("min_direct_pay", 700))
                    if not ok:
                        return self._send(400, err)
                else:
                    log_charge(sess["table"], d.get("token"), price, kind, title)
                if d.get("kind") == "credits":
                    sess["credits"] += pkg["qty"]
                else:
                    sess["pass_until"] = max(time.time(), sess["pass_until"]) + tp["minutes"] * 60
                return self._send(200, {"ok": True, "session": session_public(sess)})

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
                dur = _parse_len(d.get("length")) or int(d.get("duration") or 0) or DEFAULT_DUR
                _max_dur_min = int(STATE["settings"].get("max_song_duration_min", 0) or 0)
                if _max_dur_min > 0 and dur > _max_dur_min * 60:
                    return self._send(400, {"error": f"Esta canción dura más de {_max_dur_min} min, el máximo permitido en este local 🎵",
                                            "too_long": True})
                # yt/title/artist ya vienen resueltos si el pedido fue por link (se resolvió
                # antes de tomar el LOCK — ver do_POST, para no bloquear el servidor con I/O de red)
                yt = d.get("yt")
                if not yt or not YT_ID_RE.match(yt):
                    return self._send(400, {"error": "No pude leer el link de YouTube"})
                title = str(d.get("title") or "Canción")[:200]
                artist = str(d.get("artist") or "")[:120]
                req_msg = (d.get("message") or "").strip()[:80]
                now = time.time()
                # "Solo música" (settings.music_only): heurística de palabras clave + duración.
                if STATE["settings"].get("music_only") and not is_music_content(title, artist, dur):
                    return self._send(400, {"error": "Esto no parece ser una canción — este local solo permite música 🎵",
                                            "not_music": True})
                # Filtro de contenido: palabras bloqueadas / permitidas — compara contra
                # título+artista+género real (resuelto antes del LOCK arriba, ver "_genre"),
                # con alias para términos coloquiales que no coinciden con la etiqueta de
                # iTunes (ver _GENRE_ALIASES / _kw_matches).
                _haystack = (title + " " + (artist or "")).lower()
                _genre_l = (d.get("_genre") or "").lower()
                _blocked = [kw.strip().lower() for kw in STATE["settings"].get("blocked_keywords", []) if kw.strip()]
                if _blocked and any(_kw_matches(kw, _haystack, _genre_l) for kw in _blocked):
                    return self._send(400, {"error": "Esta canción no está disponible en este local 🎵", "content_blocked": True})
                _allowed = [kw.strip().lower() for kw in STATE["settings"].get("allowed_keywords", []) if kw.strip()]
                if _allowed and not any(_kw_matches(kw, _haystack, _genre_l) for kw in _allowed):
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
                # Anti-abuso: solo compras con cobro nuevo (salto o prioridad de un solo uso)
                # escalan de precio — pase de tiempo y créditos ya prepagados quedan igual.
                abuse_mult = priority_abuse_multiplier(table, now) if charge > 0 else 1
                charge = charge * abuse_mult
                # Dedicatoria (mensaje al pedir): cargo fijo adicional si el local lo configuró
                # — se SUMA al precio de la canción (gratis/prioridad/salto), no lo reemplaza,
                # y queda fuera del anti-abuso de arriba (son cargos independientes).
                dedica_price = int(STATE["settings"].get("dedica_price", 0) or 0)
                if req_msg and dedica_price > 0:
                    ckind = (ckind + " + dedicatoria") if charge > 0 else "dedicatoria"
                    charge += dedica_price
                charge_via, paid_amount = None, 0
                if charge > 0 and STATE["settings"].get("prepaid_mode"):
                    ok, via, err = try_charge_prepaid(sess, charge, ckind, title,
                                                       d.get("pay_method", "wallet"),
                                                       STATE["settings"].get("min_direct_pay", 700))
                    if not ok:
                        return self._send(400, err)
                    charge_via, paid_amount, charge = via, charge, 0
                if mode in ("salto", "single"):
                    record_priority_purchase(table, now)
                # Moderación del mensaje (independiente de la moderación de dedicatorias mesa a
                # mesa) — heurística de palabras clave; si lo marca, el mensaje NO se muestra en
                # TV hasta que el admin lo apruebe (la canción sí suena normal mientras tanto).
                if req_msg and STATE["settings"].get("song_message_moderation"):
                    _msg_approved, _msg_reason = _moderate_message(req_msg)
                    msg_status = "approved" if _msg_approved else "pending"
                else:
                    msg_status, _msg_reason = "approved", ""
                item = {"id": nid(), "title": title, "artist": artist, "yt": yt,
                        "token": d.get("token"), "table": table, "priority": priority,
                        "super": sup, "mode": mode, "duration": dur,
                        "status": "approved" if STATE["settings"]["auto_approve"] else "pending",
                        "play_status": "pending", "played_enough": False, "requeue_count": 0,
                        "ts": time.time(), "charge_on_play": charge, "charged": bool(charge_via),
                        "charge_kind": ckind, "charge_via": charge_via, "paid_amount": paid_amount,
                        "message": req_msg, "message_status": msg_status, "message_mod_reason": _msg_reason}
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
                return self._send(200, {"ok": True, "mode": mode, "priority_abuse_mult": abuse_mult,
                                        "session": session_public(sess)})

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
                tok = d.get("token")
                # Anti-autolike: quien pidió la canción no puede reaccionarle a la suya propia —
                # antes sí contaba, e inflaba "my_likes_total" haciéndole creer al cliente que
                # OTRAS personas reaccionaron cuando en realidad se dio like a sí mismo.
                _np_r = STATE["now_playing"]
                _react_item = next((it for it in (([_np_r] if _np_r else []) + STATE["items"] + STATE["history"])
                                     if it["id"] == item_id), None)
                if _react_item and _react_item.get("token") == tok:
                    return self._send(400, {"error": "No puedes reaccionar a tu propia canción."})
                r = STATE["reactions"].setdefault(item_id, {e: set() for e in EMOJIS})
                rp = STATE["reaction_pub"].setdefault(item_id, {e: set() for e in EMOJIS})
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
                                            "session": session_public(sess)})
                # Mismas reglas de negocio que una prioridad comprada por /api/request — antes
                # "impulsar" era una puerta de atrás: se saltaba el tope de cola premium y el
                # anti-abuso de prioridad, y además el impulsado conservaba su ts original de
                # cuando se pidió como canción normal, colándose antes de prioridades pagadas
                # legítimamente después — bug reportado en vivo ("se saltaba turnos").
                mqm = int(STATE["settings"].get("max_priority_queue_min", 0))
                if mqm > 0:
                    pq_secs = sum(i.get("duration", DEFAULT_DUR) for i in queue_view() if i.get("priority"))
                    if pq_secs // 60 >= mqm:
                        return self._send(400, {"error": f"La cola tiene más de {mqm} min de prioridades. Intenta en un rato 🎶",
                                                "priority_queue_full": True,
                                                "priority_queue_min": int(pq_secs // 60)})
                now = time.time()
                free = sess["pass_until"] > now or sess["credits"] > 0
                price = 0 if free else STATE["settings"]["price_priority"]
                abuse_mult = priority_abuse_multiplier(sess["table"], now) if price > 0 else 1
                price = price * abuse_mult
                charge_via, paid_amount = None, 0
                if price > 0 and STATE["settings"].get("prepaid_mode"):
                    ok, via, err = try_charge_prepaid(sess, price, "impulso", target["title"],
                                                       d.get("pay_method", "wallet"),
                                                       STATE["settings"].get("min_direct_pay", 700))
                    if not ok:
                        return self._send(400, err)
                    charge_via, paid_amount = via, price
                if price > 0:
                    record_priority_purchase(sess["table"], now)
                target["priority"] = True
                target["ts"] = now   # toma su lugar en la cola de prioridad desde AHORA, no desde que se pidió como normal
                target["charge_table"] = sess["table"]
                target["charge_kind"] = "impulso"
                target["charge_via"] = charge_via
                target["paid_amount"] = paid_amount
                if free:
                    if sess["pass_until"] <= now:
                        sess["credits"] -= 1
                    target["charge_on_play"] = 0
                else:
                    target["charge_on_play"] = 0 if charge_via else price
                return self._send(200, {"ok": True, "priority_abuse_mult": abuse_mult,
                                        "session": session_public(sess)})

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
                # Si hay una votación/duelo activo, su ganador ya tiene garantizado sonar
                # justo después de esta canción (super=True) — permitir un salto pagado aquí
                # rompería esa garantía (el salto tendría un ts más viejo y se colaría antes).
                if (STATE.get("poll") or {}).get("active") or (STATE.get("duelo") or {}).get("active"):
                    return self._send(400, {"error": "Hay una votación/duelo en curso — espera a que termine para saltar tu canción."})
                target = None
                for i in STATE["items"]:
                    if i["id"] == d.get("id"):
                        target = i; break
                if not target:
                    return self._send(400, {"error": "Esa canción ya no está en la cola"})
                price = STATE["settings"]["price_priority"] * STATE["settings"].get("jump_multiplier", 3)
                charge_via, paid_amount = None, 0
                if STATE["settings"].get("prepaid_mode"):
                    ok, via, err = try_charge_prepaid(sess, price, "salto al #1", target["title"],
                                                       d.get("pay_method", "wallet"),
                                                       STATE["settings"].get("min_direct_pay", 700))
                    if not ok:
                        return self._send(400, err)
                    charge_via, paid_amount = via, price
                target["super"] = True
                target["priority"] = True
                target["charge_table"] = sess["table"]
                target["charge_kind"] = "salto al #1"
                target["charge_on_play"] = 0 if charge_via else price
                target["charge_via"] = charge_via
                target["paid_amount"] = paid_amount
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

            if path == "/api/push/subscribe":
                sub = d.get("subscription")
                vid = d.get("venue") or CUR_VID
                if not sub or not sub.get("endpoint"):
                    return self._send(400, {"error": "subscription inválida"})
                subs = TYM.setdefault("push_subs", {}).setdefault(vid, [])
                # Evitar duplicados por endpoint
                endpoint = sub["endpoint"]
                if not any(s.get("endpoint") == endpoint for s in subs):
                    subs.append(sub)
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

            # ---- TV ping (marca TV como activa + arbitra dueño único por dispositivo) ----
            if path == "/api/tv_ping":
                now = time.time()
                device_id = d.get("device_id")
                force = bool(d.get("force"))
                owner = STATE.get("tv_owner")
                conflict = False
                if not device_id:
                    pass  # cliente viejo sin device_id: no arbitra, comportamiento previo
                elif force:
                    # Switch explícito confirmado por el operador (botón "Usar esta pantalla"):
                    # toma el control aunque el dueño anterior siga con ping reciente. El
                    # destronado se entera en su próximo ping (~2s) y queda en espera.
                    STATE["tv_owner"] = {"id": device_id, "last_seen": now}
                elif not owner or now - owner.get("last_seen", 0) > TV_OWNER_TIMEOUT:
                    STATE["tv_owner"] = {"id": device_id, "last_seen": now}
                elif owner["id"] == device_id:
                    owner["last_seen"] = now
                else:
                    conflict = True
                if not conflict:
                    STATE["tv_lastseen"] = now
                return self._send(200, {"ok": True, "owner_conflict": conflict})

            # ---- Player reporta progreso ----
            if path == "/api/progress":
                # Defensa en profundidad: si hay un dueño de TV establecido y este device_id
                # no es el dueño, ignorar (el bloqueo real ya ocurre en el cliente via /api/tv_ping,
                # esto solo evita que un bug/tab vieja siga pisando la posición compartida).
                owner = STATE.get("tv_owner")
                dev = d.get("device_id")
                if owner and dev and owner["id"] != dev:
                    return self._send(200, {"ok": True, "ignored": True})
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
                    if i["id"] == d.get("id"):
                        if i.get("mode") == "credito":
                            sess = get_session(i.get("token"))
                            if sess:
                                sess["credits"] += 1  # reembolsa credito reservado
                        elif i.get("charge_via") in ("wallet", "direct") and i.get("paid_amount"):
                            sess = get_session(i.get("token"))
                            customer = get_customer(sess)
                            if customer:
                                customer["balance"] = customer.get("balance", 0) + i["paid_amount"]
                                hist = customer.setdefault("wallet_history", [])
                                hist.insert(0, {"title": i["title"], "amount": i["paid_amount"],
                                                "kind": "reembolso", "ts": time.time(), "type": "refund"})
                                customer["wallet_history"] = hist[:20]
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
                          "max_priority_queue_min", "max_song_duration_min",
                          "poll_duration_secs", "duelo_duration_secs", "dedica_price"):
                    if k in d:
                        try: s[k] = max(0, int(d[k]))
                        except Exception: pass
                for k in ("fallback_shuffle", "prepaid_mode", "show_tym_brand", "allow_skip_vote", "music_only", "dedica_moderation", "song_message_moderation"):
                    if k in d:
                        s[k] = bool(d[k])
                if "min_direct_pay" in d:
                    try: s["min_direct_pay"] = max(0, int(d["min_direct_pay"]))
                    except Exception: pass
                if "theme" in d and d["theme"] in ("azul", "purpura", "verde", "rojo", "dorado", "rosa"):
                    s["theme"] = d["theme"]
                if "timezone" in d and str(d["timezone"]) in available_timezones():
                    s["timezone"] = str(d["timezone"])
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
                # Horario: se guarda directo en settings si viene en el payload
                if "schedule" in d and isinstance(d["schedule"], list):
                    slots = []
                    for sl in d["schedule"][:12]:
                        try:
                            # Validar HH:MM
                            datetime.datetime.strptime(sl["from"], "%H:%M")
                            datetime.datetime.strptime(sl["to"], "%H:%M")
                            slots.append({"from": sl["from"], "to": sl["to"],
                                          "genre": str(sl.get("genre", ""))[:40],
                                          "label": str(sl.get("label", ""))[:40]})
                        except Exception:
                            pass
                    s["schedule"] = slots
                return self._send(200, {"ok": True, "settings": s})

            # ---- Admin: anuncios para TV ----
            if path == "/api/admin/announcement":
                act = d.get("action", "create")
                if act == "create":
                    text = str(d.get("text", "")).strip()[:200]
                    if not text:
                        return self._send(400, {"error": "Texto requerido"})
                    try:
                        duration_secs = max(10, min(3600, int(d.get("duration_secs") or 120)))
                    except Exception:
                        duration_secs = 120
                    now = time.time()
                    ann = {"id": nid(), "text": text,
                           "color": str(d.get("color", "#f59e0b"))[:20],
                           "created_at": now, "active": True,
                           "duration_secs": duration_secs, "expires_at": now + duration_secs}
                    STATE.setdefault("announcements", []).append(ann)
                elif act == "toggle":
                    aid = d.get("id")
                    for a in STATE.get("announcements", []):
                        if a["id"] == aid:
                            a["active"] = not a.get("active", True)
                            if a["active"]:
                                # Reactivar (ej. tras expirar) refresca el tiempo restante en
                                # vez de mostrarlo ya vencido de nuevo.
                                a["expires_at"] = time.time() + a.get("duration_secs", 120)
                elif act == "delete":
                    aid = d.get("id")
                    STATE["announcements"] = [a for a in STATE.get("announcements", [])
                                              if a["id"] != aid]
                return self._send(200, {"ok": True,
                                        "announcements": STATE.get("announcements", [])})

            # ---- Forzar el QR visible en /tv ya mismo (botón "Mostrar QR ya" del admin) ----
            if path == "/api/admin/show_qr":
                STATE["qr_force_until"] = time.time() + 30
                return self._send(200, {"ok": True})

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
                elif act == "toggle_msg_block":
                    for t in STATE["tables"]:
                        if t["name"] == d.get("name"):
                            t["msg_blocked"] = not t.get("msg_blocked", False)
                return self._send(200, {"ok": True, "tables": STATE["tables"]})

            if path == "/api/admin/stations":
                act = d.get("action")
                if act == "add" and d.get("name"):
                    name = str(d["name"]).strip()[:40]
                    if name and name not in STATE["stations"]:
                        STATE["stations"].append(name)
                elif act == "remove":
                    STATE["stations"] = [s for s in STATE["stations"] if s != d.get("name")]
                return self._send(200, {"ok": True, "stations": STATE["stations"]})

            if path == "/api/admin/curated":
                act = d.get("action")
                if act == "add" and d.get("yt") and YT_ID_RE.match(d["yt"]):
                    if not any(c["yt"] == d["yt"] for c in STATE["curated"]):
                        STATE["curated"].append({"yt": d["yt"], "title": str(d.get("title") or "Canción")[:200],
                                                 "artist": str(d.get("artist") or "")[:120],
                                                 "duration": _parse_len(d.get("length")) or DEFAULT_DUR})
                elif act == "remove":
                    STATE["curated"] = [c for c in STATE["curated"] if c["yt"] != d.get("yt")]
                elif act == "reorder":
                    order = d.get("order") or []
                    pos = {yt: i for i, yt in enumerate(order)}
                    STATE["curated"].sort(key=lambda c: pos.get(c["yt"], 999999))
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

            if path == "/api/admin/reorder":
                new_order = d.get("order", [])
                q = queue_view()
                if new_order and len(q) > 1:
                    def _tier(it): return 0 if it.get("super") else (1 if it.get("priority") else 2)
                    id_pos = {nid: i for i, nid in enumerate(new_order)}
                    for t in (0, 1, 2):
                        tier_items = [it for it in q if _tier(it) == t]
                        if len(tier_items) < 2: continue
                        ordered = sorted(tier_items, key=lambda it: id_pos.get(it["id"], 999999))
                        ts_vals = sorted(it["ts"] for it in tier_items)
                        for i, it in enumerate(ordered):
                            if i < len(ts_vals):
                                it["ts"] = ts_vals[i]
                    save_state()
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
                STATE["duelo"] = None
                STATE["announcements"] = []
                STATE["vibe_votes"] = {}
                STATE["skip_votes"] = set()
                STATE["priority_abuse"] = {}
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
                if any(t.get("msg_blocked") and t["name"] == sess["table"] for t in STATE["tables"]):
                    return self._send(400, {"error": "El local desactivó los mensajes para tu mesa."})
                to_table = (d.get("to_table") or "").strip()
                message = (d.get("message") or "").strip()
                valid_tables = [t["name"] for t in STATE["tables"]]
                if to_table not in valid_tables:
                    return self._send(400, {"error": "Mesa de destino inválida."})
                if not message:
                    return self._send(400, {"error": "El mensaje no puede estar vacío."})
                if len(message) > 80:
                    return self._send(400, {"error": "El mensaje no puede tener más de 80 caracteres."})
                if STATE["settings"].get("dedica_moderation"):
                    _approved, mod_reason = _moderate_message(message)
                    status = "approved" if _approved else "pending"
                else:
                    status, mod_reason = "approved", ""
                ded = {"id": nid(), "from_table": sess["table"], "to_table": to_table,
                       "message": message, "ts": time.time(), "shown_tv": False,
                       "status": status, "mod_reason": mod_reason}
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

            # ---- Duelo: votar ----
            if path == "/api/duelo/vote":
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa primero."})
                _d = STATE.get("duelo")
                if not _d or not _d.get("active"):
                    return self._send(400, {"error": "No hay duelo activo."})
                yt = (d.get("yt") or "").strip()
                valid_yts = [t["yt"] for t in _d.get("teams", [])]
                if yt not in valid_yts:
                    return self._send(400, {"error": "Opción inválida."})
                tok = d.get("token")
                votes = _d.setdefault("votes", {})
                already = next((v_yt for v_yt, v_set in votes.items() if tok in v_set), None)
                if already == yt:
                    votes[yt].discard(tok); my_vote = None
                else:
                    if already: votes[already].discard(tok)
                    votes.setdefault(yt, set()).add(tok); my_vote = yt
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
                threshold = max(2, -(-active_persons_count() // 2))
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
                gate_err = _poll_gate_error()
                if gate_err:
                    return self._send(400, {"error": gate_err})
                options = d.get("options", [])
                valid_opts = [{"yt": (o.get("yt") or "").strip(),
                               "title": str(o.get("title", "Canción"))[:120],
                               "artist": str(o.get("artist", ""))[:80]}
                              for o in options if (o.get("yt") or "").strip()]
                if len(valid_opts) < 2:
                    return self._send(400, {"error": "Se necesitan al menos 2 opciones válidas."})
                if len(valid_opts) > 3:
                    valid_opts = valid_opts[:3]
                duration = _poll_dynamic_duration()
                np_id = (STATE.get("now_playing") or {}).get("id")
                now = time.time()
                STATE["poll"] = {"options": valid_opts,
                                 "votes": {o["yt"]: set() for o in valid_opts},
                                 "active": True, "created_at": now,
                                 "ends_at": now + duration,
                                 "triggered_by_np_id": np_id, "auto": False}
                STATE["poll_launched_for_id"] = np_id
                return self._send(200, {"ok": True})

            # ---- Admin: cerrar votación ----
            if path == "/api/admin/poll/close":
                _p = STATE.get("poll")
                if not _p:
                    return self._send(400, {"error": "No hay votación activa."})
                _close_poll_winner(_p)
                return self._send(200, {"ok": True})

            # ---- Admin: crear duelo ----
            if path == "/api/admin/duelo":
                gate_err = _poll_gate_error()
                if gate_err:
                    return self._send(400, {"error": gate_err})
                teams = d.get("teams", [])
                if len(teams) != 2 or not all(t.get("yt") and t.get("title") for t in teams):
                    return self._send(400, {"error": "Se necesitan exactamente 2 equipos con yt y título."})
                duration = _poll_dynamic_duration()
                now = time.time()
                STATE["duelo"] = {
                    "teams": [{"yt": t["yt"], "title": t["title"],
                               "artist": t.get("artist", ""), "label": t.get("label", f"Equipo {i+1}")}
                              for i, t in enumerate(teams)],
                    "votes": {t["yt"]: set() for t in teams},
                    "active": True,
                    "created_at": now,
                    "ends_at": now + duration,
                    "winner_yt": None,
                }
                return self._send(200, {"ok": True})

            # ---- Admin: cerrar duelo ----
            if path == "/api/admin/duelo/close":
                _d = STATE.get("duelo")
                if not _d or not _d.get("active"):
                    return self._send(400, {"error": "No hay duelo activo."})
                _close_duelo_winner(_d)
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
                        odata["pass_hash"] = hash_password(new_pass)
                        updated = True
                        break
                if not updated:
                    return self._send(400, {"error": "No se encontró el usuario."})
                save_state()
                return self._send(200, {"ok": True})

            # ---- Admin: actualizar el email registrado (a donde llega la recuperación de clave) ----
            if path == "/api/admin/update_email":
                av = self.authed_venue()
                email = (d.get("email") or "").strip()[:120]
                if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                    return self._send(400, {"error": "Correo inválido."})
                updated = False
                for uname, odata in TYM["owners"].items():
                    if odata.get("venue") == av:
                        odata["email"] = email
                        updated = True
                        break
                if not updated:
                    return self._send(400, {"error": "No se encontró el usuario."})
                save_state()
                return self._send(200, {"ok": True, "email": email})

            # ---- Admin: eliminar dedicatoria ----
            if path == "/api/admin/dedica/delete":
                ded_id = d.get("id")
                if ded_id is None:
                    return self._send(400, {"error": "Falta el id."})
                STATE["dedicas"] = [dd for dd in STATE.get("dedicas", []) if dd["id"] != ded_id]
                return self._send(200, {"ok": True})

            # ---- Admin: aprobar/rechazar dedicatoria pendiente de moderación ----
            if path in ("/api/admin/dedica/approve", "/api/admin/dedica/reject"):
                ded_id = d.get("id")
                if ded_id is None:
                    return self._send(400, {"error": "Falta el id."})
                new_status = "approved" if path.endswith("approve") else "rejected"
                for dd in STATE.get("dedicas", []):
                    if dd["id"] == ded_id:
                        dd["status"] = new_status
                        break
                return self._send(200, {"ok": True})

            # ---- Admin: aprobar/rechazar el mensaje de una canción pedida (separado de las
            # dedicatorias mesa a mesa) — el mensaje puede estar en la canción sonando ahora o
            # todavía en cola, así que se busca en ambos lugares. ----
            if path in ("/api/admin/song_message/approve", "/api/admin/song_message/reject"):
                item_id = d.get("id")
                if item_id is None:
                    return self._send(400, {"error": "Falta el id."})
                new_status = "approved" if path.endswith("approve") else "rejected"
                target = STATE["now_playing"] if STATE["now_playing"] and STATE["now_playing"]["id"] == item_id else None
                if not target:
                    target = next((i for i in STATE["items"] if i["id"] == item_id), None)
                if target:
                    target["message_status"] = new_status
                return self._send(200, {"ok": True})

        # ---- TYM Master: resetear la contraseña del dueño de un local ----
        if path == "/api/tym/reset_password":
            if self.authed_venue() != "*":
                return self._send(403, {"error": "Solo TYM master"})
            vid = (d.get("vid") or "").strip()
            new_pass = (d.get("new_pass") or "").strip()
            if len(new_pass) < 8:
                return self._send(400, {"error": "La contraseña debe tener al menos 8 caracteres."})
            with LOCK:
                updated = False
                for uname, odata in TYM["owners"].items():
                    if odata.get("venue") == vid:
                        odata["pass_hash"] = hash_password(new_pass)
                        updated = True
                        break
                if not updated:
                    return self._send(400, {"error": "No se encontró el dueño de ese local."})
                save_state()
            return self._send(200, {"ok": True})

        # ---- TYM Master: bloquear/desbloquear el login de un local (ej. moroso) ----
        if path == "/api/tym/toggle_block":
            if self.authed_venue() != "*":
                return self._send(403, {"error": "Solo TYM master"})
            vid = (d.get("vid") or "").strip()
            with LOCK:
                own = next((o for o in TYM["owners"].values() if o.get("venue") == vid), None)
                if not own:
                    return self._send(400, {"error": "No se encontró el dueño de ese local."})
                own["blocked"] = not own.get("blocked")
                save_state()
                blocked = own["blocked"]
            return self._send(200, {"ok": True, "blocked": blocked})

        # ---- TYM Master: gestión de locales ----
        if path == "/api/tym/create_venue":
            if self.authed_venue() != "*":
                return self._send(403, {"error": "Solo TYM master"})
            vid = re.sub(r"[^a-z0-9_-]", "", (d.get("vid") or "").strip().lower())[:24]
            name = str(d.get("name") or "").strip()[:60]
            username = re.sub(r"\s+", "", str(d.get("username") or "").strip())[:32]
            password = str(d.get("password") or "").strip()[:64]
            email = str(d.get("email") or "").strip()[:120]
            if not vid or not name or not username or not password or not email:
                return self._send(400, {"error": "Completa todos los campos"})
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                return self._send(400, {"error": "Correo inválido."})
            with LOCK:
                if vid in VENUES:
                    return self._send(400, {"error": f"El ID '{vid}' ya existe"})
                if username in TYM["owners"]:
                    return self._send(400, {"error": f"El usuario '{username}' ya existe"})
                VENUES[vid] = make_venue(name)
                TYM["owners"][username] = {"pass_hash": hash_password(password), "venue": vid,
                                            "email": email, "blocked": False}
                save_state()
            return self._send(200, {"ok": True, "vid": vid, "name": name})

        return self._send(404, {"error": "not found"})

# =================== Persistencia (archivo JSON; fácil de migrar a DB/multi-bar) ===================
DATA_FILE = os.path.join(HERE, "data.json")
PERSIST_KEYS = ("settings", "tables", "stations", "customers", "sessions", "now_playing", "items", "ledger",
                "history", "curated", "req_counts", "jump_used_for", "learned_end", "assists",
                "request_log", "dedicas", "poll_launched_for_id")

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
    _duelo = v.get("duelo")
    if _duelo:
        _dc = dict(_duelo)
        _dc["votes"] = {yt: list(tokens) for yt, tokens in _dc.get("votes", {}).items()}
        snap["duelo"] = _dc
    else:
        snap["duelo"] = None
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
    # La canción que sonaba justo antes de un reinicio/deploy retoma desde el inicio: el
    # reproductor de la TV siempre carga el video desde 0 en una recarga fresca (no hay forma
    # de reanudar un iframe de YouTube a mitad de canción), así que dejar aquí la posición
    # vieja solo generaba una barra de progreso/contador confusos (mostraba varios minutos
    # avanzados mientras el video en realidad arrancaba de cero) — bug reportado en vivo.
    if v.get("now_playing"):
        v["now_playing"]["position"] = 0
        v["now_playing"]["played_enough"] = False
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
    _ds = snap.get("duelo")
    if _ds:
        _dc = dict(_ds)
        _dc["votes"] = {yt: set(tokens) for yt, tokens in _dc.get("votes", {}).items()}
        v["duelo"] = _dc
    else:
        v["duelo"] = None

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
            for k in ("socials", "tym_logo", "subscribers", "events", "accounts", "vapid", "push_subs"):
                if k in d["tym"]:
                    TYM[k] = d["tym"][k]
            # Carga todos los owners desde la DB (iniciales + creados dinámicamente)
            for uname, odata in d["tym"].get("owners", {}).items():
                od = dict(odata)
                # Migra contraseñas en texto plano de versiones antiguas del código
                if "pass" in od and "pass_hash" not in od:
                    od["pass_hash"] = hash_password(od.pop("pass"))
                od.pop("pass", None)
                # Migra owners de antes de "recuperar clave por email" (sin email/blocked)
                od.setdefault("email", "jhonyt37@gmail.com")
                od.setdefault("blocked", False)
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

if __name__ == "__main__":
    load_state()
    _ensure_owner_passwords()
    _ensure_vapid_keys()
    threading.Thread(target=autosave_loop, daemon=True).start()
    ip = lan_ip()
    url = PUBLIC_URL.rstrip("/") + "/" if PUBLIC_URL else f"http://{ip}:{PORT}/"
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
