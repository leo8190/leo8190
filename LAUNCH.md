# Plan de lanzamiento — 7 días para validar o matar

El sitio está listo desde hace 40 días. Lo único que falta es tráfico.
Este documento es el plan de distribución. No toques el código hasta terminarlo.

---

## Estado del canal de datos: YA VERIFICADO ✅

Confirmado revisando el correo (25 jun 2026): la cuenta de Formspree es de
leonardo23322@gmail.com y el ID `xvzjkwvb` (form "ClickReports Beta") funciona.
Está en plan free: **50 envíos/mes**. No hace falta crear nada.

Además, los eventos clave (`checkout_attempt` y `pricing_tier_click`) ahora
**llegan a tu inbox al instante**, con el canal de origen, el plan y el ID del
visitante. No necesitás PostHog para correr el test — con el inbox alcanza.

### Historial real de submissions (a 3 ago 2026)

| Fecha | Sitio | Qué fue |
|---|---|---|
| 25 jun | leo8190.vercel.app | tu propio email — autotest |
| **29 jun** | **clickreports.vercel.app** | **`trob4077@gmail.com` — lead real** |
| 20 jul | tryreportly.netlify.app | `test-smoke@…` — autotest |
| 25 jul | tryreportly.netlify.app | `checkout_attempt` pro, `utm_source=reddit_test` — autotest |

**1 lead real en 40 días.** No es un "no" del mercado: es falta de tráfico.

### Día 0 (hoy, 15 min)

1. **Elegí UNA URL y matá el resto.** Hay tres sitios vivos (leo8190.vercel.app,
   clickreports.vercel.app, tryreportly.netlify.app). Dejá una sola, con el
   producto Notion, y anotala acá: `___________________`
2. **Escribile a `trob4077@gmail.com`.** Es la única persona que levantó la mano
   sin que se lo pidieras. Preguntale qué le llamó la atención y cómo hace hoy
   los reportes. Una respuesta suya vale más que 100 visitas.
3. *(Opcional)* PostHog gratis en `posthogKey` si querés tasas de conversión
   finas. No es bloqueante.

---

## Días 1-5 — Tráfico. 20 conversaciones, no 1 post masivo

Para un smoke test, **20 DMs bien dirigidos valen más que un post en r/Notion**
(donde además la autopromoción se banea). El objetivo no es tráfico: es
descubrir si alguien tiene el dolor lo bastante fuerte como para pagar.

### Dónde están (agencias/consultores que YA usan Notion)

- **LinkedIn**: buscá `"Notion" agency owner`, `"we run on Notion"`,
  `consultant Notion`. Filtrá por dueños de agencias chicas (2-20 personas).
- **Twitter/X**: buscá `Notion client reporting`, `Notion agency`,
  gente que postea capturas de sus dashboards de Notion.
- **Comunidades**: Notion Ambassadors, r/Notion (solo para *escuchar*, no vender),
  Slack/Discord de agencias, Indie Hackers, grupos de "Notion consultants" en Facebook.
- **Notion Certified Consultants**: hay un directorio público. Son literalmente
  gente que cobra por montar Notion a clientes → tienen el dolor exacto.

### Plantilla de DM (personalizá la primera línea SIEMPRE)

> Hola [nombre], vi que [dato concreto: tu post sobre el dashboard de Notion /
> que llevás proyectos de clientes en Notion].
>
> Estoy armando algo para agencias que trabajan en Notion: convertir una base de
> datos en un PDF de reporte con tu marca, listo para el cliente, sin
> copiar y pegar a mano.
>
> ¿Hoy cómo hacés los reportes mensuales de tus clientes? ¿Es un dolor real o
> ya lo tenés resuelto?

**Clave:** no mandes el link en el primer mensaje. Preguntá primero. Si contesta
que sí es un dolor → ahí mandás el link. Las respuestas te dicen más que los clics.

### Meta diaria

| Día | Acción | Meta |
|---|---|---|
| 1 | Armar lista de 40 prospectos (nombre + link + dato personal) | 40 |
| 2 | Mandar 20 DMs (LinkedIn + Twitter) | 20 |
| 3 | Mandar 20 DMs más + responder los que contestaron | 20 |
| 4 | Postear en 2 comunidades preguntando (no vendiendo) | 2 |
| 5 | Seguimiento a los que no contestaron | — |

Usá links etiquetados para saber qué canal funciona:
`tusitio.com/?utm_source=linkedin&utm_campaign=dm-v1`

---

## Días 6-7 — Leer los datos y decidir

Abrí la consola del navegador en el sitio y corré `stats()`, o mirá PostHog.

### Umbral de éxito (definido de antemano, no se negocia después)

**VALIDADO** si se cumple alguna:
- ≥3 visitantes no relacionados llegan a `checkout_attempt`
- ≥10% del tráfico cualificado hace clic en un plan
- ≥2 personas responden a un DM diciendo "sí, lo pagaría / cuándo sale"

**NO VALIDADO** si después de ~40 conversaciones reales:
- 0-1 checkout attempts
- Nadie contesta con interés genuino en pagar

### Si NO valida

No pivotees a otro producto de una. Primero preguntate cuál de estas fue:

1. **Nadie tiene el dolor** → el nicho está mal. Ahí sí, cambiar de nicho.
2. **Tienen el dolor pero no pagarían $19-49** → problema de precio/propuesta.
3. **Les interesó pero no llegaron al checkout** → problema de landing, no de idea.
4. **No conseguiste ni 40 conversaciones** → *no corriste el test*. No hay dato.
   Este es el caso más probable si te quedás a mitad.

**Solo el caso 1 justifica cambiar de nicho.**

---

## Regla para este proyecto

> No se toca una línea de copy, ni se cambia de nicho, hasta que 40 personas
> reales hayan visto esto.

El 27 de junio hubo 17 commits en un día puliendo texto que nadie leyó nunca.
Ese es el modo de fallar: se siente productivo y no genera información.
