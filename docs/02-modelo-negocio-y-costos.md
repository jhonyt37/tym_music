# TYM Music — Modelo de Negocio y Análisis de Costos Operativos

> Fecha: 2026-06-25 · Fase: Definición de modelo (pre-MVP) · Doc previo: estudio de mercado (`~/.claude/plans/immutable-enchanting-lemur.md`)

## 1. Modelo de negocio definido

**Tesis:** monetización 100% por **pago por prioridad (pay-per-priority)**, sin cobros iniciales ni mensualidad al comercio. El comercio adopta porque **gana dinero sin arriesgar nada**.

**Mecánica:**
1. El cliente del local pide una canción para que suene **antes** (priority) → micro-cobro **muy bajo**.
2. El cobro **NO se procesa por canción**: se **acumula y se suma a la cuenta final** del cliente con el comercio.
3. **El que quiere escuchar, paga.** Si nadie está pagando prioridades → el sistema reproduce una **lista "random" curada por el dueño** (acorde a su estilo).
4. **Control total del dueño**: define estilo/géneros permitidos, aprueba/rechaza, gestiona la lista de respaldo.
5. **Reglas anti-cola-infinita**: límites claros para que la cola no se descontrole.
6. **Legalidad** vía un "sistema intermedio" (ver §4).
7. Entrada al mercado por **nichos desatendidos** (ver §8).

**Por qué funciona la adopción:** riesgo cero para el comercio (no paga nada por adelantado), ingreso incremental nuevo, mejor experiencia de cliente, y control para no romper el ambiente del local.

---

## 2. Por qué "sumar a la cuenta" es la decisión correcta (clave de unit economics)

Procesar cada canción como un micropago independiente es **inviable** en Colombia:

| Concepto | Micropago individual ($1.500/canción) | Acumulado a la cuenta (liquidación mensual) |
|---|---|---|
| Comisión pasarela (Wompi tarjeta) | 2,65% + **$700** + IVA ≈ **$896** | 2,65% + $700 sobre el **total mensual** (despreciable por unidad) |
| % que se come la comisión | **~45–60%** 😱 | **<3%** ✅ |
| Fricción para el cliente | Pagar cada vez | Cero: paga todo junto al final |

→ El micro-cobro se **registra digitalmente** durante la noche y se **liquida una sola vez**: el cliente lo paga en su cuenta al comercio, y el comercio **liquida con TYM en bloque** (B2B). Esto hace que el modelo sea **rentable incluso con precios bajísimos por canción**.

---

## 3. Flujo de dinero (settlement)

```
Cliente ──paga su cuenta completa──> Comercio  (incluye $ de prioridades)
Comercio ──liquida en bloque (semanal/mensual)──> TYM Music  (solo la parte de TYM)
```

**Dos variantes de cobro al comercio (a decidir):**
- **A) Comercio retiene su % y paga el resto a TYM** → TYM factura mensual el acumulado de su parte.
- **B) TYM cobra todo y devuelve el % al comercio** → más control para TYM pero más fricción/confianza requerida.

Recomendado: **variante A** (el comercio se queda con SU dinero, paga solo lo de TYM) → menos fricción, más confianza, una sola transacción pasarela mensual.

**Riesgo a mitigar:** que el comercio no liquide (cartera). Mitigación: liquidación frecuente, depósito/garantía pequeño, o corte de servicio por mora.

---

## 4. El "sistema intermedio" de legalidad

Tres capas posibles (de menor a mayor solidez), combinables:

1. **Lista de respaldo curada por el dueño** desde **catálogo licenciado** (cuando nadie paga) → la mayoría del tiempo suena música legal.
2. **Requests de prioridad** del cliente: aquí está el reto legal de la fuente (Spotify/YouTube = zona gris). El "sistema intermedio" puede ser:
   - Requests **solo sobre el catálogo licenciado** (más seguro), o
   - Permitir fuentes externas pero con **gestión Sayco-Acinpro empaquetada** que cubra la comunicación pública.
3. **TYM gestiona/optimiza la licencia Sayco-Acinpro (OSA)** del comercio como servicio integrado → convierte el riesgo en valor.

> ⚠️ **Decisión crítica abierta:** definir qué significa exactamente "sistema intermedio". Hay que validar con un abogado de propiedad intelectual en Colombia. Impacta directamente costos (§6) y viabilidad técnica.

---

## 5. Reglas de producto (mecánicas a diseñar)

- **Control del dueño:** géneros/estilo permitidos, lista blanca/negra, aprobar/rechazar en tiempo real, volumen, horarios.
- **Anti-cola-infinita:**
  - Límite de canciones en cola por usuario.
  - Tiempo máximo de espera o "la prioridad garantiza sonar en las próximas N canciones".
  - Precio dinámico opcional (cuesta más adelantar si la cola está llena) → más ingreso + autorregula la cola.
  - Anti-duplicados y filtro de explícitas según el local.
- **Fallback random:** si no hay prioridades pagas en cola → reproduce la **playlist de respaldo** del dueño (legal, acorde al estilo).

---

## 6. Análisis de costos operativos

### 6.1 Costos FIJOS de plataforma (no dependen del # de locales)
| Costo | Etapa MVP (mensual) | En escala |
|---|---|---|
| Infraestructura cloud (backend tiempo real, websockets, DB, hosting) | US$50–250 | escala con uso (US$500–2.000+) |
| Desarrollo y mantenimiento (equipo/devs) | el costo dominante inicial | — |
| Dominio, monitoreo, herramientas | US$30–100 | — |

### 6.2 Costos VARIABLES (por local / por uso)
| Costo | Estimado | Notas |
|---|---|---|
| **Fuente de música** | $0 (zona gris) **hasta** US$15–30/local/mes (catálogo licenciado) | **El mayor swing de costo**; depende de §4 |
| Pasarela de pago | ~2,65% + $700 + IVA por liquidación **mensual** por local | Despreciable por canción gracias a §2 |
| Licencia Sayco-Acinpro | variable (la paga el comercio; TYM puede gestionarla) | Negociable con OSA |
| Soporte / onboarding | bajo si es self-service (QR, sin instalación) | — |

### 6.3 El costo que define todo
La **fuente de música legal** es la variable que puede hacer o romper el margen:
- **Zona gris (links Spotify/YouTube):** costo ~$0 pero riesgo legal/baneo (como Melao).
- **Catálogo licenciado propio/revendido:** US$15–30/local/mes → con pay-per-priority esto se debe cubrir con los ingresos del local; en locales de bajo volumen puede comerse el margen.

> Conclusión: el modelo pay-per-priority **exige** que el costo de fuente sea bajo o cubierto por el volumen. Resolver §4 antes de fijar precios finales.

---

## 7. Unit economics (supuestos a validar)

**Supuestos:**
- Precio por prioridad al cliente: **$1.500 COP** (muy bajo, va a la cuenta).
- Local activo: ~**25 prioridades/noche** × **18 noches/mes** = 450 prioridades/mes.
- Recaudo bruto/local: 450 × $1.500 = **$675.000 COP/mes**.
- Split propuesto: **Comercio 30% / TYM 70%** (el comercio gana "dinero gratis" sin costo inicial; TYM asume riesgo y operación).

| Concepto | Por local activo/mes |
|---|---|
| Recaudo bruto | $675.000 |
| Parte del comercio (30%) | $202.500 |
| **Ingreso bruto TYM (70%)** | **$472.500** |
| – Pasarela (2,65% s/recaudo) | ~$18.000 |
| – Fuente música (si licenciada, US$20≈$80.000) | $0 a $80.000 |
| **Margen de contribución TYM/local** | **~$375.000 a $455.000** |

**Escenarios de escala (margen de contribución, antes de fijos):**
| Locales activos | Margen contrib. (catálogo licenciado) | Margen contrib. (zona gris) |
|---|---|---|
| 50 | ~$18,7M/mes | ~$22,7M/mes |
| 150 | ~$56M/mes | ~$68M/mes |
| 400 | ~$150M/mes | ~$182M/mes |

→ Con ~50–80 locales **activos** (no solo registrados) el negocio empieza a cubrir costos fijos de un equipo pequeño. **La métrica clave no es locales registrados sino prioridades pagadas/noche.**

**Sensibilidades a validar:** precio por prioridad, % de clientes que pagan, # de noches activas, split con el comercio.

---

## 8. Nichos desatendidos (ventaja de entrada)

En lugar de pelear con Melao en bares de Medellín, entrar por donde nadie atiende:
| Nicho | Por qué encaja | Notas |
|---|---|---|
| **Gimnasios / boxes de crossfit** | música central, clientes jóvenes, "quiero que suene mi canción" | alto engagement, ambiente competitivo |
| **Cafés / coworkings** | ambiente importa, público dispuesto a micropagar | ticket bajo pero recurrente |
| **Peluquerías / barberías / spa** | espera = tiempo muerto monetizable | sesiones largas |
| **Food courts / cervecerías artesanales** | mucha gente, rotación | volumen |
| **Salas de espera / lavanderías** | tiempo muerto, cero competencia | ticket bajo |

Estrategia: elegir **1 nicho** para el piloto, dominarlo, y expandir.

> Ventaja: en nichos no-bar el "control del estilo" del dueño es aún más valioso y la competencia (Melao/Apollo) no está presente.

---

## 9. Riesgos específicos de este modelo

| Riesgo | Mitigación |
|---|---|
| **Cartera**: el comercio no liquida lo recaudado | Liquidación frecuente, garantía pequeña, corte por mora |
| **Pocos pagan** → ingreso depende 100% de prioridades | Fallback random mantiene el servicio; validar willingness-to-pay en campo |
| **Disputas en la cuenta** ("yo no pedí esa canción") | Confirmación clara al pedir, registro, límite por usuario |
| **Fuente legal** encarece o se prohíbe (Spotify/YouTube) | Resolver §4 con catálogo licenciado antes de escalar |
| **Estacionalidad/noches flojas** | Modelo sin costo fijo para el comercio aguanta meses bajos |

---

## 10. Decisiones abiertas (a resolver antes del MVP)
1. **Split comercio/TYM** (¿30/70? ¿40/60?) — define adopción y margen.
2. **Precio por prioridad** y si es **fijo o dinámico** (sube con la cola).
3. **"Sistema intermedio" de legalidad** — definir con abogado PI Colombia + decidir fuente.
4. **Nicho del piloto** (recomiendo elegir uno).
5. **Frecuencia de liquidación** y mecanismo anti-cartera.

---

## Fuentes nuevas
- Pasarelas Colombia: wompi.com/es/co/planes-tarifas, soporte.wompi.co, btodigital.com, guiadesoftware.com
- Catálogo música legal: soundtrack.io/es/pricing, jamendo licensing, epidemicsound.com
