# Desplegar TYM Music (capa free)

La app es Python puro (stdlib) + un proceso web. Funciona en cualquier host con Python.
Con **dominio + HTTPS** (que dan estos hosts) **el reproductor de YouTube funciona** desde cualquier
dispositivo (la limitación de “solo localhost” era únicamente por servir desde una IP cruda).

## Opción recomendada: Render (gratis, simple)
1. Sube esta carpeta a un repo de **GitHub**.
2. En https://render.com → **New → Blueprint** → conecta el repo. Render lee `render.yaml`.
   (O **New → Web Service**: Build `pip install -r requirements.txt`, Start `python app/server.py`.)
3. Deploy. Te da una URL tipo `https://tym-music.onrender.com`.
4. (Opcional) En Settings → Environment, agrega `PUBLIC_URL=https://tym-music.onrender.com`
   para que el **QR** apunte al dominio real. Redeploy.
5. Listo:
   - Cliente / QR: `https://tu-app.onrender.com/`
   - Pantalla del local (TV): `https://tu-app.onrender.com/tv`
   - Panel del dueño: `https://tu-app.onrender.com/admin`

> En el plan free, el servicio “se duerme” tras ~15 min sin tráfico y tarda ~30s en despertar.
> El sistema lee el puerto de la variable `PORT` que inyecta el host (ya está soportado).

## Alternativas gratis
- **Railway** / **Fly.io**: igual de simples. Fly permite **volumen persistente** para `data.json`.
- **Replit** / **PythonAnywhere**: buenos para demos rápidas.

## Persistencia en deploy
El estado se guarda en `app/data.json`. En hosts con disco **efímero** (como Render free) ese archivo
se reinicia en cada redeploy/reinicio — está bien para demo, pero **no** para locales en modo saldo
prepago (perderían el saldo real de sus clientes en cada redeploy).

**Ya soportado sin código nuevo — backup automático a Upstash Redis (gratis):**
1. Crea una DB gratuita en https://upstash.com (Redis).
2. Copia el **REST URL** y el **REST Token**.
3. En Render → Settings → Environment, agrega `UPSTASH_REDIS_REST_URL` y `UPSTASH_REDIS_REST_TOKEN`.
4. Redeploy. Cada cobro fuerza un backup (máx. cada ~60s); al arrancar, si no hay `data.json` local
   (redeploy en disco efímero), el servidor recupera el último estado desde Redis automáticamente.

Otras opciones si se quiere disco persistente real:
- Fly.io con **volumen**, o
- Migrar `data.json` a una base de datos (Postgres free de Render/Railway/Supabase). La estructura
  `{version, venues:{...}}` ya está pensada para ese salto y para **multi-bar**.

## Para producción (pendiente)
- Formalizar la **fuente de música** (catálogo licenciado para uso comercial) + Sayco-Acinpro.
- Mover el estado a base de datos (multi-bar).
- Autenticación del panel `/admin`.
