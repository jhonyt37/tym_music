#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Genera las graficas para el documento de analisis de mercado de TYM Music."""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import numpy as np

OUT = os.path.join(os.path.dirname(__file__), "charts")
os.makedirs(OUT, exist_ok=True)

# Paleta
AZUL = "#1f4e79"
AZUL2 = "#2e75b6"
NARANJA = "#ed7d31"
VERDE = "#548235"
ROJO = "#c00000"
GRIS = "#808080"
plt.rcParams.update({"font.size": 11, "axes.edgecolor": "#666666",
                     "axes.grid": True, "grid.alpha": 0.25, "figure.dpi": 130})

def cop(x, pos=None):
    return f"${x/1_000_000:.0f}M" if abs(x) >= 1_000_000 else f"${x/1000:.0f}k"

def save(fig, name):
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, name), bbox_inches="tight")
    plt.close(fig)

# 1. Crecimiento del sector bares/discotecas 2025
fig, ax = plt.subplots(figsize=(6.2, 3.4))
cats = ["Ocupacion\n(+13,6%)", "Ingresos prom.\n(+64,3%)", "Sector total\n(+1,9%)"]
vals = [13.6, 64.3, 1.9]
bars = ax.bar(cats, vals, color=[AZUL2, VERDE, GRIS])
ax.set_ylabel("Crecimiento real %")
ax.set_title("Sector bares y discotecas en Colombia — crecimiento 2025", fontweight="bold")
for b, v in zip(bars, vals):
    ax.text(b.get_x()+b.get_width()/2, v+1, f"{v}%", ha="center", fontweight="bold")
ax.set_ylim(0, 72)
save(fig, "01_crecimiento_sector.png")

# 2. Mapa de posicionamiento (oceano azul) 2x2
fig, ax = plt.subplots(figsize=(6.4, 5.0))
# x = foco/calidad en musica como producto ; y = legalidad / defensibilidad
players = {
    "Melao":        (3.0, 2.5, NARANJA),
    "Apollo App":   (4.5, 2.8, NARANJA),
    "Secret DJ":    (4.0, 2.2, NARANJA),
    "TouchTunes":   (8.5, 7.0, GRIS),
    "Rockbot":      (7.0, 9.0, GRIS),
    "TYM Music":    (8.5, 8.5, ROJO),
}
for name,(x,y,c) in players.items():
    ax.scatter(x, y, s=320 if name=="TYM Music" else 180, color=c,
               edgecolors="black", zorder=3, alpha=0.9)
    ax.annotate(name, (x,y), xytext=(6,8), textcoords="offset points",
                fontweight="bold" if name=="TYM Music" else "normal", fontsize=10)
ax.axhline(5, color="#aaaaaa", lw=1); ax.axvline(5, color="#aaaaaa", lw=1)
ax.set_xlim(0,10); ax.set_ylim(0,10)
ax.set_xlabel("Producto de musica dedicado / monetizable  ->")
ax.set_ylabel("Legalidad / defensibilidad  ->")
ax.set_title("Mapa de posicionamiento — el oceano azul de TYM", fontweight="bold")
ax.text(1.2, 9.3, "Competencia local\n(zona gris, musica secundaria)", fontsize=8, color=NARANJA)
ax.text(5.4, 1.0, "Objetivo TYM:\nmusica dedicada + legal", fontsize=8, color=ROJO)
save(fig, "02_posicionamiento.png")

# 3. El problema del micropago: comision por cancion vs acumulado
fig, ax = plt.subplots(figsize=(6.2, 3.4))
modos = ["Micropago por cancion\n($1.500 c/u)", "Acumulado a la cuenta\n(liquidacion mensual)"]
pct = [55, 3]
bars = ax.bar(modos, pct, color=[ROJO, VERDE])
for b,v in zip(bars, pct):
    ax.text(b.get_x()+b.get_width()/2, v+1.5, f"~{v}%", ha="center", fontweight="bold")
ax.set_ylabel("% del cobro que se come la comision")
ax.set_title("Por que 'sumar a la cuenta' es clave", fontweight="bold")
ax.set_ylim(0, 65)
save(fig, "03_comision_micropago.png")

# 4. Precio variable por cancion segun comercio
fig, ax = plt.subplots(figsize=(6.4, 3.4))
tiers = ["Cafe / espera\n(bajo)", "Gym / barberia\n(medio)", "Bar / cerveceria\n(alto)", "Disco / top\n(premium)"]
pmin = [200, 500, 1000, 1500]
pmax = [500, 1000, 1500, 2000]
x = np.arange(len(tiers))
ax.bar(x, np.array(pmax)-np.array(pmin), bottom=pmin, color=AZUL2, width=0.5)
for i,(lo,hi) in enumerate(zip(pmin,pmax)):
    ax.text(i, hi+40, f"${hi:,}".replace(",","."), ha="center", fontsize=9)
    ax.text(i, lo-90, f"${lo:,}".replace(",","."), ha="center", fontsize=9, color=GRIS)
ax.set_xticks(x); ax.set_xticklabels(tiers)
ax.set_ylabel("Precio por prioridad (COP)")
ax.set_title("Precio por cancion: variable, lo fija cada comercio ($200–$2.000)", fontweight="bold")
ax.set_ylim(0, 2300)
save(fig, "04_precios_tiers.png")

# 5. Unit economics por local activo (waterfall) - fase embed (musica $0)
fig, ax = plt.subplots(figsize=(6.6, 3.6))
# avg $700 * 25 * 18 = 315.000
recaudo = 315_000
comercio = -int(recaudo*0.35)
pasarela = -int(recaudo*0.0265)
musica = 0
labels = ["Recaudo\nbruto", "Comercio\n(35%)", "Pasarela\n(2,65%)", "Musica\n(embed=$0)", "Margen\nTYM"]
vals = [recaudo, comercio, pasarela, musica]
margen = recaudo+comercio+pasarela+musica
cum = [0]
for v in vals[:-1]:
    cum.append(cum[-1]+v)
colors = [AZUL, NARANJA, NARANJA, NARANJA]
for i,(v,c) in enumerate(zip(vals,colors)):
    base = cum[i] if v>=0 else cum[i]+v
    ax.bar(i, abs(v), bottom=base, color=c if v>=0 else "#f0a868")
ax.bar(len(vals), margen, color=VERDE)
ax.set_xticks(range(len(labels))); ax.set_xticklabels(labels)
ax.yaxis.set_major_formatter(FuncFormatter(cop))
ax.set_title("Unit economics por local activo / mes (fase embed)", fontweight="bold")
ax.text(len(vals), margen+8000, cop(margen), ha="center", fontweight="bold", color=VERDE)
ax.text(0, recaudo+8000, cop(recaudo), ha="center", fontsize=9)
save(fig, "05_unit_economics.png")

# 6. Proyeccion de margen de contribucion por escala (embed vs licenciado)
fig, ax = plt.subplots(figsize=(6.6, 3.6))
locales = [50, 150, 400]
recaudo_u = 315_000
tym_share = 0.65
contrib_embed_u = recaudo_u*tym_share - recaudo_u*0.0265 - 0      # ~196.500
contrib_lic_u   = recaudo_u*tym_share - recaudo_u*0.0265 - 80_000 # ~116.500
embed = [l*contrib_embed_u for l in locales]
lic = [l*contrib_lic_u for l in locales]
x = np.arange(len(locales)); w=0.35
ax.bar(x-w/2, embed, w, label="Fase embed (musica $0)", color=AZUL2)
ax.bar(x+w/2, lic, w, label="Fase catalogo licenciado", color=VERDE)
ax.set_xticks(x); ax.set_xticklabels([f"{l} locales\nactivos" for l in locales])
ax.yaxis.set_major_formatter(FuncFormatter(cop))
ax.set_ylabel("Margen de contribucion / mes")
ax.set_title("Margen de contribucion mensual por escala", fontweight="bold")
ax.legend(fontsize=8)
save(fig, "06_proyeccion_escala.png")

# 7. Punto de equilibrio (break-even)
fig, ax = plt.subplots(figsize=(6.6, 3.6))
n = np.arange(0, 200)
fijos = 15_000_000
ax.plot(n, n*contrib_embed_u, color=AZUL2, lw=2, label="Margen contrib. (embed)")
ax.plot(n, n*contrib_lic_u, color=VERDE, lw=2, label="Margen contrib. (licenciado)")
ax.axhline(fijos, color=ROJO, ls="--", lw=1.8, label="Costos fijos (~$15M/mes)")
be_embed = fijos/contrib_embed_u
be_lic = fijos/contrib_lic_u
ax.axvline(be_embed, color=AZUL2, ls=":", alpha=0.7)
ax.axvline(be_lic, color=VERDE, ls=":", alpha=0.7)
ax.text(be_embed+2, fijos*0.4, f"~{be_embed:.0f}\nlocales", color=AZUL2, fontsize=8)
ax.text(be_lic+2, fijos*0.7, f"~{be_lic:.0f}\nlocales", color=VERDE, fontsize=8)
ax.yaxis.set_major_formatter(FuncFormatter(cop))
ax.set_xlabel("Locales activos"); ax.set_ylabel("$ / mes")
ax.set_title("Punto de equilibrio segun fuente de musica", fontweight="bold")
ax.legend(fontsize=8, loc="upper left")
save(fig, "07_break_even.png")

# 8. Roadmap de monetizacion (entrada -> suscripcion)
fig, ax = plt.subplots(figsize=(6.6, 3.6))
periodos = ["Ano 1\n(entrar)", "Ano 2\n(crecer)", "Ano 3\n(consolidar)"]
priority = [180, 600, 1200]   # millones COP/ano (ilustrativo)
suscrip = [0, 120, 360]
x = np.arange(len(periodos))
ax.bar(x, priority, color=AZUL2, label="Pay-per-priority")
ax.bar(x, suscrip, bottom=priority, color=NARANJA, label="Suscripcion anual comercio")
for i,(p,s) in enumerate(zip(priority,suscrip)):
    ax.text(i, p+s+20, f"${p+s}M", ha="center", fontweight="bold", fontsize=9)
ax.set_xticks(x); ax.set_xticklabels(periodos)
ax.set_ylabel("Ingresos COP/ano (millones, ilustrativo)")
ax.set_title("Roadmap de monetizacion: entrar gratis -> suscripcion despues", fontweight="bold")
ax.legend(fontsize=8); ax.set_ylim(0, 1750)
save(fig, "08_roadmap_monetizacion.png")

# 9. Desglose de costos operativos (pie)
fig, ax = plt.subplots(figsize=(5.8, 4.2))
labels = ["Equipo / desarrollo", "Infraestructura cloud", "Pasarela de pago",
          "Soporte / onboarding", "Legal / Sayco-Acinpro", "Catalogo (fase 2)"]
sizes = [55, 12, 6, 8, 7, 12]
colors = [AZUL, AZUL2, NARANJA, "#f0a868", GRIS, VERDE]
ax.pie(sizes, labels=labels, autopct="%1.0f%%", colors=colors,
       startangle=90, textprops={"fontsize":8})
ax.set_title("Estructura de costos operativos (ilustrativa)", fontweight="bold")
save(fig, "09_costos.png")

print("OK - graficas generadas en", OUT)
print(f"break-even embed: {be_embed:.0f} | licenciado: {be_lic:.0f}")
print(f"contrib/u embed: {contrib_embed_u:,.0f} | lic: {contrib_lic_u:,.0f}")
