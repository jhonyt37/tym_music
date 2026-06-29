# TYM Music — Master Brief (documento maestro para futuras sesiones)

> **Lee esto primero al retomar el proyecto.** Consolida decisiones, estado y contexto. Última actualización: 2026-06-25.

## Qué es
Sistema de **música por demanda** para establecimientos comerciales en **Colombia**. Los clientes ponen su propia música desde el celular vía **web app sin descargas** (cero fricción). Cola compartida, control del dueño.

Directorio del proyecto: `/Users/jhonytoro/Documents/emprendimiento/tym_music`

## Decisiones tomadas (firmes)
| Tema | Decisión |
|---|---|
| **Monetización** | 100% **pay-per-priority** (pagar para adelantar tu canción). **Sin cobro inicial ni mensualidad al comercio** en la entrada. |
| **Cobro al usuario** | Micro-cobro **se suma a la cuenta final** del cliente (NO se procesa por canción → evita comisión de pasarela que se comería ~55%). Liquidación B2B en bloque con el comercio. |
| **Precio por canción** | **Variable, lo fija cada comercio**: $200 (cafés/espera) → $2.000 COP (premium/discos). |
| **Fallback** | Si nadie paga → suena **lista random curada por el dueño** (acorde a su estilo, legal). |
| **Control del dueño** | Total: estilo/géneros, aprobar/rechazar, volumen, horarios. |
| **Anti-cola-infinita** | Límite por usuario, tiempo máx. de espera, precio dinámico opcional. |
| **Producto** | Primero **web app** (sin descargas). App/ecosistema de usuario solo a futuro. |
| **Fuente/legal** | Empezar con **embeds de reproductores oficiales** (front desacoplado del back, sin atarse a APIs Spotify/YouTube) → migrar a **catálogo licenciado** con el tiempo. **Nunca meterse en problemas legales.** Gestionar Sayco-Acinpro. |
| **Nichos** | Atacar **varios gradualmente** (gym, cafés, barberías, food courts, salas de espera) y ver dónde tracciona más. NO entrar de frente con bares (ahí está Melao). |
| **Roadmap ingresos** | Año 1 entrar (pay-per-priority) → Año 2 agregar **suscripción anual mínima** al comercio que cubra operación → Año 3 catálogo licenciado + app. |

## Veredicto de viabilidad
**VIABLE con diferenciación.** No es océano azul clonar a Melao. El azul está en: música dedicada + legalidad + ingreso para el comercio.

## Números clave (supuestos a validar)
- Precio promedio ponderado: $700/prioridad. Local activo: ~25 prioridades/noche × 18 noches = 450/mes → recaudo $315k/local/mes.
- Split base: comercio 35% / TYM 65% (palanca a validar).
- Margen contribución TYM/local: **~$196k/mes (embed)**, ~$116k/mes (catálogo licenciado).
- **Punto de equilibrio: ~76 locales activos (embed)**, ~129 (licenciado), con costos fijos ~$15M COP/mes.
- **Métrica que importa: prioridades pagadas por noche** (no locales registrados).

## Competencia
- **Melao** (melao.app): rival más cercano, pero es POS todo-en-uno; música = feature secundaria; zona gris legal; 1 bar visible. Vulnerable.
- Otros locales: Apollo App, Secret DJ, "Programa tu música" (Antioquia).
- Internacionales (referencia): TouchTunes (pay-per-song, 65k locales), Rockbot (US$25/mes licenciado), Soundtrack.

## Riesgos top
1. Legal de fuentes (Spotify/YouTube prohíben uso comercial) → mitigado con embeds + catálogo + Sayco-Acinpro. **Validar con abogado PI.**
2. Cartera (comercio no liquida) → liquidación frecuente + garantía.
3. Pocos pagan → fallback random + validar en campo.

## Decisiones abiertas (resolver antes/durante MVP)
- Split % final comercio/TYM.
- Precio dinámico vs fijo.
- Definición legal exacta del "sistema intermedio" (con abogado PI).
- Mecanismo anti-cartera y frecuencia de liquidación.
- Costos reales: cloud, catálogo licenciado, pasarela.

## Próximos pasos
1. Validación de campo (10–15 entrevistas, guion en doc 02 §14).
2. Asesoría legal PI sobre embeds desacoplados.
3. MVP web app: cola tiempo real + panel dueño + QR cliente + registro de cobros a la cuenta.
4. Piloto en 1–2 nichos midiendo prioridades pagadas/noche.

## Documentos del proyecto
- `docs/00-MASTER-BRIEF.md` — este archivo (resumen maestro).
- `docs/02-modelo-negocio-y-costos.md` — modelo y costos detallados.
- `docs/TYM_Music_Analisis_Mercado.docx` — **documento Word completo con gráficas, tablas y proyecciones** (para presentar/compartir).
- `~/.claude/plans/immutable-enchanting-lemur.md` — estudio de mercado original (benchmark, océano rojo/azul, warnings).
- Scripts de generación: scratchpad de la sesión (`build_charts.py`, `build_docx.py`).
