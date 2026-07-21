#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TYM Music — MVP v2 (Python stdlib, sin dependencias).
Vistas: /  (cliente)  ·  /player (pantalla del local)  ·  /admin (dueno)
Novedades: sesion de mesa por PIN, paquetes (creditos + pase), progreso de
reproduccion, recomendadas (mas pedido / del local / populares / genero).
"""
import json, os, re, socket, threading, time, random, datetime, struct, zlib
import unicodedata, difflib
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
# Cambia SOLO en el momento en que arranca este proceso (a diferencia de VERSION, que hay que
# subir a mano) — sirve para que /tv detecte un redeploy sin depender de que alguien recuerde
# actualizar VERSION en cada deploy. Ver BOOT_ID en /api/state y el watchdog en tv.html.
BOOT_ID = secrets.token_hex(6)
PORT = int(os.environ.get("PORT", 8000))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
# Upstash Redis (backup remoto: evita perder datos en Render/hosts con disco efímero)
# Crea una DB gratuita en upstash.com → copia REST URL y token → ponlos como variables de entorno
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_KEY   = "tym_state"
DEFAULT_DUR = 210  # 3:30 si no se conoce la duracion
NEW_SONG_WINDOW_SECS = 7 * 86400  # ventana de "🆕 nueva" en el catálogo local — no configurable
                                   # todavía (pedido explícito de mantenerlo simple por ahora)
TV_OWNER_TIMEOUT = 8  # seg sin ping del dueño actual de la TV -> se libera (4x el intervalo de ping de 2s)
EMOJIS = ["❤️", "🔥", "👍", "💃", "🎉"]  # reacciones positivas
LOVED_CELEBRATION_THRESHOLD = 5  # reacciones acumuladas (todos los emojis) para "entró al top de hoy"
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

def gen_unique_pin():
    """PIN de 4 dígitos que no choca con ningún código ya asignado en el local (ni el PIN
    principal de una mesa ni los códigos extra por persona) — usado al agregar una mesa o un
    código adicional para una mesa que ya tiene gente."""
    used = {t["pin"] for t in STATE["tables"]} | {p for t in STATE["tables"] for p in t.get("extra_pins", [])}
    for _ in range(200):
        p = gen_pin()
        if p not in used:
            return p
    return gen_pin()  # fallback extremo, prácticamente imposible de alcanzar

def gen_token():
    return secrets.token_hex(16)

def remove_solid_bg(data_uri):
    """Procesa un logo subido (data URI base64) antes de guardarlo:
    (1) Lo redimensiona si es más grande de lo necesario — en pantalla un logo nunca se
    muestra a más de ~180px, así que una foto de varios cientos de KB solo desperdicia
    espacio y además puede reventar el límite de tamaño guardado (700000 caracteres),
    TRUNCANDO el base64 a la mitad y corrompiéndolo en un archivo irrenderizable — bug
    reportado en vivo: un logo real de 634KB (846000 caracteres en base64, por encima del
    límite) nunca aparecía en NINGUNA pantalla, ni TV ni cliente, sin ningún error visible.
    (2) Si el fondo es de un color sólido/uniforme, lo vuelve transparente — así en el TV/la
    app no se ve un rectángulo de color alrededor del logo, solo el logo. Heurística: si los
    4 bordes del PNG/JPG son del mismo color (dentro de una tolerancia), se hace flood-fill
    de ese color desde el borde hacia adentro (nunca borra zonas del mismo color que estén
    DENTRO del logo, solo lo que es alcanzable desde afuera). Si la imagen ya tiene
    transparencia real, o el borde no es uniforme (foto, degradado, diseño sin fondo
    sólido), el color de fondo se deja intacto — "en caso de que aplique", nunca a ciegas.
    (3) Limpia el halo de anti-aliasing: el borde del dibujo original está mezclado con el
    fondo (para que se vea suave), así que tras el flood-fill queda un anillo delgado de
    píxeles OPACOS con un color intermedio — se ve como un contorno claro alrededor del logo
    en pantallas oscuras. Segunda pasada: cualquier píxel opaco pegado a uno recién vuelto
    transparente, si su color todavía se parece bastante al fondo (tolerancia más ancha),
    también se vuelve transparente.
    (4) Recorta al rectángulo real del contenido (bbox de los píxeles no transparentes) — sin
    esto, el margen vacío que dejó el fondo removido queda guardado como si fuera parte del
    logo, y en pantalla (limitado por altura, ej. 56px) el dibujo real ocupa solo una fracción
    chica de esa altura porque el resto es aire transparente. Bug reportado en vivo: un logo
    real con bastante margen blanco se veía diminuto en TV y cliente pese a procesarse bien.
    """
    try:
        from PIL import Image
    except ImportError:
        return data_uri
    try:
        header, _, b64 = data_uri.partition(",")
        if not b64:
            return data_uri
        import base64, io
        raw = base64.b64decode(b64)
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        w, h = im.size
        if w < 4 or h < 4:
            return data_uri
        MAX_DIM = 480  # de sobra incluso para retina a los tamaños que se muestra en pantalla
        if max(w, h) > MAX_DIM:
            scale = MAX_DIM / max(w, h)
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
            w, h = im.size
        px = im.load()
        has_alpha_already = any(px[x, y][3] < 250 for x in (0, w - 1) for y in (0, h - 1))
        if not has_alpha_already:
            corners = [px[0, 0][:3], px[w - 1, 0][:3], px[0, h - 1][:3], px[w - 1, h - 1][:3]]
            def dist(a, b):
                return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5
            base = corners[0]
            uniform_border = all(dist(base, c) <= 30 for c in corners[1:])
            if uniform_border:
                TOL = 40
                from collections import deque
                seen = bytearray(w * h)
                made_transparent = bytearray(w * h)
                q = deque()
                for x in range(w):
                    q.append((x, 0)); q.append((x, h - 1))
                for y in range(h):
                    q.append((0, y)); q.append((w - 1, y))
                while q:
                    x, y = q.popleft()
                    if x < 0 or x >= w or y < 0 or y >= h:
                        continue
                    idx = y * w + x
                    if seen[idx]:
                        continue
                    seen[idx] = 1
                    r, g, b, a = px[x, y]
                    if dist((r, g, b), base) > TOL:
                        continue
                    px[x, y] = (r, g, b, 0)
                    made_transparent[idx] = 1
                    q.append((x - 1, y)); q.append((x + 1, y)); q.append((x, y - 1)); q.append((x, y + 1))
                TOL2 = TOL * 2.2
                for y in range(h):
                    for x in range(w):
                        idx = y * w + x
                        if made_transparent[idx]:
                            continue
                        r, g, b, a = px[x, y]
                        if a == 0:
                            continue
                        near_transparent = False
                        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                            if 0 <= nx < w and 0 <= ny < h and made_transparent[ny * w + nx]:
                                near_transparent = True
                                break
                        if near_transparent and dist((r, g, b), base) <= TOL2:
                            px[x, y] = (r, g, b, 0)
        bbox = im.getbbox()
        if bbox and bbox != (0, 0, w, h):
            pad = 4
            l, t, r2, b2 = bbox
            im = im.crop((max(0, l - pad), max(0, t - pad), min(w, r2 + pad), min(h, b2 + pad)))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        print("remove_solid_bg error:", e)
        return data_uri

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
            # Anti-abuso de precio (pedido explícito: antes 60 min fijo sin poder cambiarlo, y
            # compartido entre "Prioridad" y "Al frente" — pedir uno subía el precio del otro
            # también). Cada tipo tiene su propia ventana configurable y su propio contador
            # (ver priority_abuse_multiplier/record_priority_purchase, kind="prio"/"jump") — la
            # escala 1x→2x→3x se queda fija, solo la ventana de tiempo es configurable.
            "priority_abuse_window_min": 60,
            "jump_abuse_window_min": 60,
            "venue_logo": "",          # logo del BAR
            "max_priority_queue_min": 0,   # bloquear nuevas prioridades si cola premium > N min (0=off)
            "max_song_duration_min": 0,    # rechazar pedidos de canciones más largas que N min (0=off)
            "music_only": False,           # rechazar pedidos que la IA clasifique como no-música
            "song_message_moderation": False,  # moderar el mensaje al pedir canción (separado de dedicatorias)
            "dedica_price": 0,             # cargo extra si el pedido incluye mensaje (0=gratis, se suma al precio de la canción)
            "dedica_display_secs": 5,      # cuánto dura el overlay de dedicatoria mesa a mesa en /tv
            "dedica_presets": [             # mensajes predeterminados: pasan SIEMPRE sin código ni moderación
                "🎉 ¡Qué buen ambiente!",
                "🎂 ¡Feliz cumpleaños!",
                "🔥 ¡Esta rola está buenísima!",
                "💃 ¡A bailar se ha dicho!",
                "🙌 Saludos para toda la mesa",
            ],
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
            "content_mode": "youtube",  # "youtube" (buscador/catálogo ilimitado, hoy) | "local" (catálogo propio
                                         # del bar, archivos que TYM nunca aloja — ver plan federated-knitting-lagoon)
            "allow_self_react": False,  # ON: dejar que quien pidió una canción reaccione a la suya propia
                                         # (bloqueado por defecto — ver /api/react)
        },
        "tables": [{"name": f"Mesa {i}", "pin": str(i) * 4, "extra_pins": []} for i in range(1, 6)],  # PINs 1111..5555
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
        "dedica_codes": [],        # [{code, created_at, used, used_at, used_by_table}] — códigos de un
                                    # solo uso que el admin genera para aprobar mensajes personalizados
        "bis_votes": {},           # {yt: set(tokens)} — votos de bis por canción
        "poll": None,              # {options, votes, active, created_at, ends_at, triggered_by_np_id, auto}
        "poll_launched_for_id": None,  # np.id para el que se lanzó/cerró el último poll
        "duelo": None,             # {teams:[{yt,title,artist,label},...], votes:{yt:set()}, active, ends_at, created_at}
        "announcements": [],       # [{id, text, color, created_at, active}]
        "vibe_votes": {},          # {emoji: set(tokens)} — votos de vibe
        "skip_votes": set(),       # set de tokens que votaron para saltar la canción actual
        "celebrated_loved": set(),      # yt IDs ya celebrados hoy (entrada al top de "lo más querido") —
                                         # en memoria nomás, no se persiste: perderlo en un redeploy solo
                                         # significa que una canción se puede volver a celebrar una vez, sin costo real
        "loved_celebration": None,      # {"yt","title","artist","total","ts"} — última celebración, la TV
                                         # se guía por el "ts" para no repetir la animación en cada poll
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
    # Caché de identificación por audio (AudD, ver identify_audio_bytes) — COMPARTIDA entre
    # TODOS los bares, no por venue. Pedido explícito: "nuestra propia huella musical antes de
    # ir hasta AudD" — muchos bares seguramente tienen el mismo mp3 (bajado del mismo sitio/
    # grupo), así que si un bar ya lo identificó, el siguiente no gasta cuota de AudD para lo
    # mismo. Clave = sha256 del archivo completo (huella exacta, gratis, sin dependencias
    # nuevas) -> {"artist","title","ts"}.
    "audd_cache": {},
    # Base de metadata compartida entre TODOS los bares (catálogo local) — pedido explícito:
    # "ir haciendo nuestra propia huella musical". Clave = título+artista normalizados (ver
    # _track_key), NO el archivo — así una canción con distinto bitrate/formato entre dos bares
    # igual hace match, mientras el nombre/tag sea razonable. Ver track_db_lookup/_contribute.
    "track_db": {},
    # Huella EXACTA del archivo (sha256, calculado en el navegador con Web Crypto durante el
    # escaneo — el audio nunca se sube) -> {"genre","cover","ts"}. Más confiable que track_db
    # cuando hace match (son literalmente los mismos bytes, no solo un título parecido) —
    # pedido explícito: detectar el mismo mp3 entre bares sin depender del nombre del archivo.
    "file_db": {},
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

def refund_song_charge(item):
    """El admin saltó a mano una canción PAGADA (prioridad/salto al #1) que no alcanzó el
    80% de su duración — se le devuelve el dinero al cliente, cubriendo los 2 caminos de
    cobro posibles: saldo prepago (ya se descontó al pedir el impulso, vía try_charge_prepaid)
    o cuenta de mesa (se cobra recién al sonar, vía log_charge en promote_next)."""
    paid_amount = item.get("paid_amount", 0)
    if item.get("charge_via") in ("wallet", "direct") and paid_amount > 0:
        sess = get_session(item.get("token"))
        customer = get_customer(sess) if sess else None
        if customer:
            customer["balance"] = customer.get("balance", 0) + paid_amount
            hist = customer.setdefault("wallet_history", [])
            hist.insert(0, {"title": item.get("title", ""), "amount": paid_amount,
                            "kind": "reembolso — el bar saltó la canción", "ts": time.time(), "type": "refund"})
            customer["wallet_history"] = hist[:20]
    elif item.get("charged") and item.get("charge_on_play", 0) > 0:
        STATE["ledger"].append({"table": item.get("charge_table") or item.get("table", ""),
                                 "title": item.get("title", ""), "amount": -item["charge_on_play"],
                                 "kind": "reembolso — el bar saltó la canción", "ts": time.time()})
    item["refunded"] = True
    _redis_last_save[0] = 0

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

def is_local_id(yt):
    """Identifica una canción del catálogo local del bar (Fase 1 del modo sin YouTube — ver
    plan federated-knitting-lagoon.md) — reutiliza el mismo campo `yt` que usa todo el
    sistema, con un prefijo distinguible, en vez de un campo paralelo. La gran mayoría del
    código que toca `.yt` lo trata como clave opaca (cola, votos, reacciones, historial) y no
    necesita cambios; solo los puntos que validan formato o hacen una llamada de red específica
    de YouTube (este archivo, ~6 lugares) necesitan revisar esto explícitamente."""
    return isinstance(yt, str) and yt.startswith("local:")

def new_local_count(now):
    """Cuántas canciones del catálogo local se agregaron en los últimos NEW_SONG_WINDOW_SECS —
    para el badge "🆕 Nuevas" del cliente (index.html), sin que el cliente tenga que descargar
    el catálogo completo solo para saber si hay algo nuevo que mostrar."""
    cutoff = now - NEW_SONG_WINDOW_SECS
    return sum(1 for c in STATE["curated"]
               if is_local_id(c.get("yt")) and not c.get("missing") and not c.get("excluded")
               and c.get("added_at") and c["added_at"] >= cutoff)

def _fold(s):
    """minúsculas + sin tildes/diacríticos — pedido explícito: la búsqueda del catálogo local
    debe tolerar tildes/mayúsculas ("cancion" debe encontrar "canción" y viceversa)."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def _fuzzy_local_search(pool, qf):
    """Respaldo cuando la búsqueda exacta (por substring, ya con tildes normalizadas) no
    encuentra nada — pedido explícito: tolerar palabras mal escritas. Compara cada palabra de
    la búsqueda contra las palabras de título+artista con SequenceMatcher (stdlib, sin
    dependencias externas); exige que TODAS las palabras de la búsqueda tengan una pareja
    razonablemente parecida, para no devolver resultados demasiado sueltos."""
    qwords = [w for w in qf.split() if len(w) >= 3]
    if not qwords:
        return []
    out = []
    for c in pool:
        twords = _fold(c["title"] + " " + (c.get("artist") or "")).split()
        if all(any(difflib.SequenceMatcher(None, qw, tw).ratio() >= 0.72 for tw in twords) for qw in qwords):
            out.append(c)
    return out

def yt_id(text):
    text = (text or "").strip()
    m = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    return None

def yt_title(vid):
    """Usado cuando el cliente PEGA un link de YouTube en vez de buscar — a diferencia de
    /api/search, este título/artista nunca pasaba por _clean_title_display()/enrich_artists(),
    así que quedaba con basura tipo "(Official Video)" y con el CANAL de YouTube como artista
    en vez del artista real (mismo bug que se arregló para búsqueda en 5695c3a, pero este
    camino se quedó fuera). Se limpia y se intenta iTunes igual que el resto de la app."""
    try:
        u = "https://www.youtube.com/oembed?format=json&url=https://youtu.be/" + vid
        d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=8).read())
        title = _clean_title_display(d.get("title", "Canción"))
        channel = d.get("author_name", "") or "YouTube"
        artist = _lookup_real_artist(_clean_for_lookup(title)) or channel
        return title, artist
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
    "official music video", "official video", "official audio", "official lyric video",
    "video oficial", "vídeo oficial", "audio oficial",
    "video lirico", "video lírico", "vídeo lírico", "lirico", "lírico",
    "lyric video", "lyrics video", "video lyrics", "lyrics", "letra oficial",
    "letra completa", "letra", "visualizer", "karaoke",
    "video clip oficial", "videoclip oficial", "videoclip", "clip officiel",
    "audio", "video", "mv",
]
_BRACKET_RE = re.compile(r"[\(\[\{]([^()\[\]{}]*)[\)\]\}]")
_JUNK_WORD_RE = re.compile(r"\b(hd|4k|hq|official|oficial)\b", re.IGNORECASE)
_JUNK_TRAILING_RE = re.compile(
    r"\s*[-|]\s*(?:" + "|".join(re.escape(p) for p in _JUNK_PHRASES) + r")\s*$",
    re.IGNORECASE)
# Basura que sale FUERA de paréntesis en resultados de búsqueda reales (no solo en el
# catálogo/curados, que suelen venir limpios): separador "// Reggaeton Viejo 🔥" que algunos
# canales agregan como tag de género, una sola palabra de basura pegada al final sin guion
# ("... Letra"), y emoji decorativo al final del título.
_TRAILING_SEP_RE = re.compile(r"\s*//.*$")
_TRAILING_BARE_JUNK_RE = re.compile(
    r"\s+(?:" + "|".join(re.escape(p) for p in ("letra", "lyrics", "audio", "video", "official", "oficial")) + r")\s*$",
    re.IGNORECASE)
_TRAILING_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+\\s*$")

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
    t = _TRAILING_SEP_RE.sub("", t)
    t = _JUNK_TRAILING_RE.sub("", t)
    # Palabra de basura pegada al final sin guion/parentesis (ej. "... Letra") y emoji
    # decorativo — se repite unas pasadas por si quedan varias encadenadas al final.
    for _ in range(3):
        new_t = _TRAILING_EMOJI_RE.sub("", t)
        new_t = _TRAILING_BARE_JUNK_RE.sub("", new_t).strip()
        if new_t == t:
            break
        t = new_t
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
# Wikidata pide un User-Agent identificable (política de Wikimedia) — sin esto puede bloquear.
WIKIDATA_UA = "TYMMusic/1.0 (https://tym-music.onrender.com; contacto via app) genre-lookup"

def _clean_for_lookup(t):
    """Quita '(Video Oficial)'/'[Lyrics]'/etc y todo tras 'ft./feat.' — sin esto iTunes
    no encuentra nada (probado: con parentesis da 0 resultados, limpio si matchea)."""
    t = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", t or "")
    t = re.sub(r"\b(ft|feat|featuring)\b.*$", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()

def _itunes_query(term, entity, limit=3):
    if not term:
        return []
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": entity, "limit": limit, "country": "CO"})
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

# ---- Clasificación de género combinada (varias fuentes, para catálogo local) ----
# Pruebas reales (2026-07-20, ~103 archivos + 109 artistas colombianos) mostraron que ninguna
# fuente sola pasa del ~55%: iTunes solo (55%, y con etiquetas GENÉRICAS a veces incorrectas —
# ej. clasifica a Diomedes Díaz, vallenato puro, como "Salsa y tropical"), Wikidata solo (36%,
# pero cuando responde es MÁS específico y preciso), y AcoustID (huella de audio) reconoce el
# audio al 97% pero su base gratuita casi nunca lo enlaza a un título/artista usable (0% útil).
# La señal MÁS fuerte y precisa resultó ser el propio NOMBRE del archivo/carpeta — la gente los
# nombra con el género ("...Video Letra - Sentir Vallenato.mp3", carpeta "Salsa Clásica") — y
# eso es a la vez más cobertura y más preciso que iTunes para música regional. Por eso el orden
# de prioridad es: nombre del archivo → Wikidata → iTunes. Todo gratis, sin límite comercial.

# Palabras que, si aparecen LITERALES en el nombre del archivo/carpeta, dan el género con
# altísima precisión. Orden = prioridad (el primero que matchea gana). Solo términos inequívocos.
_GENRE_KEYWORDS = [
    ("vallenato", "vallenato"), ("champeta", "champeta"), ("cumbia", "cumbia"),
    ("salsa", "salsa"), ("merengue", "merengue"), ("bachata", "bachata"),
    ("reggaeton", "reggaeton"), ("reguetón", "reggaeton"), ("reguet", "reggaeton"),
    ("ranchera", "ranchera"), ("mariachi", "ranchera"), ("bolero", "bolero"),
    ("carranga", "carranga"), ("joropo", "joropo"), ("currulao", "currulao"),
    ("porro", "porro"), ("bullerengue", "bullerengue"), ("mapale", "mapalé"),
    ("musica popular", "música popular"), ("corrido", "corrido"), ("banda", "banda"),
    ("balada", "balada"), ("metal", "metal"), ("punk", "punk"), ("rock", "rock"),
    ("jazz", "jazz"), ("blues", "blues"), ("hip hop", "hip hop"), ("hip-hop", "hip hop"),
    ("electronica", "electrónica"), ("techno", "electrónica"), ("house", "electrónica"),
    ("tropical", "tropical"),
]

def _fold_txt(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))

def genre_from_text(text):
    """Detecta un género explícito nombrado en un texto (nombre de archivo/carpeta/ruta).
    Gratis, instantáneo, sin red — la señal más precisa para música regional. None si no hay."""
    ft = _fold_txt(text)
    for kw, genre in _GENRE_KEYWORDS:
        if kw in ft:
            return genre
    return None

_wikidata_cache = {}

def wikidata_genre(artist):
    """Género del artista vía Wikidata (propiedad P136), datos CC0 (dominio público, libre para
    uso comercial — confirmado en sus términos). Busca la entidad por nombre, lee su género y lo
    traduce a la etiqueta en español. Menos cobertura que iTunes pero MÁS específico/preciso
    cuando responde (ej. da 'Vallenato' donde iTunes da 'Salsa y tropical'). None si no hay."""
    artist = (artist or "").strip()[:80]
    if not artist:
        return None
    key = artist.lower()
    now = time.time()
    hit = _wikidata_cache.get(key)
    if hit and now - hit[0] < GENRE_TTL:
        return hit[1]
    genre = None
    try:
        def _wget(url):
            req = urllib.request.Request(url, headers={"User-Agent": WIKIDATA_UA})
            return json.loads(urllib.request.urlopen(req, timeout=6).read())
        su = ("https://www.wikidata.org/w/api.php?action=wbsearchentities&search="
              + urllib.parse.quote(artist) + "&language=es&type=item&format=json&limit=1")
        res = _wget(su).get("search", [])
        if res:
            qid = res[0]["id"]
            eu = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={qid}&props=claims&format=json"
            claims = list(_wget(eu)["entities"].values())[0].get("claims", {})
            gqids = [g["mainsnak"]["datavalue"]["value"]["id"]
                     for g in claims.get("P136", []) if "datavalue" in g["mainsnak"]]
            if gqids:
                lu = (f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={gqids[0]}"
                      "&props=labels&languages=es&format=json")
                lab = list(_wget(lu)["entities"].values())[0].get("labels", {}).get("es", {})
                genre = lab.get("value")
    except Exception:
        genre = None
    _wikidata_cache[key] = (now, genre)
    if len(_wikidata_cache) > 3000:
        stale = [k for k, v in _wikidata_cache.items() if now - v[0] > GENRE_TTL]
        for k in stale:
            del _wikidata_cache[k]
    return genre

# ---- Base de metadata compartida entre bares (TYM["track_db"]) — "nuestra propia huella
# musical": clave = título+artista normalizados, NO el archivo, para que la misma canción con
# distinto bitrate/formato/nombre de archivo entre dos bares igual haga match con solo un poco
# de parecido en el título/artista (mismo criterio que ya usan itunes_genre/wikidata_genre para
# buscar). Sin huella de audio real todavía (necesitaría decodificar el archivo — ver Fase 2,
# huella acústica vía Docker+chromaprint) — esto es puro texto, gratis, instantáneo.
TRACK_DB_MAX = 20000

def _track_key(artist, title):
    a = _fold_txt((artist or "").strip())
    t = _fold_txt(_clean_for_lookup((title or "").strip()))
    if not a and not t:
        return None
    return f"{a}|{t}"

def track_db_lookup(artist, title):
    key = _track_key(artist, title)
    if not key:
        return None
    return TYM["track_db"].get(key)

def track_db_contribute(artist, title, genre=None, cover=None, verified=False):
    """Sube (o corrige) una entrada de la base compartida. Sin `verified` (clasificación
    automática al escanear/autocompletar) nunca pisa un dato ya existente — evita que un guess
    débil de un bar arruine el dato bueno que ya subió otro. Con `verified` (edición manual del
    admin, o entrada 'congelada') sí sobreescribe — es la fuente más confiable que hay."""
    key = _track_key(artist, title)
    if not key or not (genre or cover):
        return
    existing = TYM["track_db"].get(key)
    if existing and not verified:
        return
    TYM["track_db"][key] = {
        "genre": genre or (existing or {}).get("genre"),
        "cover": cover or (existing or {}).get("cover"),
        "ts": time.time(),
    }
    if len(TYM["track_db"]) > TRACK_DB_MAX:
        stale = sorted(TYM["track_db"].items(), key=lambda kv: kv[1].get("ts", 0))
        for k, _ in stale[: len(TYM["track_db"]) - TRACK_DB_MAX]:
            del TYM["track_db"][k]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

def _norm_sha(sha):
    sha = str(sha or "").strip().lower()
    return sha if _SHA256_RE.match(sha) else None

def file_db_lookup(sha256):
    key = _norm_sha(sha256)
    if not key:
        return None
    return TYM["file_db"].get(key)

def file_db_contribute(sha256, genre=None, cover=None, verified=False):
    """Misma lógica de confianza que track_db_contribute (ver arriba), pero por huella exacta
    del archivo — un match acá es más fuerte que uno por texto, así que al importar se consulta
    PRIMERO (ver acción 'import' de /api/admin/local_catalog)."""
    key = _norm_sha(sha256)
    if not key or not (genre or cover):
        return
    existing = TYM["file_db"].get(key)
    if existing and not verified:
        return
    TYM["file_db"][key] = {
        "genre": genre or (existing or {}).get("genre"),
        "cover": cover or (existing or {}).get("cover"),
        "ts": time.time(),
    }
    if len(TYM["file_db"]) > TRACK_DB_MAX:
        stale = sorted(TYM["file_db"].items(), key=lambda kv: kv[1].get("ts", 0))
        for k, _ in stale[: len(TYM["file_db"]) - TRACK_DB_MAX]:
            del TYM["file_db"][k]

def classify_genre(artist, title, hint=""):
    """Cascada combinada (ver comentario grande arriba). Prioridad por precisión:
    1) género nombrado en el archivo/carpeta (hint) — lo más preciso y gratis,
    2) Wikidata por artista — específico cuando responde,
    3) iTunes por artista/título — mayor cobertura pero etiquetas genéricas.
    Devuelve la primera que dé algo, o None."""
    hit = track_db_lookup(artist, title)
    if hit and hit.get("genre"):
        return hit["genre"]
    g = genre_from_text(hint)
    if g:
        return g
    g = wikidata_genre(artist)
    if g:
        return g
    return itunes_genre(artist, title)

_cover_cache = {}

def itunes_cover(artist, title):
    """Carátula real del álbum/sencillo vía iTunes (pedido explícito: para catálogo local, ni
    tags embebidos en el archivo ni un fotograma del video — la carátula oficial, cruzando por
    título/artista igual que itunes_genre(). Nunca sube el archivo a ningún lado, solo hace la
    misma búsqueda de texto que ya se hace para el género."""
    artist = (artist or "").strip()[:80]
    title = (title or "").strip()[:150]
    if not artist and not title:
        return None
    key = (artist.lower(), title.lower())
    now = time.time()
    hit = _cover_cache.get(key)
    if hit and now - hit[0] < GENRE_TTL:
        return hit[1]
    clean_title = _clean_for_lookup(title)
    results = _itunes_query(f"{artist} {clean_title}".strip(), "song")
    al = artist.lower()
    match = next((r for r in results if al and (al in (r.get("artistName") or "").lower()
                  or (r.get("artistName") or "").lower() in al)), None)
    art = (match or (results[0] if results else {})).get("artworkUrl100")
    # iTunes sirve 100x100 por defecto — el mismo path acepta cualquier tamaño en el nombre,
    # 600x600 se ve bien hasta en la pantalla grande del TV sin pedir nada especial.
    cover = art.replace("100x100bb", "600x600bb") if art else None
    _cover_cache[key] = (now, cover)
    if len(_cover_cache) > 3000:
        stale = [k for k, v in _cover_cache.items() if now - v[0] > GENRE_TTL]
        for k in stale:
            del _cover_cache[k]
    return cover

# ---- Carátula propia subida por el admin (pedido explícito, distinto del logo del bar) —
# límite duro por bar: son potencialmente cientos de canciones, no un solo logo, así que sin
# límite el estado guardado (data.json/Redis) crecería sin control. El navegador ya la
# redimensiona antes de subir (evita reventar el límite de 3MB del body); esto es una segunda
# pasada de seguridad server-side, más liviana que remove_solid_bg (logo) — una carátula real
# ya trae su propio diseño, quitarle el fondo la arruinaría, así que NUNCA se toca el fondo acá.
MAX_CUSTOM_COVERS = 50

def resize_cover_image(data_uri, max_dim=500):
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        header, _, b64 = data_uri.partition(",")
        if not b64:
            return None
        import base64, io
        raw = base64.b64decode(b64)
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = im.size
        if w < 4 or h < 4:
            return None
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None

# ---- Sugerencias de carátula vía Google Images (respaldo cuando iTunes no tiene nada bueno) —
# pedido explícito, "combinadas": iTunes primero (oficial, sin riesgo de derechos), Google
# como respaldo con disclaimer claro en la UI de que NO es oficial y es responsabilidad del
# bar. Requiere que el usuario registre su propia Google Programmable Search Engine (búsqueda
# de imágenes activada) + una API key — sin eso, esta función simplemente no devuelve nada
# (las sugerencias de iTunes solas siguen funcionando igual). Cuota gratis: 100/día; de ahí en
# adelante Google cobra por consulta.
GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX", "")

def google_image_suggestions(query, limit=6):
    if not (GOOGLE_CSE_API_KEY and GOOGLE_CSE_CX) or not query:
        return []
    try:
        url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode({
            "key": GOOGLE_CSE_API_KEY, "cx": GOOGLE_CSE_CX, "q": query,
            "searchType": "image", "num": min(limit, 10), "safe": "active", "imgSize": "medium",
        })
        d = json.loads(urllib.request.urlopen(url, timeout=6).read())
        out = []
        for item in d.get("items", []):
            link = item.get("link")
            if link:
                out.append({"cover": link, "source": "google", "label": str(item.get("title", ""))[:80]})
        return out
    except Exception:
        return []

# ---- Identificación por audio (AudD) — SOLO última instancia manual, nunca automática/masiva.
# Pruebas reales (2026-07-20, 50 archivos) confirmaron 100% de respuesta pero con errores de
# identificación reales (sobre todo en vallenato) y género propio demasiado genérico — por eso
# el resultado se muestra como preview y el admin debe confirmar antes de aplicarlo (ver UI en
# admin.html). Token vía variable de entorno para poder rotarlo a otra cuenta (otras 300
# gratis) sin tocar código mientras se valida si vale la pena pagar el plan comercial.
AUDD_API_TOKEN = os.environ.get("AUDD_API_TOKEN", "")
AUDD_MAX_BYTES = 15 * 1024 * 1024

def _multipart_encode(fields, files):
    """Codifica multipart/form-data a mano (sin `requests`, todo el proyecto usa urllib).
    fields: {name: str}. files: {name: (filename, bytes, content_type)}."""
    boundary = "----TYMAudd" + secrets.token_hex(16)
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    for name, (filename, data, ctype) in files.items():
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
             f'Content-Type: {ctype}\r\n\r\n').encode("utf-8") + data + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"

def identify_audio_bytes(audio_bytes, filename):
    """Identifica un clip de audio vía AudD, con caché compartida (todos los bares) por
    sha256 exacto del archivo — pedido explícito: 'nuestra propia huella musical antes de ir
    hasta AudD', para no gastar cuota en un mp3 que otro bar ya identificó. Devuelve
    {"ok":True,"artist":...,"title":...,"cached":bool} o {"ok":False,"error":...}."""
    digest = hashlib.sha256(audio_bytes).hexdigest()
    cached = TYM["audd_cache"].get(digest)
    if cached:
        return {"ok": True, "artist": cached["artist"], "title": cached["title"], "cached": True}
    if not AUDD_API_TOKEN:
        return {"ok": False, "error": "AudD no configurado (falta AUDD_API_TOKEN)"}
    try:
        body, content_type = _multipart_encode(
            {"api_token": AUDD_API_TOKEN, "return": "apple_music"},
            {"file": (filename or "clip.mp3", audio_bytes, "application/octet-stream")},
        )
        req = urllib.request.Request(
            "https://api.audd.io/", data=body, headers={"Content-Type": content_type}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=25).read())
    except Exception as e:
        return {"ok": False, "error": f"AudD no respondió ({e})"}
    result = resp.get("result") if resp.get("status") == "success" else None
    if not result or not (result.get("artist") or result.get("title")):
        return {"ok": False, "error": "No se pudo identificar la canción"}
    artist, title = result.get("artist") or "", result.get("title") or ""
    TYM["audd_cache"][digest] = {"artist": artist, "title": title, "ts": time.time()}
    return {"ok": True, "artist": artist, "title": title, "cached": False}

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
    """El PIN principal de la mesa y cualquier código extra emitido para una persona de esa
    mesa (ver settings de Mesas → "+ Código") identifican la MISMA mesa para cobro/límites —
    solo dan una identidad de sesión distinta para que un código filtrado no comprometa a
    todos los que comparten mesa."""
    pin = (pin or "").strip()
    for t in STATE["tables"]:
        if t["pin"] == pin or pin in t.get("extra_pins", []):
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
# cada compra pagada (salto o prioridad de un solo uso) de la MISMA mesa dentro de una
# ventana de tiempo encarece la siguiente: 1x la primera, 2x la segunda, 3x de ahí en
# adelante. Pase de tiempo y créditos ya prepagados no cuentan — solo compras con cobro nuevo.
# Pedido explícito: "Prioridad" (⚡, cola normal) y "Al frente" (⏫/salto, #1 inmediato) tienen
# cada una su propio contador Y su propia ventana configurable (antes compartían uno solo fijo
# en 60 min — pagar un salto subía el precio de la siguiente prioridad normal, y viceversa,
# aunque fueran compras de tipo distinto). `kind` es "prio" o "jump"; la escala 1x/2x/3x se
# queda fija para ambos, solo la ventana de tiempo es configurable por separado (Ajustes).
def _abuse_key(kind):
    return "priority_abuse" if kind == "prio" else "jump_abuse"

def _abuse_window_secs(kind):
    field = "priority_abuse_window_min" if kind == "prio" else "jump_abuse_window_min"
    return max(1, int(STATE["settings"].get(field, 60) or 60)) * 60

def priority_abuse_multiplier(table, now, kind):
    hist = STATE.setdefault(_abuse_key(kind), {})
    window = _abuse_window_secs(kind)
    times = [t for t in hist.get(table, []) if now - t < window]
    hist[table] = times
    n = len(times)
    return 1 if n == 0 else (2 if n == 1 else 3)

def record_priority_purchase(table, now, kind):
    hist = STATE.setdefault(_abuse_key(kind), {})
    hist.setdefault(table, []).append(now)

def priority_abuse_preview(table, now, kind):
    """Como priority_abuse_multiplier, pero de solo lectura y pensado para mostrarle el
    precio real al cliente ANTES de pagar (bug reportado: antes solo se enteraba del x2/x3
    en un toast DESPUÉS del cobro). Devuelve (mult, reset_at) — reset_at es el momento en
    que el multiplicador baja un escalón (None si ya está en x1). Debe reflejar exactamente
    la misma fórmula que /api/request usa al cobrar de verdad."""
    mult = priority_abuse_multiplier(table, now, kind)
    window = _abuse_window_secs(kind)
    times = sorted(STATE.get(_abuse_key(kind), {}).get(table, []))
    reset_at = None
    if mult == 2 and times:
        reset_at = times[0] + window
    elif mult == 3 and len(times) >= 2:
        reset_at = times[-2] + window
    return mult, reset_at

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
            # Salto manual de una canción PAGADA que no llegó al 80% de su duración: se
            # reembolsa — el cliente no paga por algo que el bar decidió cortar. El skip por
            # votación NUNCA llega hasta acá con una canción pagada (se bloquea antes, en
            # /api/skip_vote), así que esto solo puede dispararse desde el botón ⏭ del admin.
            was_paid = old.get("charge_on_play", 0) > 0 or old.get("paid_amount", 0) > 0
            if manual and was_paid and not old.get("refunded"):
                dur = old.get("duration") or DEFAULT_DUR
                if dur > 0 and (old.get("position", 0) / dur) < 0.8:
                    refund_song_charge(old)
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
        # Nunca silencio: fallback de la lista del local (shuffle o secuencial).
        # En modo catálogo local (content_mode=="local") NUNCA se cae a CATALOG (100%
        # YouTube) — un venue local se queda sin música de fondo (now_playing=None, "Esperando
        # canciones…") antes que mezclar YouTube en un local que eligió no usarlo. Además se
        # filtra `curated` a solo entradas locales: un admin pudo haber agregado antes una
        # canción de YouTube vía el buscador normal (esa ruta no cambia en esta fase), y esa
        # entrada no debe colarse en la rotación de un venue que ya está en modo local.
        if STATE["settings"].get("content_mode") == "local":
            # missing: el último re-escaneo no encontró el archivo en disco. excluded: el admin
            # la descartó a mano (✕ en el catálogo) — ninguna de las dos debe sonar de fondo.
            local_pool = [c for c in STATE["curated"]
                          if is_local_id(c.get("yt")) and not c.get("missing") and not c.get("excluded")]
            # "featured" (pedido explícito): estar en el catálogo completo (pedible por
            # clientes) es distinto de estar en la lista de fondo — un admin puede elegir un
            # subconjunto, igual que "Recomendadas del local" ya funciona en modo YouTube.
            # Mientras nadie haya marcado ninguna, sigue sonando el catálogo entero (nunca
            # silencio, ni tampoco un cambio de comportamiento para un venue que ya estaba en
            # producción antes de que existiera este flag).
            featured_pool = [c for c in local_pool if c.get("featured")]
            base_pool = featured_pool if featured_pool else local_pool
        else:
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
            "duration": s.get("duration", DEFAULT_DUR), "position": 0,
            "media_type": s.get("media_type"), "local_path": s.get("local_path"),
            "genre": s.get("genre"), "cover": s.get("cover")} if s else None)
    return STATE["now_playing"]

def in_play_or_queue(yt):
    if STATE["now_playing"] and STATE["now_playing"].get("yt") == yt:
        return True
    return any(i["yt"] == yt for i in STATE["items"])

def repeat_block_info(yt, now):
    """None si se puede pedir. Si no, {"reason":"songs"/"min", "available_at": ts|None} —
    "songs" no tiene ETA fija (depende de cuántas canciones más suenen, no del reloj);
    "min" sí, se usa para mostrar cuenta regresiva en "Próximamente" en el cliente."""
    if yt in STATE.get("repeat_exceptions", set()):
        return None
    s = STATE["settings"]
    n = max(0, int(s.get("repeat_block_songs", 3)))
    if n and any(h.get("yt") == yt for h in STATE["history"][:n]):
        return {"reason": "songs", "available_at": None}
    mins = int(s.get("repeat_block_min", 0))
    if mins > 0:
        cutoff = now - mins * 60
        latest = None
        for h in STATE["history"]:
            if h.get("yt") == yt:
                t = h.get("played_at", h.get("ts", 0))
                if t >= cutoff and (latest is None or t > latest):
                    latest = t
        if latest is not None:
            return {"reason": "min", "available_at": latest + mins * 60}
    return None

def repeat_block_reason(yt, now):
    info = repeat_block_info(yt, now)
    return info["reason"] if info else None

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
    # Filtro de salida (no solo de entrada): cubre cola, now_playing e historial sin importar
    # por dónde haya entrado el título (búsqueda, link pegado, o datos viejos guardados antes
    # de que existiera _clean_title_display) — barato e idempotente, seguro correrlo siempre.
    return {"id": it["id"], "title": _clean_title_display(it["title"]), "artist": it.get("artist", ""),
            "yt": it["yt"], "table": it.get("table", ""), "priority": it.get("priority", False),
            "super": it.get("super", False),
            "duration": it.get("duration", DEFAULT_DUR), "mine": bool(token) and it.get("token") == token,
            "play_status": it.get("play_status", "pending"),
            "requeue_count": it.get("requeue_count", 0),
            "ts": it.get("ts"), "played_at": it.get("played_at"),
            "media_type": it.get("media_type"), "local_path": it.get("local_path"),
            "cover": it.get("cover"),
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
        np_pub = {"id": np["id"], "title": _clean_title_display(np["title"]), "artist": np.get("artist", ""), "yt": np["yt"],
                  "table": np.get("table", ""), "priority": np.get("priority", False),
                  "fallback": np.get("fallback", False), "mine": bool(token) and np.get("token") == token,
                  "paid": np.get("charge_on_play", 0) > 0 or np.get("paid_amount", 0) > 0,
                  "duration": np.get("duration", DEFAULT_DUR), "position": np.get("position", 0),
                  "media_type": np.get("media_type"), "local_path": np.get("local_path"),
                  "genre": np.get("genre"), "cover": np.get("cover"),
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
            a = agg.setdefault(it["yt"], {"yt": it["yt"], "title": _clean_title_display(it["title"]),
                                          "artist": it.get("artist", ""), "total": 0, "tables": [],
                                          "media_type": it.get("media_type"), "cover": it.get("cover")})
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
            req_songs[yt] = {"yt": yt, "title": _clean_title_display(e.get("title") or "?"), "artist": e.get("artist", ""),
                              "count": 0, "media_type": e.get("media_type"), "cover": e.get("cover")}
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
        "boot_id": BOOT_ID,
        "settings": dict({k: s.get(k) for k in ("venue_name", "price_priority", "style", "auto_approve",
                                       "genre", "credit_packages", "time_pass",
                                       "repeat_block_min", "repeat_block_songs", "trim_end_secs",
                                       "free_per_window", "free_window_min", "jump_multiplier",
                                       "priority_abuse_window_min", "jump_abuse_window_min",
                                       "venue_logo", "max_priority_queue_min", "max_song_duration_min", "fallback_shuffle",
                                       "theme", "blocked_keywords", "allowed_keywords",
                                       "allow_skip_vote", "poll_duration_secs", "duelo_duration_secs",
                                       "schedule", "timezone", "prepaid_mode", "min_direct_pay",
                                       "show_tym_brand", "music_only", "content_mode",
                                       "song_message_moderation", "dedica_price", "dedica_display_secs",
                                       "dedica_presets", "allow_self_react")},
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
        "new_local_count": new_local_count(time.time()) if s.get("content_mode") == "local" else 0,
        "assists": [a for a in STATE.get("assists", []) if not a.get("resolved")],
        "queue": qout,
        "queue_count": len(q),
        "queue_total_secs": int(acc),
        "now_remaining_secs": int(rem),
        "my_pos": my_pos,
        "my_wait_secs": my_wait,
        "history": history,
        "top_loved": top_loved,
        "loved_celebration": STATE.get("loved_celebration"),
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
        # Precio real de prioridad/salto AHORA MISMO, con el multiplicador anti-abuso ya
        # aplicado — antes el cliente solo se enteraba del x2/x3 en un toast DESPUÉS de que
        # ya lo habían cobrado. Misma fórmula que /api/request usa al cobrar de verdad.
        _prio_mult, _prio_reset_at = priority_abuse_preview(tbl, time.time(), "prio")
        _jump_mult, _jump_reset_at = priority_abuse_preview(tbl, time.time(), "jump")
        out["priority_price_now"] = s["price_priority"] * _prio_mult
        out["jump_price_now"] = s["price_priority"] * s.get("jump_multiplier", 3) * _jump_mult
        out["priority_abuse_mult_now"] = _prio_mult
        out["priority_abuse_reset_at"] = _prio_reset_at
        out["jump_abuse_mult_now"] = _jump_mult
        out["jump_abuse_reset_at"] = _jump_reset_at
        if STATE["settings"].get("prepaid_mode"):
            customer = get_customer(sess)
            out["wallet_balance"] = customer.get("balance", 0) if customer else 0
            out["wallet_history"] = customer.get("wallet_history", [])[:20] if customer else []
        # total de reacciones a las canciones que pidió esta sesión (para avisar al autor) —
        # excluye SIEMPRE tus propias reacciones a tu propia canción (relevante si el admin
        # habilitó allow_self_react; si no, mine siempre viene vacío y esto no cambia nada).
        likes = 0
        for it in (([np] if np else []) + STATE["items"] + STATE["history"]):
            if it.get("token") == token:
                _counts, _mine, _total = react_counts(it["id"], token)
                likes += _total - len(_mine)
        out["my_likes_total"] = likes
        # canciones que este usuario ha reaccionado (para "Mis likes" en social)
        my_liked = []
        for it in (([np] if np else []) + STATE["items"] + STATE["history"]):
            _, my_r, _ = react_counts(it["id"], token)
            if my_r:
                my_liked.append({"id": it["id"], "yt": it["yt"], "title": it["title"],
                                 "artist": it.get("artist", ""), "my_reacts": my_r,
                                 "media_type": it.get("media_type"), "cover": it.get("cover")})
        out["my_liked"] = my_liked[:20]
    if admin:
        out["pending"] = [public_item(i, token) for i in pending_view()]
        out["ledger"] = list(reversed(STATE["ledger"]))[:40]
        out["ledger_total"] = sum(l["amount"] for l in STATE["ledger"])
        out["curated"] = [{**c, "title": _clean_title_display(c["title"])} for c in STATE["curated"]]
        out["subscribers"] = list(reversed(TYM["subscribers"]))[:100]
        out["history"] = [{"id": h["id"], "yt": h["yt"], "title": _clean_title_display(h["title"]),
                           "artist": h.get("artist", ""),
                           "ts": h.get("ts"), "played_at": h.get("played_at")} for h in STATE["history"][:10]]
        out["repeat_exceptions"] = list(STATE.get("repeat_exceptions", set()))
        out["all_dedicas"] = list(reversed(STATE.get("dedicas", [])))[:30]
        out["dedica_codes"] = list(reversed(STATE.get("dedica_codes", [])))[:15]
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
    """Rellena con el catálogo (preferentemente del género) hasta n, sin duplicar.
    En modo catálogo local nunca rellena con CATALOG (100% YouTube) — mejor una lista corta
    (o vacía) que mezclar YouTube en un local que eligió no usarlo."""
    if STATE["settings"].get("content_mode") == "local":
        return lst
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
    mas = [{"yt": c["yt"], "title": _clean_title_display(c["title"]), "artist": c["artist"]} for c in counts]
    if not mas:
        mas = [{"yt": x["yt"], "title": _clean_title_display(x["title"]), "artist": x.get("artist", "")} for x in STATE["curated"][:8]]
    local = [{"yt": x["yt"], "title": _clean_title_display(x["title"]), "artist": x.get("artist", "")} for x in STATE["curated"]]
    # Populares: canciones del historial del local (lo que ha sonado aquí)
    seen = set()
    populares = []
    for h in STATE.get("history", []):
        if not h.get("fallback") and h.get("yt") and h["yt"] not in seen:
            seen.add(h["yt"])
            populares.append({"yt": h["yt"], "title": _clean_title_display(h["title"]), "artist": h.get("artist", "")})
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

    def _client_ip(self):
        """IP real del cliente para rate limiting.

        En Render (runtime nativo, sin Docker/TCP passthrough) las conexiones le
        llegan a este proceso desde el proxy HTTP interno de Render, no desde el
        navegador — self.client_address[0] es SIEMPRE la IP del proxy, la misma
        para todo el tráfico de todos los bares. Sin leer X-Forwarded-For, todos
        los límites de _rate_ok() terminaban compartidos entre TODOS los clientes
        de TODOS los venues en vez de ser por-cliente (encontrado investigando un
        reporte de "Por si te gustó no carga en Safari, sí en Chrome, mismo
        celular": Chrome agotaba el cupo de "search" y Safari, con la misma IP de
        proxy, heredaba el bloqueo).

        Usamos el ÚLTIMO valor de X-Forwarded-For, no el primero: cualquier
        cliente puede mandar ese header con lo que quiera, así que el primer
        valor NO es confiable (dejaría bypassear el rate limit rotando un valor
        falso en cada request). El único hop que no se puede falsificar es el
        que agrega el propio proxy de Render justo antes de reenviar — ese
        siempre queda al final de la cadena.
        """
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            last = xff.split(",")[-1].strip()
            if last:
                return last
        return self.client_address[0]

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
            ip = self._client_ip()
            if not _rate_ok(ip, "search"):
                return self._send(429, {"error": "Demasiadas búsquedas. Espera un momento."})
            q = self._q("q")[:150]
            results = yt_search(q)
            now = time.time()
            # yt_search() cachea la lista en memoria compartida entre TODOS los bares (por
            # texto de búsqueda) — nunca hay que mutar esos dicts directamente, o el estado de
            # bloqueo de un bar se filtraría al resultado cacheado de otro bar. Se arma una
            # copia nueva por request con el bloqueo de ESTE venue.
            with LOCK:
                self.set_venue(self.resolve_vid())
                out = []
                for r in results:
                    info = repeat_block_info(r.get("yt"), now)
                    item = dict(r)
                    item["blocked"] = bool(info)
                    item["block_reason"] = (info or {}).get("reason")
                    item["block_available_at"] = (info or {}).get("available_at")
                    out.append(item)
            return self._send(200, out)
        if path == "/api/search_local":
            # Fase 3 del modo catálogo local (ver plan federated-knitting-lagoon.md) — busca por
            # texto sobre STATE["curated"] filtrado a entradas locales, sin llamar nunca a
            # YouTube.
            ip = self._client_ip()
            if not _rate_ok(ip, "search"):
                return self._send(429, {"error": "Demasiadas búsquedas. Espera un momento."})
            q = self._q("q")[:150].strip().lower()
            # genre: pedido explícito — "Por si te gustó" en modo local debe sugerir por mismo
            # artista/género igual que ya funciona en YouTube (ahí usa /api/search por artista +
            # /api/genre de iTunes) — acá el género ya viene del propio catálogo, sin llamar a
            # nada externo. Filtro aparte de `q` (no se mezclan) para no dar falsos positivos si
            # el nombre del género aparece suelto en un título.
            genre_q = self._q("genre")[:40].strip().lower()
            only_new = self._q("new") == "1"
            with LOCK:
                self.set_venue(self.resolve_vid())
                # missing: archivo que el último re-escaneo no encontró en disco. excluded:
                # el admin la descartó a mano (✕ en el catálogo) — ninguna de las dos se le
                # ofrece a los clientes.
                base = [c for c in STATE["curated"]
                        if is_local_id(c.get("yt")) and not c.get("missing") and not c.get("excluded")]
                if only_new:
                    # "🆕 Nuevas" (ver new_local_count en public_state) — ignora q/genre, es su
                    # propia vista dedicada. Más reciente primero.
                    _cutoff = time.time() - NEW_SONG_WINDOW_SECS
                    pool = sorted([c for c in base if c.get("added_at") and c["added_at"] >= _cutoff],
                                  key=lambda c: -c["added_at"])
                elif genre_q:
                    gf = _fold(genre_q)
                    pool = [c for c in base if gf in _fold(c.get("genre") or "")]
                elif q:
                    # Con texto, se busca en TODO el catálogo — el cliente tiene que poder pedir
                    # cualquier cosa que el bar tenga, esté o no destacada. _fold() tolera
                    # tildes/mayúsculas; si el substring exacto no encuentra nada, se reintenta
                    # con tolerancia a palabras mal escritas (_fuzzy_local_search).
                    qf = _fold(q)
                    pool = [c for c in base if qf in _fold(c["title"] + " " + (c.get("artist") or ""))]
                    if not pool:
                        pool = _fuzzy_local_search(base, qf)
                else:
                    # Sin texto (pantalla de "Pedir" recién abierta): pedido explícito — lo que
                    # se sugiere por defecto debe ser lo que el bar destacó (featured, ver
                    # "Recomendadas del local" en /admin cuando content_mode=="local"), no un
                    # corte arbitrario de todo el catálogo. Si nadie ha destacado nada todavía,
                    # se cae al catálogo completo — nunca una pantalla vacía.
                    featured = [c for c in base if c.get("featured")]
                    pool = featured if featured else base
                now = time.time()
                _new_cutoff = now - NEW_SONG_WINDOW_SECS
                out = []
                for c in pool[:40]:
                    info = repeat_block_info(c["yt"], now)
                    added_at = c.get("added_at")
                    out.append({"yt": c["yt"], "title": c["title"], "artist": c.get("artist", ""),
                                "duration": c.get("duration") or DEFAULT_DUR,
                                "media_type": c.get("media_type"), "cover": c.get("cover"),
                                "featured": bool(c.get("featured")),
                                "blocked": bool(info),
                                "block_reason": (info or {}).get("reason"),
                                "block_available_at": (info or {}).get("available_at"),
                                "added_at": added_at,
                                "is_new": bool(added_at and added_at >= _new_cutoff)})
            return self._send(200, out)
        if path == "/api/genre":
            ip = self._client_ip()
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
                log = [dict(e, title=_clean_title_display(e.get("title", "")))
                       for e in VENUES[av].get("request_log", []) if e["ts"] >= cutoff]
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
                    log = [dict(e, venue=vid, venue_name=venue["settings"]["venue_name"],
                                title=_clean_title_display(e.get("title", "")))
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

    def _handle_identify_audio(self):
        """Identificación manual de audio (AudD) — último recurso, un archivo a la vez, nunca
        automático/masivo (pedido explícito). El cuerpo es el audio crudo (no JSON); el nombre
        del archivo va en el query string (?filename=...) para no mezclarse con el binario."""
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not self.authed_venue():
            if n:
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            return self._send(401, {"error": "No autorizado"})
        if not n or n > AUDD_MAX_BYTES:
            if n:
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            return self._send(400, {"error": "Archivo vacío o demasiado grande (máx 15MB)"})
        audio_bytes = self.rfile.read(n)
        qs = parse_qs(urlparse(self.path).query)
        filename = (qs.get("filename") or ["clip.mp3"])[0][:200]
        result = identify_audio_bytes(audio_bytes, filename)
        if not result["ok"]:
            return self._send(200, {"ok": False, "error": result["error"]})
        if not result["cached"]:
            save_state()
        return self._send(200, result)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        # 3MB: acota el abuso (no es una API de subida de archivos genérica) pero alcanza
        # para un logo real de cámara/diseño sin que el admin tenga que redimensionarlo a
        # mano — antes el límite era 64KB y CUALQUIER request más grande (ej. subir un logo
        # de unos cientos de KB) devolvía {} en silencio: el POST respondía 200 OK pero no
        # cambiaba nada, sin ningún error visible. Bug reportado en vivo.
        MAX_BODY = 3_000_000
        if n > MAX_BODY:
            # Antes solo se leían 128KB fijos del body para "no romper la conexión" — con un
            # body de verdad grande eso dejaba el resto sin leer en el socket, corrompiendo
            # el siguiente request en la misma conexión keep-alive. Ahora se drena TODO.
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        path = urlparse(self.path).path
        # Intercepta ANTES de _body(): ese método asume JSON y acota a 3MB, pero acá llega el
        # audio crudo (binario, puede pesar varios MB) — necesita su propio límite y su propio
        # drenado del socket en caso de rechazo (mismo motivo que el comentario en _body()).
        if path == "/api/admin/identify_audio":
            return self._handle_identify_audio()
        d = self._body()
        # ---- Login / logout (dueños TYM) ----
        if path == "/api/login":
            ip = self._client_ip()
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
            save_state()   # AUTH sobrevive a un redeploy (ver save_state/load_state) — si no,
                            # cada reinicio del proceso desloguea /admin y /tv sin avisar
            body = json.dumps({"ok": True, "venue": o["venue"]}).encode("utf-8")
            secure_flag = "; Secure" if PUBLIC_URL.startswith("https://") else ""
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"tymauth={tk}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400{secure_flag}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/api/logout":
            AUTH.pop(self.get_cookie("tymauth"), None)
            save_state()
            return self._send(200, {"ok": True})
        if path == "/api/forgot_password":
            ip = self._client_ip()
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
                       "/api/admin/dedica_presets", "/api/admin/dedica_codes",
                       "/api/admin/song_message/approve", "/api/admin/song_message/reject",
                       "/api/admin/change_password", "/api/admin/update_email",
                       "/api/admin/local_catalog", "/api/admin/classify_track", "/api/admin/cover_suggestions")
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
                if not _rate_ok(self._client_ip(), "session"):
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
                if not _rate_ok(self._client_ip(), "session"):
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
                if not _rate_ok(self._client_ip(), "session"):
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
                if not _rate_ok(self._client_ip(), "request"):
                    return self._send(429, {"error": "Demasiadas solicitudes. Espera un momento."})
                sess = get_session(d.get("token"))
                if not sess:
                    return self._send(400, {"error": "Ingresa el código de tu mesa para pedir."})
                table = sess["table"]
                sup = bool(d.get("super"))
                priority = bool(d.get("priority")) or sup
                dur = _parse_len(d.get("length")) or int(d.get("duration") or 0) or DEFAULT_DUR
                # yt/title/artist ya vienen resueltos si el pedido fue por link (se resolvió
                # antes de tomar el LOCK — ver do_POST, para no bloquear el servidor con I/O de red)
                yt = d.get("yt")
                if not yt or not (YT_ID_RE.match(yt) or is_local_id(yt)):
                    return self._send(400, {"error": "No pude leer el link de YouTube"})
                # En modo catálogo local, un id de YouTube se rechaza acá — es el único punto
                # por el que TODO pedido pasa (cliente, link pegado, o el admin arrastrando algo
                # de "Recomendadas del local" a la cola en vivo), así que cierra la puerta sin
                # importar por dónde se intente. Antes solo el buscador del cliente evitaba
                # mostrar YouTube (Fase 3) — pero nada bloqueaba que igual se colara un pedido
                # real, bug reportado en vivo con un local real.
                if STATE["settings"].get("content_mode") == "local" and not is_local_id(yt):
                    return self._send(400, {"error": "Este local solo pide música de su catálogo propio — busca en 'Del local' 🎵",
                                            "content_blocked": True})
                title = str(d.get("title") or "Canción")[:200]
                artist = str(d.get("artist") or "")[:120]
                # Canción local: la duración/tipo de archivo/ruta salen del catálogo curado por
                # el admin (fuente de verdad), no de lo que mande el cliente — Fase 4 del modo
                # sin YouTube (ver plan federated-knitting-lagoon.md), necesarios para que /tv
                # sepa qué archivo abrir y si mostrar la vista de audio o de video. Se resuelve
                # ANTES del chequeo de duración máxima para que aplique sobre el valor real.
                local_media_type, local_path, local_genre, local_cover = None, None, None, None
                if is_local_id(yt):
                    _cur_entry = next((c for c in STATE["curated"] if c["yt"] == yt), None)
                    if not _cur_entry:
                        return self._send(400, {"error": "Esa canción ya no está en el catálogo del local — puede que la hayan quitado."})
                    if _cur_entry.get("missing"):
                        return self._send(400, {"error": "Ese archivo ya no está en la carpeta del local — puede que se haya movido o borrado."})
                    if _cur_entry.get("excluded"):
                        return self._send(400, {"error": "Esa canción fue descartada del catálogo por el local."})
                    dur = _cur_entry.get("duration") or DEFAULT_DUR
                    local_media_type = _cur_entry.get("media_type")
                    local_path = _cur_entry.get("local_path")
                    # género del catálogo (pedido explícito: "Por si te gustó" en modo local debe
                    # sugerir por artista/género igual que en YouTube, sin llamar a iTunes de
                    # nuevo — el catálogo ya trae el género que el admin clasificó).
                    local_genre = _cur_entry.get("genre")
                    # carátula real (iTunes, ver itunes_cover) — pedido explícito: mostrarla en
                    # /tv junto al fondo animado de la vista de solo-audio, cuando exista.
                    local_cover = _cur_entry.get("cover")
                _max_dur_min = int(STATE["settings"].get("max_song_duration_min", 0) or 0)
                if _max_dur_min > 0 and dur > _max_dur_min * 60:
                    return self._send(400, {"error": f"Esta canción dura más de {_max_dur_min} min, el máximo permitido en este local 🎵",
                                            "too_long": True})
                req_msg = (d.get("message") or "").strip()[:80]
                now = time.time()
                # Dedicatoria al pedir: mismo modelo anti-abuso que /api/dedica (mesa a mesa) —
                # un mensaje predeterminado del bar (settings.dedica_presets) va directo, pero
                # CUALQUIER otro texto (antes se podía editar libremente el campo aunque el
                # cliente hubiera elegido un preset — bug reportado) necesita un código de un
                # solo uso que el admin genera desde /admin. El código ES la moderación acá,
                # igual que en /api/dedica — reemplaza el filtro heurístico para texto libre.
                dedica_code_entry = None
                if req_msg and req_msg not in STATE["settings"].get("dedica_presets", []):
                    _code = str(d.get("dedica_code") or "").strip()
                    dedica_code_entry = next((c for c in STATE.get("dedica_codes", [])
                                              if c["code"] == _code and not c["used"]), None)
                    if not dedica_code_entry:
                        return self._send(400, {"error": "Ese mensaje no es uno de los predeterminados del bar. "
                                                 "Pídele al mesero o al admin un código para enviarlo.",
                                                 "needs_code": True})
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
                # "salto" (Al frente) y "single" (Prioridad) tienen cada uno su propio contador
                # (pedido explícito — antes compartían uno solo).
                abuse_kind = "jump" if mode == "salto" else "prio"
                abuse_mult = priority_abuse_multiplier(table, now, abuse_kind) if charge > 0 else 1
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
                    record_priority_purchase(table, now, abuse_kind)
                # Punto de no retorno para el mensaje: recién acá se consume el código (si el
                # mensaje era texto libre) — no antes, para no gastarlo si el pedido termina
                # rechazado por otra razón (canción bloqueada, etc). Un código consumido
                # reemplaza la moderación heurística, igual que en /api/dedica.
                if dedica_code_entry:
                    dedica_code_entry["used"] = True
                    dedica_code_entry["used_at"] = now
                    dedica_code_entry["used_by_table"] = table
                    msg_status, _msg_reason = "approved", ""
                elif req_msg and STATE["settings"].get("song_message_moderation"):
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
                        "message": req_msg, "message_status": msg_status, "message_mod_reason": _msg_reason,
                        "media_type": local_media_type, "local_path": local_path, "genre": local_genre,
                        "cover": local_cover}
                STATE["items"].append(item)
                bump_count(yt, title, artist)
                log_order(table, d.get("token"), mode, title, yt)   # analítica (free/premium)
                # Log de pedidos (3 días)
                _now = time.time()
                STATE["request_log"].append({
                    "ts": _now, "title": title, "artist": artist or "",
                    "yt": yt, "table": table, "mode": mode,
                    "priority": priority, "charge": charge,
                    "media_type": item.get("media_type"), "cover": item.get("cover"),
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
                if not _rate_ok(self._client_ip(), "social"):
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
                # Anti-autolike: por defecto quien pidió la canción no puede reaccionarle a la
                # suya propia — bloqueado, no silencioso (settings.allow_self_react, apagado por
                # defecto, lo habilita el admin). "my_likes_total" (el aviso "a N les gusta tu
                # música") SIEMPRE excluye tus propias reacciones a tu propia canción, esté
                # permitido el autolike o no — ver public_state(), si no se inflaría haciéndole
                # creer al cliente que OTRAS personas reaccionaron cuando se dio like a sí mismo.
                _np_r = STATE["now_playing"]
                _react_item = next((it for it in (([_np_r] if _np_r else []) + STATE["items"] + STATE["history"])
                                     if it["id"] == item_id), None)
                if (_react_item and _react_item.get("token") == tok
                        and not STATE["settings"].get("allow_self_react")):
                    return self._send(400, {"error": "No puedes reaccionar a tu propia canción.", "self_react_blocked": True})
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
                    # Celebración "entró al top de hoy": si esta reacción hace que el total
                    # acumulado de la canción (sumado entre todas sus veces sonada hoy —
                    # now_playing+cola+historial, igual que el ranking "Lo más querido") cruce
                    # el umbral por primera vez, se dispara la animación en el TV. Una sola vez
                    # por canción por noche (celebrated_loved).
                    if _react_item and _react_item["yt"] not in STATE["celebrated_loved"]:
                        yt = _react_item["yt"]
                        yt_total = sum(
                            react_counts(it["id"])[2]
                            for it in (([_np_r] if _np_r else []) + STATE["items"] + STATE["history"])
                            if it["yt"] == yt
                        )
                        if yt_total >= LOVED_CELEBRATION_THRESHOLD:
                            STATE["celebrated_loved"].add(yt)
                            STATE["loved_celebration"] = {
                                "yt": yt, "title": _clean_title_display(_react_item["title"]),
                                "artist": _react_item.get("artist", ""), "total": yt_total, "ts": now_ts,
                            }
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
                # "Impulsar" sube una canción a prioridad normal (⚡), no a "Al frente" —
                # cuenta contra el mismo contador anti-abuso que /api/request usa para "prio".
                abuse_mult = priority_abuse_multiplier(sess["table"], now, "prio") if price > 0 else 1
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
                    record_priority_purchase(sess["table"], now, "prio")
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
                          "priority_abuse_window_min", "jump_abuse_window_min",
                          "max_priority_queue_min", "max_song_duration_min",
                          "poll_duration_secs", "duelo_duration_secs", "dedica_price"):
                    if k in d:
                        try: s[k] = max(0, int(d[k]))
                        except Exception: pass
                if "dedica_display_secs" in d:
                    try: s["dedica_display_secs"] = max(2, min(30, int(d["dedica_display_secs"])))
                    except Exception: pass
                for k in ("fallback_shuffle", "prepaid_mode", "show_tym_brand", "allow_skip_vote", "music_only", "song_message_moderation", "allow_self_react"):
                    if k in d:
                        s[k] = bool(d[k])
                if "min_direct_pay" in d:
                    try: s["min_direct_pay"] = max(0, int(d["min_direct_pay"]))
                    except Exception: pass
                if "theme" in d and d["theme"] in ("azul", "purpura", "verde", "rojo", "dorado", "rosa"):
                    s["theme"] = d["theme"]
                if "content_mode" in d and d["content_mode"] in ("youtube", "local"):
                    _prev_content_mode = s.get("content_mode")
                    s["content_mode"] = d["content_mode"]
                    # Al pasar a modo local: una canción de YouTube que ya estaba en cola de
                    # ANTES de activar el modo seguía sonando igual (la cola siempre tiene
                    # prioridad sobre el fallback filtrado) — bug reportado en vivo, "sigue
                    # cayendo en YouTube" pese a que /api/request ya rechaza pedidos NUEVOS.
                    # Corte limpio: se saca de la cola todo lo que no sea local (reembolsando lo
                    # ya cobrado por adelantado, ej. saldo prepago) y si estaba sonando algo de
                    # YouTube en ese momento, se salta como un skip manual del admin (mismo
                    # camino ya construido: reembolsa si no llegó al 80%, archiva en historial).
                    if _prev_content_mode != "local" and s["content_mode"] == "local":
                        _kept_items = []
                        for _it in STATE["items"]:
                            if is_local_id(_it.get("yt")):
                                _kept_items.append(_it)
                            else:
                                refund_song_charge(_it)
                        STATE["items"] = _kept_items
                        _np = STATE.get("now_playing")
                        if _np and not is_local_id(_np.get("yt")):
                            promote_next(manual=True)
                if "timezone" in d and str(d["timezone"]) in available_timezones():
                    s["timezone"] = str(d["timezone"])
                for k in ("blocked_keywords", "allowed_keywords"):
                    if k in d and isinstance(d[k], list):
                        s[k] = [str(w).strip().lower()[:50] for w in d[k] if str(w).strip()][:30]
                if "venue_logo" in d:                       # logo del BAR
                    s["venue_logo"] = remove_solid_bg(str(d["venue_logo"]))[:700000]
                if "tym_logo" in d:                          # logo de TYM (global)
                    TYM["tym_logo"] = remove_solid_bg(str(d["tym_logo"]))[:700000]
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
                new_pin = None
                if act == "add":
                    name = d.get("name")
                    if not name:
                        # Reutiliza el primer número "Mesa N" libre (ej. si borraste la 3 de 7,
                        # la próxima mesa nueva vuelve a ser la 3, no la 8) en vez de usar
                        # len(tables)+1, que duplicaba nombres apenas había un hueco.
                        used = set()
                        for t in STATE["tables"]:
                            m = re.match(r"^Mesa (\d+)$", t["name"])
                            if m:
                                used.add(int(m.group(1)))
                        n = 1
                        while n in used:
                            n += 1
                        name = f"Mesa {n}"
                    STATE["tables"].append({"name": name, "pin": gen_unique_pin(), "extra_pins": []})
                elif act == "remove":
                    name = d.get("name")
                    tab_total = sum(l["amount"] for l in STATE["ledger"] if l["table"] == name)
                    if tab_total > 0:
                        amount_fmt = f"{tab_total:,}".replace(",", ".")
                        return self._send(400, {"error": f"{name} tiene una cuenta abierta (${amount_fmt}) — "
                                                 "ciérrala primero desde Caja o Mesas antes de borrarla."})
                    STATE["tables"] = [t for t in STATE["tables"] if t["name"] != name]
                elif act == "regen":
                    for t in STATE["tables"]:
                        if t["name"] == d.get("name"):
                            t["pin"] = gen_unique_pin()
                elif act == "toggle_msg_block":
                    for t in STATE["tables"]:
                        if t["name"] == d.get("name"):
                            t["msg_blocked"] = not t.get("msg_blocked", False)
                elif act == "add_pin":
                    # Código individual adicional para otra persona de la misma mesa — evita que
                    # todos tengan que compartir el mismo PIN (menos exposición si uno se filtra)
                    # sin tocar cobros/límites, que siguen siendo por mesa. Un click, sin formulario.
                    for t in STATE["tables"]:
                        if t["name"] == d.get("name"):
                            new_pin = gen_unique_pin()
                            t.setdefault("extra_pins", []).append(new_pin)
                            break
                elif act == "remove_pin":
                    for t in STATE["tables"]:
                        if t["name"] == d.get("name"):
                            t["extra_pins"] = [p for p in t.get("extra_pins", []) if p != d.get("pin")]
                            break
                return self._send(200, {"ok": True, "tables": STATE["tables"], "new_pin": new_pin})

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
                if act == "add" and d.get("yt") and (YT_ID_RE.match(d["yt"]) or is_local_id(d["yt"])):
                    if not any(c["yt"] == d["yt"] for c in STATE["curated"]):
                        # genre/media_type/local_path solo se llenan para entradas del catálogo
                        # local (Fase 2 en adelante los sube el importador) — quedan en None para
                        # una canción de YouTube agregada normal, como siempre.
                        STATE["curated"].append({"yt": d["yt"], "title": str(d.get("title") or "Canción")[:200],
                                                 "artist": str(d.get("artist") or "")[:120],
                                                 "duration": _parse_len(d.get("length")) or DEFAULT_DUR,
                                                 "genre": (str(d.get("genre"))[:40] if d.get("genre") else None),
                                                 "media_type": (d.get("media_type") if d.get("media_type") in ("audio", "video") else None),
                                                 "local_path": (str(d.get("local_path"))[:500] if d.get("local_path") else None),
                                                 "added_at": time.time()})
                elif act == "remove":
                    STATE["curated"] = [c for c in STATE["curated"] if c["yt"] != d.get("yt")]
                elif act == "remove_missing":
                    # Pedido explícito: sacar una por una las que ya no están en la carpeta (con
                    # confirm() individual cada vez) era demasiado manual si cambió la carpeta
                    # entera (ej. se renombró/movió) y de golpe hay decenas marcadas "missing" —
                    # esto las saca TODAS del catálogo en un solo paso, con una sola confirmación
                    # del lado del cliente. Nunca toca una entrada que no esté marcada missing en
                    # este momento (si el archivo reapareció justo antes de tocar el botón, se
                    # salva sola).
                    removed = sum(1 for c in STATE["curated"] if is_local_id(c.get("yt")) and c.get("missing"))
                    STATE["curated"] = [c for c in STATE["curated"]
                                        if not (is_local_id(c.get("yt")) and c.get("missing"))]
                    save_state()
                    return self._send(200, {"ok": True, "removed": removed, "curated": STATE["curated"]})
                elif act == "reorder":
                    order = d.get("order") or []
                    pos = {yt: i for i, yt in enumerate(order)}
                    STATE["curated"].sort(key=lambda c: pos.get(c["yt"], 999999))
                save_state()
                return self._send(200, {"ok": True, "curated": STATE["curated"]})

            if path == "/api/admin/local_catalog":
                # Fase 2 del modo catálogo local (ver plan federated-knitting-lagoon.md) — el
                # importador de /admin escanea una carpeta 100% del lado del navegador (nunca
                # sube el archivo, solo esta metadata de texto) y manda el lote acá.
                act = d.get("action")
                if act == "import":
                    tracks = d.get("tracks") or []
                    if not isinstance(tracks, list):
                        return self._send(400, {"error": "tracks debe ser una lista"})
                    # Fase 5: cuando el TV se auto-importa contenido nuevo que encontró en un
                    # re-escaneo (sin que el admin lo haya revisado), se marca auto_added para
                    # que /admin lo destaque como pendiente de revisar — nunca se toca este flag
                    # en una entrada YA existente (ni al admin re-escanear manualmente y toparse
                    # con algo que ya estaba, ni al TV): solo editar una entrada a mano (abajo)
                    # cuenta como "revisada".
                    auto = bool(d.get("auto"))
                    by_yt = {c["yt"]: c for c in STATE["curated"]}
                    added, updated = 0, 0
                    for t in tracks[:2000]:
                        if not isinstance(t, dict):
                            continue
                        path = str(t.get("path") or "").strip()[:500]
                        if not path:
                            continue
                        yt = "local:" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]
                        entry_title = str(t.get("title") or "Canción")[:200]
                        entry_artist = str(t.get("artist") or "")[:120]
                        entry_genre = str(t.get("genre"))[:40] if t.get("genre") else None
                        entry_cover = None
                        entry_sha = _norm_sha(t.get("sha256"))
                        # Base compartida entre bares: si este bar no trajo género (el escaneo del
                        # navegador no reconoció nada en el nombre/tags), primero se busca por
                        # huella EXACTA del archivo (más confiable — mismos bytes, no solo un
                        # título parecido) y si no hay match ahí, por título+artista normalizados.
                        # Ninguna llama a iTunes/Wikidata — el admin igual puede corregir a mano.
                        if not entry_genre:
                            hit = file_db_lookup(entry_sha) or track_db_lookup(entry_artist, entry_title)
                            if hit:
                                entry_genre = hit.get("genre") or entry_genre
                                entry_cover = hit.get("cover")
                        entry = {
                            "yt": yt, "title": entry_title, "artist": entry_artist,
                            "duration": int(t["duration"]) if str(t.get("duration") or "").isdigit() else DEFAULT_DUR,
                            "genre": entry_genre,
                            "media_type": (t.get("media_type") if t.get("media_type") in ("audio", "video") else None),
                            "local_path": path,
                        }
                        if entry_cover:
                            entry["cover"] = entry_cover
                        if entry_sha:
                            entry["sha256"] = entry_sha
                        # Contribuye a las dos bases compartidas (no verificado — un guess de
                        # escaneo nunca pisa un dato ya subido por otro bar).
                        track_db_contribute(entry_artist, entry_title, entry_genre, entry_cover)
                        file_db_contribute(entry_sha, entry_genre, entry_cover)
                        if yt in by_yt:
                            by_yt[yt].update(entry); updated += 1
                        else:
                            entry["auto_added"] = auto
                            # Solo en la rama de "nueva de verdad" — si esto viviera en el dict
                            # `entry` de arriba, un re-escaneo (TV o "Forzar revisión") pisaría
                            # added_at de canciones YA existentes en cada pasada, vía el
                            # by_yt[yt].update(entry) de la rama "updated" (ver "🆕 Nuevas" en
                            # el cliente, que se calcula a partir de este campo).
                            entry["added_at"] = time.time()
                            by_yt[yt] = entry; STATE["curated"].append(entry); added += 1
                    # Resincronización (pedido explícito: "en cada canción debería resincronizar
                    # el catálogo previniendo mostrar archivos que ya no existan"): quien escanea
                    # (tv.html en maybeRescanFolder, o admin.html al forzar revisión) ya recorre
                    # la carpeta entera cada vez — si manda TODAS las rutas que vio (no solo las
                    # nuevas), acá se puede marcar `missing` a cualquier entrada local cuya ruta
                    # ya no aparezca, y des-marcarla sola si vuelve a aparecer (ej. una carpeta de
                    # red que se desconectó un momento) — nunca se borra nada solo, igual que el
                    # resto de este importador.
                    seen_paths = d.get("seen_paths")
                    revived = missing_now = 0
                    if isinstance(seen_paths, list):
                        seen_set = {str(p)[:500] for p in seen_paths[:5000]}
                        for c in STATE["curated"]:
                            if not is_local_id(c.get("yt")) or not c.get("local_path"):
                                continue
                            now_missing = c["local_path"] not in seen_set
                            if c.get("missing", False) != now_missing:
                                c["missing"] = now_missing
                                if now_missing:
                                    missing_now += 1
                                else:
                                    revived += 1
                    # Bug real encontrado probando la carátula propia: NINGUNA acción de este
                    # endpoint (import/edit/upload_cover/remove_cover) guardaba el estado — un
                    # reinicio del proceso (redeploy, o el spin-down del free tier de Render tras
                    # 15 min sin uso) perdía en silencio cualquier edición del catálogo local que
                    # no coincidiera con otra ruta que sí guarde (ej. login). Corregido acá y en
                    # las otras 3 acciones de este mismo endpoint.
                    save_state()
                    return self._send(200, {"ok": True, "added": added, "updated": updated,
                                             "missing_now": missing_now, "revived": revived,
                                             "curated": STATE["curated"]})
                elif act == "edit":
                    yt = d.get("yt")
                    # Solo canciones locales por esta vía — el buscador de YouTube ya tiene su
                    # propio flujo de agregar y no necesita edición (el título/artista se saca
                    # del video real).
                    if not yt or not is_local_id(yt):
                        return self._send(400, {"error": "yt inválido"})
                    c = next((c for c in STATE["curated"] if c["yt"] == yt), None)
                    if not c:
                        return self._send(404, {"error": "No encontrada"})
                    c["auto_added"] = False  # editar a mano cuenta como revisada
                    if "title" in d:
                        c["title"] = str(d.get("title") or "Canción")[:200]
                    if "artist" in d:
                        c["artist"] = str(d.get("artist") or "")[:120]
                    if "genre" in d:
                        c["genre"] = str(d.get("genre"))[:40] if d.get("genre") else None
                    # Carátula real (iTunes, ver itunes_cover) — nunca un archivo subido, solo
                    # la URL que ya devuelve /api/admin/classify_track.
                    if "cover" in d:
                        cov = str(d.get("cover") or "")[:500]
                        c["cover"] = cov if cov.startswith("http") else None
                    # "Congelar" (pedido explícito): una vez el admin confirma que un título/
                    # artista/género quedó bien a mano, protege esa entrada de que el botón de
                    # iTunes la pise sin querer — el botón sigue funcionando, pero el cliente le
                    # muestra una confirmación extra si la entrada está congelada.
                    if "locked" in d:
                        c["locked"] = bool(d.get("locked"))
                    # "featured": pedido explícito — estar en el catálogo (orderable por
                    # clientes) es distinto de estar en la lista de fondo/destacadas que suena
                    # cuando nadie está pidiendo nada (antes TODO el catálogo sonaba de fondo
                    # sin poder elegir un subconjunto, igual que ya funciona en modo YouTube con
                    # "Recomendadas del local"). Ver promote_next(): mientras nadie haya marcado
                    # ninguna como featured, sigue sonando el catálogo completo — nunca silencio.
                    if "featured" in d:
                        c["featured"] = bool(d.get("featured"))
                    # "excluded" (pedido explícito): antes la ✕ borraba la entrada del todo, pero
                    # el archivo seguía en la carpeta — el siguiente re-escaneo (automático por
                    # canción, o "Forzar revisión") la volvía a importar como nueva, así que
                    # parecía que la ✕ "no funcionaba". Ahora la ✕ solo la descarta (se sigue
                    # viendo acá, desvanecida, con opción de restaurar) — al quedar la entrada
                    # en el catálogo, el re-escaneo ya no la trata como nueva. Se oculta de
                    # clientes y del fallback (ver /api/search_local y promote_next()).
                    if "excluded" in d:
                        c["excluded"] = bool(d.get("excluded"))
                    # Edición manual (o el resultado ya confirmado de "Autocompletar"/AudD que
                    # el admin aplicó) es la fuente más confiable que hay — sobreescribe lo que
                    # ya estuviera en la base compartida entre bares para esta canción.
                    if "genre" in d or "cover" in d or d.get("locked"):
                        track_db_contribute(c.get("artist"), c.get("title"), c.get("genre"), c.get("cover"), verified=True)
                        file_db_contribute(c.get("sha256"), c.get("genre"), c.get("cover"), verified=True)
                    save_state()
                    return self._send(200, {"ok": True, "curated": STATE["curated"]})
                elif act == "upload_cover":
                    # Carátula propia subida a mano (pedido explícito) — a diferencia de la que
                    # trae "Autocompletar"/AudD (iTunes, oficial), esta es una elección del bar,
                    # así que NUNCA se contribuye a track_db/file_db (esas bases son para datos
                    # objetivos compartidos, no para el gusto de un bar en particular).
                    yt = d.get("yt")
                    if not yt or not is_local_id(yt):
                        return self._send(400, {"error": "yt inválido"})
                    c = next((c for c in STATE["curated"] if c["yt"] == yt), None)
                    if not c:
                        return self._send(404, {"error": "No encontrada"})
                    if not c.get("custom_cover"):
                        used = sum(1 for x in STATE["curated"] if x.get("custom_cover"))
                        if used >= MAX_CUSTOM_COVERS:
                            return self._send(400, {"error": f"Límite de {MAX_CUSTOM_COVERS} carátulas propias alcanzado en este bar — borra alguna o usa una sugerencia"})
                    processed = resize_cover_image(str(d.get("image") or ""))
                    if not processed:
                        return self._send(400, {"error": "No se pudo procesar la imagen"})
                    c["cover"] = processed
                    c["custom_cover"] = True
                    save_state()
                    return self._send(200, {"ok": True, "cover": processed})
                elif act == "remove_cover":
                    # Pedido explícito: clic en una carátula propia la quita — libera el cupo
                    # (no tendría sentido que siguiera contando contra el límite de 50 si ya no
                    # existe). Solo limpia si de verdad era una subida propia; en una carátula
                    # de iTunes no hace nada raro, simplemente no había cupo que liberar.
                    yt = d.get("yt")
                    if not yt or not is_local_id(yt):
                        return self._send(400, {"error": "yt inválido"})
                    c = next((c for c in STATE["curated"] if c["yt"] == yt), None)
                    if not c:
                        return self._send(404, {"error": "No encontrada"})
                    c["cover"] = None
                    c["custom_cover"] = False
                    save_state()
                    return self._send(200, {"ok": True})
                elif act == "backfill_hash":
                    # "Forzar revisión" ahora se salta un archivo SOLO si ya tiene sha256 (ver
                    # scanFolder en admin.html — pedido explícito: "esto debería ser súper
                    # rápido de detectar y refrescar" cuando alguien carga UNA canción, no
                    # rehashear las 400 de siempre). Un catálogo importado antes de que
                    # existiera el sha256 necesita este único paso para calcularlo una vez;
                    # nunca toca título/artista/género — solo adjunta el hash y, si el género
                    # ya estaba puesto, lo comparte con file_db (no verificado — es un
                    # backfill automático, no una corrección manual del admin).
                    items = d.get("items") or []
                    by_yt = {c["yt"]: c for c in STATE["curated"]}
                    n = 0
                    for it in items[:2000]:
                        if not isinstance(it, dict):
                            continue
                        yt = it.get("yt")
                        sha = _norm_sha(it.get("sha256"))
                        c = by_yt.get(yt)
                        if not c or not sha or not is_local_id(yt):
                            continue
                        c["sha256"] = sha
                        n += 1
                        if c.get("genre") or c.get("cover"):
                            file_db_contribute(sha, c.get("genre"), c.get("cover"))
                    save_state()
                    return self._send(200, {"ok": True, "updated": n})
                return self._send(400, {"error": "Acción inválida"})

            if path == "/api/admin/cover_suggestions":
                # Sugerencias para elegir carátula a mano (pedido explícito, "combinadas"):
                # primero iTunes (catálogo oficial, sin riesgo de derechos), luego Google Images
                # como respaldo si iTunes no trae nada bueno — el cliente debe mostrar un
                # disclaimer claro en las de Google ("no oficial, responsabilidad del bar"), acá
                # solo se etiquetan con source:"google" para que la UI las distinga.
                artist = str(d.get("artist") or "").strip()[:80]
                title = str(d.get("title") or "").strip()[:150]
                clean_title = _clean_for_lookup(title)
                results, seen = [], set()
                for r in _itunes_query(f"{artist} {clean_title}".strip(), "song", limit=8):
                    art = r.get("artworkUrl100")
                    if not art:
                        continue
                    cov = art.replace("100x100bb", "600x600bb")
                    if cov in seen:
                        continue
                    seen.add(cov)
                    results.append({"cover": cov, "source": "itunes",
                                     "label": f"{r.get('artistName','')} — {r.get('trackName','')}"[:100]})
                    if len(results) >= 6:
                        break
                results += google_image_suggestions(f"{artist} {clean_title} carátula álbum".strip())
                return self._send(200, {"ok": True, "results": results})

            if path == "/api/admin/classify_track":
                # Ayuda opcional del importador: recibe un título/artista "adivinado" del
                # nombre del archivo y lo refina cruzando con iTunes — reutiliza las mismas
                # funciones que ya limpian/corrigen resultados de YouTube (construidas para
                # eso, pero el cruce con iTunes no le importa de dónde salió el texto).
                title_guess = str(d.get("title") or "").strip()[:200]
                artist_guess = str(d.get("artist") or "").strip()[:120]
                # hint: nombre del archivo/carpeta/ruta original — la señal MÁS precisa para
                # género en música regional (ver classify_genre). El cliente lo manda si lo tiene.
                hint = str(d.get("hint") or d.get("path") or "")[:300]
                clean_title = _clean_title_display(title_guess)
                artist = _lookup_real_artist(_clean_for_lookup(clean_title)) if not artist_guess else None
                final_artist = artist or artist_guess or ""
                genre = classify_genre(artist_guess or artist or "", clean_title, hint)
                cover = itunes_cover(final_artist, clean_title)
                if not cover:
                    hit = track_db_lookup(final_artist, clean_title)
                    if hit:
                        cover = hit.get("cover")
                # Contribuye a la base compartida entre bares con lo que se acaba de resolver
                # (no verificado — el admin todavía puede corregirlo antes de guardar).
                track_db_contribute(final_artist, clean_title, genre, cover)
                return self._send(200, {"ok": True, "title": clean_title,
                                         "artist": final_artist, "genre": genre, "cover": cover})

            if path == "/api/admin/dedica_presets":
                act = d.get("action")
                presets = STATE["settings"].setdefault("dedica_presets", [])
                if act == "add":
                    text = str(d.get("text") or "").strip()[:80]
                    if text and text not in presets and len(presets) < 20:
                        presets.append(text)
                elif act == "remove":
                    STATE["settings"]["dedica_presets"] = [p for p in presets if p != d.get("text")]
                return self._send(200, {"ok": True, "dedica_presets": STATE["settings"]["dedica_presets"]})

            if path == "/api/admin/dedica_codes":
                if d.get("action") == "generate":
                    used = {c["code"] for c in STATE.get("dedica_codes", []) if not c["used"]}
                    code = gen_pin()
                    for _ in range(200):
                        if code not in used:
                            break
                        code = gen_pin()
                    entry = {"code": code, "created_at": time.time(), "used": False,
                             "used_at": None, "used_by_table": None}
                    STATE.setdefault("dedica_codes", []).append(entry)
                    # No necesitamos historial ilimitado — solo lo suficiente para que el admin
                    # vea los últimos códigos entregados y si ya se usaron.
                    if len(STATE["dedica_codes"]) > 100:
                        STATE["dedica_codes"] = STATE["dedica_codes"][-100:]
                    return self._send(200, {"ok": True, "code": code})
                return self._send(400, {"error": "Acción inválida"})

            if path == "/api/admin/add":
                yt = (d.get("yt") or "").strip()
                title = str(d.get("title") or "Canción")[:120]
                artist = str(d.get("artist") or "")[:80]
                if not yt:
                    return self._send(400, {"error": "Falta yt"})
                # Mismo criterio que /api/request: en modo catálogo local, este es OTRO punto
                # por el que se puede colar un pedido — el admin agregando directo a la cola en
                # vivo no debe poder meter YouTube si el local eligió no usarlo.
                if STATE["settings"].get("content_mode") == "local" and not is_local_id(yt):
                    return self._send(400, {"error": "Este local solo pide música de su catálogo propio."})
                local_media_type, local_path, local_genre, local_cover = None, None, None, None
                if is_local_id(yt):
                    _cur_entry = next((c for c in STATE["curated"] if c["yt"] == yt), None)
                    if not _cur_entry:
                        return self._send(400, {"error": "Esa canción ya no está en el catálogo del local."})
                    if _cur_entry.get("missing"):
                        return self._send(400, {"error": "Ese archivo ya no está en la carpeta del local."})
                    if _cur_entry.get("excluded"):
                        return self._send(400, {"error": "Esa canción fue descartada del catálogo."})
                    dur = _cur_entry.get("duration") or DEFAULT_DUR
                    local_media_type = _cur_entry.get("media_type")
                    local_path = _cur_entry.get("local_path")
                    local_genre = _cur_entry.get("genre")
                    local_cover = _cur_entry.get("cover")
                else:
                    dur = _parse_len(d.get("length")) or DEFAULT_DUR
                pos = d.get("position")
                item = {"id": nid(), "title": title, "artist": artist, "yt": yt,
                        "token": None, "table": "Admin", "priority": False, "super": False,
                        "mode": "normal", "duration": dur, "status": "approved",
                        "play_status": "pending", "played_enough": False, "requeue_count": 0,
                        "ts": time.time(), "charge_on_play": 0, "charged": False, "charge_kind": "",
                        "media_type": local_media_type, "local_path": local_path,
                        "genre": local_genre, "cover": local_cover}
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
                STATE["jump_abuse"] = {}
                STATE["celebrated_loved"] = set()
                STATE["loved_celebration"] = None
                _id[0] = 0
                FB_IDX[0] = 0
                return self._send(200, {"ok": True})

            # ---- Dedicatorias ----
            if path == "/api/dedica":
                if not _rate_ok(self._client_ip(), "social"):
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
                # Mensajes predeterminados (configurados por el admin) pasan siempre sin revisión.
                # Cualquier otro texto necesita un código de un solo uso que el admin genera desde
                # /admin — el código ES la moderación: reemplaza el filtro por palabras clave.
                if message in STATE["settings"].get("dedica_presets", []):
                    status, mod_reason = "approved", ""
                else:
                    code = str(d.get("code") or "").strip()
                    entry = next((c for c in STATE.get("dedica_codes", [])
                                 if c["code"] == code and not c["used"]), None)
                    if not entry:
                        return self._send(400, {"error": "Ese mensaje no es uno de los predeterminados. "
                                                 "Pídele al mesero o al admin un código para enviarlo.",
                                                 "needs_code": True})
                    entry["used"] = True
                    entry["used_at"] = time.time()
                    entry["used_by_table"] = sess["table"]
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
                if not _rate_ok(self._client_ip(), "social"):
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
                if not _rate_ok(self._client_ip(), "social"):
                    return self._send(429, {"error": "Demasiadas solicitudes. Espera un momento."})
                if not STATE["settings"].get("allow_skip_vote"):
                    return self._send(400, {"error": "El skip por votación no está activado en este local."})
                np_sv = STATE.get("now_playing")
                if not np_sv:
                    return self._send(400, {"error": "No hay ninguna canción sonando ahora."})
                # Aplica a la lista del local y a las canciones gratis — nunca a una pagada
                # (prioridad o salto al #1): sería injusto para quien pagó por sonar.
                if np_sv.get("charge_on_play", 0) > 0 or np_sv.get("paid_amount", 0) > 0:
                    return self._send(400, {"error": "Esta canción fue pagada — el skip por votación no aplica a canciones pagadas."})
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
                if not _rate_ok(self._client_ip(), "social"):
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
                "tym": TYM, "venues": {vid: venue_snapshot(v) for vid, v in VENUES.items()},
                "auth": AUTH}   # sesiones de dueño (cookie tymauth) — sobreviven a un redeploy
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
        # Backup a Redis: máx una vez por minuto, en hilo separado para no bloquear.
        # Los logos (venue_logo/tym_logo) SÍ viajan en este backup — antes se excluían por
        # miedo a que fueran muy pesados para el free tier de Redis, pero desde que
        # remove_solid_bg() recorta y redimensiona (ver más arriba) un logo real pesa unos
        # 50-150KB, no varios MB — dejar el logo afuera del backup significa que un redeploy
        # de Render (disco efímero, sin data.json local) lo perdía y el admin tenía que
        # volver a subirlo cada vez.
        now = time.time()
        if REDIS_URL and REDIS_TOKEN and now - _redis_last_save[0] > 60:
            _redis_last_save[0] = now
            threading.Thread(target=redis_save, args=(data,), daemon=True).start()
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
            for k in ("socials", "tym_logo", "subscribers", "events", "accounts", "vapid", "push_subs", "audd_cache", "track_db", "file_db"):
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
        AUTH.clear()
        for tk, vid in d.get("auth", {}).items():
            if vid == "*" or vid in VENUES:
                AUTH[tk] = vid
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
