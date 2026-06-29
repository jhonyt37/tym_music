# Resumen ejecutivo

TYM Music es un sistema que permite a los clientes de establecimientos comerciales (gimnasios, cafés, barberías, bares, etc.) poner su propia música desde el celular mediante una web app sin descargas. El modelo de monetización es pago por prioridad (pay-per-priority): quien quiere que su canción suene antes paga un micro-cobro variable que el comercio fija ($200 a $2.000 COP) y que se suma a la cuenta final del cliente. Si nadie paga, suena una lista de respaldo curada por el dueño.

Conclusiones clave:

- Mercado real y en crecimiento: +40.000 establecimientos nocturnos en Colombia y un subsector de bares/discotecas que creció +64,3% en ingresos promedio en 2025.
- El concepto YA existe en Colombia (Melao, Apollo, Secret DJ), pero como feature secundaria y en zona gris legal. Hay espacio para un producto dedicado, legal y monetizable.
- Veredicto: VIABLE con diferenciación. El océano azul está en "música legal + monetización para el comercio", no en clonar a Melao.
- Sin cobro inicial al comercio: adopta porque gana dinero sin arriesgar. La monetización por suscripción llega después, al crecer el volumen.
- Sumar el micro-cobro a la cuenta (en vez de cobrar por canción) es la decisión que hace rentable el modelo: baja la comisión de pasarela de ~55% a <3%.
- Punto de equilibrio estimado: ~76 locales activos en fase embed (música de bajo costo vía reproductores embebidos).

# 1. Concepto y producto

Web app accesible por QR/enlace, sin descargas, para eliminar fricción en el primer contacto. El cliente pide canciones a una cola compartida que se reproduce en el local. A futuro, una app/ecosistema opcional para usuarios recurrentes.

## Mecánica del producto

- Quien quiere escuchar, paga: micro-cobro por prioridad para adelantar su canción.
- El cobro se acumula y se suma a la cuenta final del cliente (no se procesa canción por canción).
- Si no hay prioridades pagas en cola, suena la lista de respaldo curada por el dueño (legal y acorde al estilo).
- Control total del dueño: estilo/géneros permitidos, aprobar/rechazar, volumen, horarios.
- Reglas anti-cola-infinita: límite por usuario, tiempo máximo de espera, precio dinámico opcional.

# 2. Mercado (Colombia)

| Métrica | Dato | Fuente |
|---|---|---|
| Establecimientos nocturnos | +40.000 (+200.000 empleos directos) | Asobares |
| Empleo bares/gastrobares/discotecas | ~96.000 empleos/mes | DANE |
| Crecimiento sector H1 2025 | +1,9% ingresos reales | Asobares |
| Subsector bares/discotecas 2025 | +13,6% ocupación / +64,3% ingresos | Asobares |
| Informalidad en restaurantes | ~80% informales | DANE |
| Establecimientos independientes | ~95% del mercado | ACODRES |

![Figura 1. El subsector de bares y discotecas fue uno de los de mayor recuperación en 2025.](/Users/jhonytoro/Documents/emprendimiento/tym_music/docs/_build/charts/01_crecimiento_sector.png)

## TAM / SAM / SOM (orden de magnitud)

| Nivel | Definición | Estimación |
|---|---|---|
| TAM | Todos los establecimientos con música en Colombia | ~50.000+ locales; ~$48.000M COP/año potencial |
| SAM | Formales, urbanos, con ambiente musical (4 ciudades) | 8.000-15.000 locales |
| SOM | Alcanzable año 1-2 de forma realista | 100-400 locales |

Riesgo de tamaño: la alta informalidad (80%) reduce el mercado realmente pagador. La métrica que importa no es locales registrados sino prioridades pagadas por noche.

# 3. Competencia y benchmark

## Competidores en Colombia (directos)

| Producto | Cómo funciona | Notas |
|---|---|---|
| Melao | QR en mesa, links Spotify/YouTube, cola, carta digital | Es un POS todo-en-uno; la música es feature secundaria. 1 bar visible. Zona gris legal. |
| Apollo App | Rockola en tiempo real, votar, ver menú | Creada por colombianos (2021). Validar si sigue activa. |
| "Programa tu música" | Pedir canciones en bares/discotecas/emisoras | +50 establecimientos en Antioquia. |
| Secret DJ | Rockola anónima desde el celular | Presencia en Colombia. |

## Referentes internacionales

| Empresa | Modelo | Datos clave |
|---|---|---|
| TouchTunes (USA) | Jukebox + app, pay-per-song | ~65.000 locales, 12M reproducciones/sem, 2M MAU |
| Rockbot (USA) | Música de fondo licenciada + requests | US$25/mes por zona; cubre licencias ASCAP/BMI/SESAC |
| Soundtrack Your Brand | Música de fondo legal B2B | +125M canciones licenciadas para uso comercial |

![Figura 2. Los competidores locales operan con música secundaria y en zona gris. TYM apunta al cuadrante de producto de música dedicado, monetizable y legal.](/Users/jhonytoro/Documents/emprendimiento/tym_music/docs/_build/charts/02_posicionamiento.png)

# 4. Océano rojo vs océano azul

Océano ROJO (saturado): "app/QR para pedir canciones con cola y control del dueño" — ya lo hace Melao y otros.

Océano AZUL (diferenciación defendible):

- Legalidad como producto: gestionar Sayco-Acinpro + evolucionar a catálogo licenciado.
- Monetización pay-per-priority que genera ingreso nuevo para el comercio.
- Producto de música dedicado (no un add-on de un POS).
- Nichos desatendidos fuera del foco bar/discoteca.

# 5. Modelo de negocio

Monetización 100% por pay-per-priority, sin cobro inicial ni mensualidad al comercio en la fase de entrada. El comercio adopta porque gana dinero sin arriesgar. La suscripción anual mínima llega después, cuando el volumen y los ingresos lo justifiquen.

## Por qué "sumar a la cuenta" es la decisión correcta

| Concepto | Micropago por canción ($1.500) | Acumulado a la cuenta |
|---|---|---|
| Comisión pasarela (Wompi tarjeta) | 2,65% + $700 + IVA ~ $896 | 2,65% sobre el total mensual |
| % que se come la comisión | ~45-60% | <3% |
| Fricción para el cliente | Pagar cada vez | Cero: paga todo junto al final |

![Figura 3. Procesar cada canción como micropago es inviable; acumular y liquidar en bloque hace rentable el modelo.](/Users/jhonytoro/Documents/emprendimiento/tym_music/docs/_build/charts/03_comision_micropago.png)

## Flujo de dinero (settlement)

- Cliente paga su cuenta completa al comercio (incluye el $ de prioridades).
- Comercio liquida en bloque (semanal/mensual) la parte de TYM.
- Recomendado: el comercio retiene su % y paga solo lo de TYM (menos fricción, más confianza).
- Riesgo de cartera: mitigar con liquidación frecuente, garantía pequeña o corte por mora.

# 6. Precio variable por comercio

El precio por prioridad NO es fijo: cada comercio lo define según su público y ticket, desde $200 COP (cafés, salas de espera) hasta $2.000 COP (locales premium / discotecas). El costo para el usuario debe sentirse muy bajo porque va sumado a su cuenta.

![Figura 4. Rango de precio por prioridad según tipo de comercio.](/Users/jhonytoro/Documents/emprendimiento/tym_music/docs/_build/charts/04_precios_tiers.png)

# 7. Estrategia legal y de fuentes de música

Principio rector: desde el principio NO meterse en problemas legales. Estrategia por fases:

| Fase | Fuente de música | Enfoque legal |
|---|---|---|
| Entrada (MVP) | Reproductores oficiales embebidos (YouTube/Spotify) con front desacoplado del back | No redistribuimos audio ni usamos APIs prohibidas; el reproductor oficial corre del lado del cliente. Se gestiona/recomienda licencia Sayco-Acinpro del local. |
| Crecimiento | Lista de respaldo desde catálogo licenciado | Reduce dependencia de fuentes externas; mayoría del tiempo suena música con derechos. |
| Madurez | Catálogo licenciado propio o revendido para uso comercial | Elimina la zona gris; diferenciador defendible (modelo Rockbot/Soundtrack). |

Importante: en Colombia toda comunicación pública de música requiere licencia de la Organización Sayco y Acinpro (OSA), con tarifa variable y negociable. TYM puede gestionarla como servicio integrado. La estrategia de fuente debe validarse con un abogado de propiedad intelectual antes de escalar.

# 8. Costos operativos

| Tipo | Costo | Notas |
|---|---|---|
| Fijo | Equipo / desarrollo | Costo dominante en etapa inicial |
| Fijo | Infraestructura cloud (tiempo real, DB) | US$50-250/mes en MVP; escala con uso |
| Variable | Fuente de música | $0 en fase embed; US$15-30/local/mes con catálogo licenciado |
| Variable | Pasarela de pago | ~2,65% + $700 por liquidación mensual por local (despreciable por canción) |
| Variable | Licencia Sayco-Acinpro | Variable; la paga el comercio, TYM puede gestionarla |

![Figura 5. Estructura de costos operativos (ilustrativa). El equipo/desarrollo domina al inicio; el catálogo licenciado entra en fase 2.](/Users/jhonytoro/Documents/emprendimiento/tym_music/docs/_build/charts/09_costos.png)

La fuente de música es la variable que más mueve el margen: empezar con embeds (costo ~$0) permite alcanzar el punto de equilibrio antes y financiar la migración a catálogo licenciado.

# 9. Unit economics

Supuestos: precio promedio ponderado $700/prioridad, ~25 prioridades/noche, ~18 noches/mes (450 prioridades/mes), split comercio 35% / TYM 65%.

| Concepto | Por local activo / mes |
|---|---|
| Recaudo bruto (450 x $700) | $315.000 |
| Parte del comercio (35%) | $110.250 |
| Ingreso bruto TYM (65%) | $204.750 |
| – Pasarela (2,65%) | ~$8.350 |
| – Música (fase embed) | $0 |
| Margen de contribución TYM (embed) | ~$196.400 |
| Margen de contribución TYM (catálogo licenciado) | ~$116.400 |

![Figura 6. Descomposición del recaudo por local activo en la fase embed.](/Users/jhonytoro/Documents/emprendimiento/tym_music/docs/_build/charts/05_unit_economics.png)

# 10. Proyecciones y punto de equilibrio

![Figura 7. Margen de contribución mensual según número de locales activos y fuente de música.](/Users/jhonytoro/Documents/emprendimiento/tym_music/docs/_build/charts/06_proyeccion_escala.png)

![Figura 8. Punto de equilibrio: ~76 locales activos en fase embed; ~129 con catálogo licenciado (asumiendo costos fijos de ~$15M COP/mes).](/Users/jhonytoro/Documents/emprendimiento/tym_music/docs/_build/charts/07_break_even.png)

La métrica crítica no es locales registrados, sino locales ACTIVOS (con prioridades pagadas por noche). Empezar con embeds reduce el punto de equilibrio casi a la mitad.

# 11. Roadmap de monetización

- Año 1 — Entrar: 100% pay-per-priority, sin cobro al comercio. Objetivo: tracción y locales activos.
- Año 2 — Crecer: introducir suscripción anual mínima que cubra gastos operativos, una vez probado el valor.
- Año 3 — Consolidar: catálogo licenciado, ecosistema de usuario (app), expansión de nichos.

![Figura 9. Evolución de ingresos: entrar gratis para el comercio y agregar suscripción al crecer (cifras ilustrativas).](/Users/jhonytoro/Documents/emprendimiento/tym_music/docs/_build/charts/08_roadmap_monetizacion.png)

# 12. Estrategia de nichos

Estrategia: atacar varios nichos gradualmente y dejar que los datos muestren dónde tracciona más, en lugar de competir de frente con Melao en bares.

| Nicho | Por qué encaja |
|---|---|
| Gimnasios / crossfit | Música central, público joven, alto engagement con "que suene mi canción" |
| Cafés / coworkings | Ambiente importa; público dispuesto a micropagar; diurno y recurrente |
| Peluquerías / barberías / spa | Sesiones largas = tiempo muerto monetizable; sin competencia |
| Food courts / cervecerías | Mucha gente, rotación, volumen |
| Salas de espera / lavanderías | Tiempo muerto, cero competencia |

# 13. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Legal de fuentes (Spotify/YouTube prohíben uso comercial) | Embeds de reproductores oficiales + migración a catálogo licenciado + gestión Sayco-Acinpro |
| Cartera: el comercio no liquida | Liquidación frecuente, garantía pequeña, corte por mora |
| Pocos pagan (ingreso depende de prioridades) | Fallback random mantiene el servicio; validar willingness-to-pay en campo |
| Disputas en la cuenta | Confirmación clara al pedir, registro, límite por usuario |
| Concepto ya existe (Melao, etc.) | Diferenciar por música dedicada + legalidad + ingreso para el comercio |
| Alta informalidad del mercado | Foco en locales activos y nichos; modelo sin costo fijo para el comercio |

# 14. Supuestos del análisis

- Precio promedio ponderado por prioridad: $700 COP (rango real $200-$2.000 según comercio).
- Local activo: ~25 prioridades/noche x ~18 noches/mes = 450 prioridades/mes.
- Split comercio 35% / TYM 65% (palanca a validar).
- Costos fijos de equipo pequeño: ~$15M COP/mes (estimado a validar).
- Comisión pasarela: 2,65% + $700 + IVA (Wompi, tarjeta; PSE 1,49%).
- Costo catálogo licenciado: US$15-30/local/mes (a confirmar con proveedores).
- Las cifras de proyección de ingresos por año son ilustrativas, no proyección financiera final.

# 15. Próximos pasos

1. Validación de campo: 10-15 entrevistas a dueños de locales en varios nichos (guion ya definido).
2. Definir con abogado PI el "sistema intermedio" legal de fuentes (embeds desacoplados).
3. Confirmar split, precio promedio y costos reales (cloud, catálogo, pasarela).
4. Construir MVP web app: cola en tiempo real + panel del dueño + QR cliente + registro de cobros a la cuenta.
5. Piloto en 1-2 nichos para medir prioridades pagadas por noche (la métrica clave).

# Fuentes

- TouchTunes / Rockbot / BarBox: business.touchtunes.com, rockbot.com, tracxn.com, pitchbook.com
- Colombia: melao.app, infobae (Apollo App), enter.co (Secret DJ), telemedellin.tv
- Legal: support.spotify.com, soundtrack.io, osa.org.co, sayco.org, acinpro.org.co, saycoacinpro.org
- Pasarelas: wompi.com, soporte.wompi.co, guiadesoftware.com
- Catálogo legal: soundtrack.io, jamendo licensing, epidemicsound.com
- Mercado: asobares.org, eltiempo.com, revistalabarra.com, acoga.org, semana.com, DANE/ACODRES
