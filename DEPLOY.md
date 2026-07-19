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

## Correo de "olvidé mi contraseña"
El botón "¿Olvidaste tu contraseña?" (en `/admin`, `/tv`, `/player`, `/tym`) necesita un proveedor de
correo configurado para enviar de verdad. **Sin ninguna variable configurada, el endpoint sigue
respondiendo "ok" pero el correo NUNCA sale — solo queda en el log del servidor** (así se puede
probar el flujo en local sin credenciales, pero en producción hay que configurar algo o el botón
parece funcionar y no llega nada).

**Confirmado en vivo (2026-07-18): Render bloquea las conexiones SMTP salientes** — con
`SMTP_USER`/`SMTP_PASS` bien configurados (probados exitosamente desde una terminal local), el
correo nunca llegó desde Render y tampoco generó ninguna alerta de seguridad de Google (descartando
un bloqueo de Gmail) — el patrón apunta a que Render no deja salir tráfico por el puerto 465/587.
**Por eso el envío usa Resend (API HTTPS) como primera opción, con SMTP como respaldo para hosts
que sí permitan SMTP saliente (o para correr en local).**

### Opción recomendada: Resend (API HTTPS, sí funciona en Render)
1. Crea una cuenta gratis en https://resend.com (3.000 correos/mes gratis).
2. Genera un **API Key** desde su dashboard.
3. En Render → Settings → Environment, agrega:
   - `RESEND_API_KEY` = el API key que generaste.
   - `RESEND_FROM` (opcional) — por default usa `TYM Music <onboarding@resend.dev>`, su dirección
     de pruebas que funciona sin verificar dominio propio. Para producción real, verifica tu propio
     dominio en Resend y pon algo como `RESEND_FROM=TYM Music <noreply@tudominio.com>`.
4. Redeploy. Con `RESEND_API_KEY` presente, el servidor usa Resend automáticamente (tiene prioridad
   sobre SMTP) — no hace falta borrar las variables SMTP si ya las tenías.

### Alternativa: SMTP (solo si el host lo permite — NO funciona en Render)
1. En Render → Settings → Environment, agrega:
   - `SMTP_HOST` (default `smtp.gmail.com`, no hace falta si usas Gmail)
   - `SMTP_PORT` (default `465`, no hace falta si usas Gmail)
   - `SMTP_USER` = tu correo de Gmail
   - `SMTP_PASS` = una **contraseña de aplicación** (Cuenta Google → Seguridad → Verificación en
     2 pasos → Contraseñas de aplicaciones) — **no** la contraseña normal de la cuenta, Gmail la rechaza.
2. Redeploy. Prueba el flujo y revisa los logs de Render si sigue sin llegar (`✉️ Error enviando
   correo a...` indica credenciales/SMTP mal configurados o el host bloqueando SMTP saliente; el
   mensaje `[correo simulado]` indica que ninguna variable está puesta).

## Moderación de dedicatorias y "solo música" (heurística, sin IA)
Dos ajustes opcionales, apagados por defecto, que funcionan por palabras clave/duración —
sin llamadas a servicios externos, sin costo, sin variables de entorno que configurar:

- **Panel → Social → "Moderar antes de mostrar en TV"**: los mensajes que los clientes se
  mandan entre mesas (dedicatorias) se revisan contra una lista de palabras/patrones (groserías,
  spam, links, números de teléfono) antes de salir en `/tv`. Con el ajuste apagado, salen directo
  sin revisión (comportamiento original). Los marcados quedan pendientes de aprobación manual del
  admin, nunca se rechazan solos.
- **Panel → Ajustes → Filtro de contenido → "Solo música"**: rechaza pedidos cuyo título/canal
  contenga palabras típicas de contenido no-musical (podcast, tutorial, gameplay, etc) o que
  superen 20 minutos de duración sin otro indicio.

Ambos son heurísticas de palabras clave, no perfectas — pueden dejar pasar algo raro o bloquear
un título inusual — pero no dependen de ningún servicio externo ni tienen costo por uso.

## Para producción (pendiente)
- Formalizar la **fuente de música** (catálogo licenciado para uso comercial) + Sayco-Acinpro.
- Mover el estado a base de datos (multi-bar).
- Autenticación del panel `/admin`.
