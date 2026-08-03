# Plan de lanzamiento — 7 días para validar o matar

El sitio está listo desde hace 40 días. Lo único que falta es tráfico.
Este documento es el plan de distribución. No toques el código hasta terminarlo.

---

## Día 0 (hoy, 30 min) — Dejar el canal de datos verificado

1. **Confirmá la URL de producción.** Si Vercel sigue conectado, el sitio ya está
   en línea. Buscá la URL y anotala acá: `___________________`
2. **Verificá que los emails te lleguen a vos.** Entrá a la URL, hacé el flujo
   completo (plan → "Start paid beta" → poné tu propio email → enviar).
   - Si te llega un mail de Formspree a leonardo23322@gmail.com → el form es tuyo. Listo.
   - Si no llega en 5 minutos → creá uno nuevo en formspree.io y cambiá
     `formspreeId` en `assets/config.js`.
3. **Opcional pero recomendado:** creá cuenta gratis en posthog.com y pegá la
   Project API Key (`phc_...`) en `posthogKey`. Es lo único que te da tasas de
   conversión reales entre visitantes distintos.

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
