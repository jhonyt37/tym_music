# TYM Music — MVP para validación de campo

Web app funcional (sin descargas) para validar el flujo real con clientes y dueños de locales.
Corre en tu red local (mismo WiFi), **no necesita DNS ni internet para servirse** (solo se usa internet para cargar los videos de YouTube y las miniaturas).

## Cómo arrancarlo

```bash
cd /Users/jhonytoro/Documents/emprendimiento/tym_music/app
python3 server.py
```

Al iniciar imprime las URLs. **El servidor ya está corriendo en esta sesión.**

## URLs (tu IP actual: 192.168.1.186 · puerto 8000)

> ⚠️ **IMPORTANTE:** la **pantalla del local** y el **panel del dueño** se abren EN EL COMPUTADOR con `localhost` (YouTube **no reproduce** desde una IP cruda). Solo el **celular del cliente** usa la IP. La pantalla del local solo necesita correr en tu laptop; los clientes nunca reproducen video, solo piden.

| Vista | URL | Para quién / dónde |
|---|---|---|
| 📺 **Pantalla del local** | `http://localhost:8000/player` | **En tu laptop** (la "rocola"): reproduce y avanza sola, con controles. **Debe ser localhost.** |
| 📺 **TV (pantalla grande)** | `http://localhost:8000/tv` | **Laptop conectado por HDMI al TV**: video a pantalla completa + QR para que la gente pida. Toca una vez "iniciar" para activar el sonido. **Debe ser localhost.** |
| 🛠️ **Panel del dueño** | `http://localhost:8000/admin` | **En tu laptop**: aprobar/rechazar, precio, estilo, **ver los cobros a la cuenta**. |
| 🎵 **Cliente** | `http://192.168.1.186:8000/` | El **celular del cliente** (mismo WiFi). Es lo que abres al validar. |
| 🔳 **QR del local** | `http://192.168.1.186:8000/qr.png` | QR que apunta a la vista de cliente (imprímelo y simula el "escanea en la mesa"). |

> Si cambias de red WiFi, la IP cambia (la del cliente). Vuelve a correr el servidor: imprime la IP nueva, o mira tu IP con `ipconfig getifaddr en0`. La pantalla/panel siguen en `localhost`.

## Antes de la demo: configura las mesas (una vez)
En el **panel del dueño** (`/admin`) → sección **"Mesas y códigos (PIN)"**: cada mesa tiene un **código de 4 dígitos**. Imprímelo/escríbelo y pégalo en cada mesa (ej: "Mesa 3 — código 3935"). Puedes regenerar (↻) o agregar mesas. Esto es lo que evita que un cliente pida en nombre de otra mesa: para cargar a la Mesa 3 hay que tener el código que solo está en la Mesa 3.

## Cómo hacer la demo con un cliente (guion sugerido)

1. Abre la **pantalla del local** (`http://localhost:8000/player`) en tu laptop y toca **"▶ Activar audio"**. Con la cola vacía, suena la **lista del local** (fallback).
2. Dale al cliente tu celular (o que escanee el **QR**) → abre la vista de **cliente** → **ingresa el código de su mesa** (PIN).
3. Busca una canción (búsqueda real de YouTube) o usa las **recomendadas** (Más pedido aquí / Del local / Populares / Por género) → **Pedir** → elige cola normal o **⚡ Prioridad**.
4. El cliente ve **qué suena, cuánto falta, cuántas hay en cola y en qué puesto va su canción**.
5. Opcional: el cliente **compra un paquete** de prioridades (con descuento) o un **pase por tiempo** → se suma a la cuenta de su mesa.
6. En el **panel del dueño**: apruebas (si "auto-aprobar" está apagado) y ves los **cobros por mesa** acumulándose → momento de la conversación de negocio.

## Qué valida este MVP
- ¿El cliente entiende y le gusta poner su música? ¿Pagaría la prioridad o un paquete?
- ¿Al dueño le interesa el ingreso extra y el control? ¿Cuánto cobraría?
- El flujo completo: ingresar mesa → pedir → prioridad/paquete → aprobar → sonar → **cobro a la cuenta** → fallback del dueño.

## Funcionalidades (v2)
- **Sesión de mesa por PIN**: el cliente entra con el código de su mesa; los cobros van a esa mesa (anti-suplantación). **PINs fijos: Mesa 1=1111 … Mesa 5=5555** (no cambian al reiniciar; el dueño puede regenerarlos).
- **Social / comunidad**: reacciones positivas (❤️🔥👍) a lo que suena, lo que viene y "Ya sonaron"; al autor le avisa "a N personas les gusta tu música"; **ranking "Lo más querido de la noche"**; y botón **"⚡ Impulsar"** para pasar una canción de la cola a prioridad (lo paga quien impulsa).
- **Nunca en silencio**: si nadie pide, suena la lista del local en orden (y si está vacía, el catálogo).
- **Pantalla de TV** (`/tv`): video a pantalla completa con info que **aparece y se desvanece escalonada** (sonando ahora, a continuación, QR, ranking) con ratos de solo video; botón/tecla **F** de pantalla completa; contador de reacciones en vivo; **emojis flotantes** cuando llueven likes.
- **Cerrar cuenta de mesa**: en el panel del dueño, botón "Cerrar cuenta" por mesa (cobra y libera la mesa para el próximo cliente).
- **Cobro claro**: el cliente ve "Tu cuenta de la mesa" (coincide con la del bar) y **confirma cada cobro** de prioridad/paquete/impulso antes de aplicarlo. Al impulsar la canción de otra mesa, se le avisa que el cargo va a **su** mesa.
- **Anti-repetición**: no repite la misma canción dentro de X min ni de las últimas N canciones (configurable).
- **Sin silencios**: watchdog que avanza solo si el video terminó o se traba, y **recorte de los últimos N seg** del video (corta outros/silencios, configurable).
- **Persistencia**: el estado se guarda en `data.json` (sobrevive reinicios). Estructura `{version, venues:{...}}` pensada para migrar a base de datos y **multi-bar** más adelante.

## Novedades (v3 — multi-bar + login + analítica)
- **Multi-establecimiento (multi-tenant)**: un mismo deploy sirve a varios bares. Cada bar es un "venue" con sus datos aislados. Cliente entra por su QR `…/?v=<bar>` (ej. `?v=bardemo`, `?v=lazona`).
- **Login por dueño**: `/admin`, `/tv` y `/player` requieren ingreso. Demo: **bardemo / tym1234**, **lazona / tym1234**. Cierre de sesión con "Salir".
- **Dashboard TYM global** (`/tym`, login **tym / tymmaster**): facturación total y **por local**, **horas pico**, pedidos **free vs premium**, y **cuentas de mesa con hora de apertura/cierre** (una mesa puede tener varias cuentas el mismo día). La data de TYM se alimenta de TODO lo que pasa en cada bar.
- **Trim de silencios "aprendido"**: no se puede medir el silencio real desde YouTube embed, así que el corte ciego viene **apagado** (no corta finales con sonido) y el sistema **aprende el punto de corte** cuando el local salta una canción cerca del final (botón Saltar del player); la próxima vez esa canción salta sola ahí.

## Novedades (v2.3)
- **Saltar al #1 ⏫**: pasa una canción al primer lugar; **premium** (Nx el precio, default 3x), **1 sola vez por canción** y para la **primera mesa** que lo use (las demás quedan bloqueadas hasta la siguiente canción).
- **Límite de canciones gratis**: máx N por mesa cada X min (default 3/10min); pasado eso, hay que usar prioridad.
- **Síguenos + correo**: en la pestaña Social, botones de redes (Instagram/TikTok/Web, configurables) + captura de email + “Próximamente app”. Los correos se ven en el panel del dueño.
- **Logos en el TV**: sube el logo de TYM y el del bar (panel del dueño) → salen **por separado** en la pantalla, recordación de ambas marcas.

## Despliegue (producción)
Ver **`DEPLOY.md`** (Render gratis, paso a paso). Resumen:
El bloqueo del reproductor de YouTube ocurre **solo al servir por IP cruda** (ej. `192.168.1.186`). En un **host con dominio propio + HTTPS** el embed funciona desde cualquier dispositivo (se acaba la limitación de localhost). Pendiente para producción: formalizar la fuente de música (catálogo licenciado) y migrar el estado de `data.json` a una base de datos.
- **Ahora suena con progreso**: barra de avance, "cuánto falta", "N en cola (~min)" y "tu canción: puesto #K, ~min".
- **Recomendadas** (4 fuentes): más pedido en el local, lista curada del dueño, populares, por género.
- **Paquetes**: créditos de prioridad con descuento + pase por tiempo (prioridad ilimitada). Configurables por el dueño.
- **Búsqueda real** de YouTube y **pegar link**.

## Límites conscientes (es un MVP de validación, no producción)
- **Estado en memoria**: si reinicias el servidor, se borra la cola/cobros (botón "Reiniciar demo" en el panel).
- **Búsqueda**: real sobre YouTube (escribe cualquier canción y trae resultados con miniatura/duración) + "pegar link". Se hace en el servidor parseando los resultados de YouTube, **sin API key**. Para producción conviene migrar a la **YouTube Data API** (más robusta) o a catálogo licenciado.
- **Pago**: los cobros se *registran* (simulan sumarse a la cuenta); aún no hay pasarela ni liquidación real.
- **Música**: usa el reproductor embebido de YouTube (estrategia legal de la fase de entrada). Migración a catálogo licenciado = fase posterior.
- Un solo local a la vez (multi-local = fase siguiente).

## Estructura
```
app/
  server.py            # backend (Python stdlib, sin dependencias)
  static/
    index.html         # cliente (celular)
    player.html        # pantalla del local
    admin.html         # panel del dueño
    style.css          # estilos (mobile-first, tema oscuro)
    qr.png             # QR generado al iniciar
  README_MVP.md
```
