# Estrategia revisada — de SaaS a servicio

Fecha: 2026-08-03. Escrito después de revisar los datos reales del proyecto
(4 submissions en 40 días, 1 lead real) y el mercado de consultores de Notion.

---

## El hallazgo que cambia el plan

Fuentes públicas sobre el mercado de consultoría de Notion (ago 2026):

- Un consultor de Notion cobra **US$75–250/hora**, o **US$999–10.000 por proyecto**.
- Notion tiene limitaciones reconocidas justamente en **reporting de agencias
  con múltiples clientes simultáneos** — el dolor existe y está documentado por
  terceros, no es una hipótesis nuestra.

### Por qué esto invalida el pricing actual

| | Starter $19/mes | Pro $49/mes |
|---|---|---|
| Valor que entrega (4 h/mes a $150/h) | $600 | $600 |
| Lo que cobramos | $19 | $49 |
| % del valor capturado | 3% | 8% |

El problema no es que sea caro. Es que **es tan barato que no genera urgencia**.
Alguien que factura $150/hora no mueve un dedo por ahorrar $49. Para ese público,
un precio bajo es señal de que la herramienta es un juguete.

**Hipótesis a testear (no cambiar sin datos):** el Pro debería estar en
US$99–199/mes, o directamente cobrarse por proyecto.

---

## El camino recomendado: vender el servicio primero

El smoke test valida con clics. **Vender el servicio valida con dinero**, que es
evidencia infinitamente más fuerte — y encima cobrás mientras validás.

### La jugada

En vez de esperar a que un SaaS de $19 consiga 100 visitantes, vendé el
**servicio** que ese SaaS automatiza:

> "Te monto el sistema de reportes de cliente en tu Notion:
> base de datos conectada, plantilla con tu marca, y el PDF listo para enviar.
> Proyecto cerrado, US$400–900."

Todo el research del nicho ya está hecho. El landing ya existe y sirve de
portfolio. Lo único nuevo es cobrar.

### Por qué es mejor que seguir con el SaaS solo

| | SaaS $19-49/mes | Servicio $400-900/proyecto |
|---|---|---|
| Tiempo hasta el primer peso | meses | **días** |
| Clientes necesarios para $1.000 | 20-50 | **2** |
| Producto que hay que construir antes | todo | **nada** |
| Calidad de la validación | un clic | **pagó** |

### Y el puente al producto

Si vendés el servicio 5 veces y las 5 hacés exactamente lo mismo (conectar la
base, aplicar plantilla, exportar PDF), **eso es la validación definitiva del
SaaS** — con 5 clientes pagando y sabiendo exactamente qué automatizar. Ahí sí
se justifica construirlo, y ya tenés los primeros 5 usuarios.

Es el mismo producto. Solo cambia el orden: cobrar primero, automatizar después.

---

## Dónde están los clientes (fuentes verificadas)

No pude extraer los nombres desde este entorno (el proxy bloquea el acceso a los
directorios). Estas son las fuentes reales, hay que abrirlas a mano:

1. **Directorio oficial de Notion** — https://www.notion.com/explore-consultants
   Consultores certificados. Cobran caro y tienen lista de espera → tienen más
   trabajo del que pueden hacer → el dolor de reportar es real para ellos.
2. **Notion Consultants Directory** — https://notionconsultants.com/consultants/
3. **Notion Solutions Partner Program** — https://www.notion.com/partners/solutions-partner-program
   Agencias e integradores. El perfil exacto.
4. **LinkedIn** — buscar `"Notion consultant"`, `"Notion agency"`,
   `"we run on Notion"`. Filtrar por agencias de 2-20 personas.
5. **Agency Supply** — https://agencysupply.co/ publica sobre agencias + Notion.

### Cómo armar la lista (30 min, una sola vez)

Abrí las fuentes 1-3, y por cada consultor anotá: nombre, sitio, y **un dato
concreto** (a qué se dedica, qué postea). Meta: 40 filas. Sin el dato concreto
el mensaje se vuelve spam y no contesta nadie.

---

## Mensaje para vender el servicio (no el SaaS)

> Hola [nombre], vi que [dato concreto: montás Notion para agencias / tu caso
> de estudio de X].
>
> Pregunta corta: cuando tus clientes te piden el reporte mensual, ¿cómo lo
> resolvés hoy? Lo pregunto porque armé un sistema que convierte una base de
> Notion en un PDF con la marca del cliente, y estoy buscando 2-3 agencias
> para montárselo a precio de arranque.
>
> Si te sirve te muestro cómo queda. Si no, ignorame tranquilo.

**Clave:** ofrece *hacer el trabajo*, no vender software. Es lo que ese público
ya está acostumbrado a comprar.

---

## Umbral de decisión (7 días)

- **2 respuestas interesadas** → agendá llamadas. Hay negocio.
- **1 venta cerrada** → el nicho está validado con dinero. Seguí por acá.
- **0 respuestas sobre 40 mensajes** → el problema no es el precio ni el
  producto: es el canal. Probá otro (comunidades, contenido) antes de matar el nicho.

---

## Lo que NO hay que hacer

Abrir un cuarto proyecto. Hay tres smoke tests vivos (ClickReports en Vercel,
Reportly en Netlify, este) y entre los tres suman 1 lead real. El problema nunca
fue la idea. Matá dos, quedate con uno, y salí a cobrar.
