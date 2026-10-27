# API de RTVE (audio) — notas

La API de RTVE **no es pública ni está documentada**. Estas notas salen del funcionamiento de
rtve.es, de proyectos de la comunidad y de lo que usa esta app (`rtve.py`). Puede cambiar sin
aviso; `diagnostico.bat` indica qué paso falla si deja de funcionar.

Leyenda de fiabilidad: ✅ lo usa la app y funciona · ⚠️ conocido pero sin verificar a fondo.

## Generalidades

- **Base:** `https://www.rtve.es/api/` (algunos proyectos usan `https://api2.rtve.es/api/`,
  que sirve los mismos recursos).
- **Formato** según la extensión: `.json`, `.xml` y, en algunos recursos, `.rss`.
- **Acceso:** solo `GET`, sin autenticación ni clave. Conviene enviar un `User-Agent` de
  navegador normal y aceptar `gzip`.
- **Paginación:** `?page=N&size=M` (empieza en 1). Los listados vienen en un sobre común:

```json
{
  "page": {
    "items": [ ... ],
    "number": 1,
    "size": 50,
    "offset": 0,
    "total": 734,
    "totalPages": 15,
    "numElements": 50
  }
}
```

Las consultas de un solo recurso (`/audios/{id}.json`) suelen venir también dentro de
`page.items`, con un único elemento.

## Endpoints de audio

| Endpoint | Devuelve | |
|---|---|---|
| `GET /api/programas/{id}.json` | Ficha del programa | ✅ |
| `GET /api/programas/{id}/audios.json?page=&size=` | Episodios, del más nuevo al más antiguo | ✅ |
| `GET /api/programas/{id}/audios.rss` | Los mismos episodios como podcast RSS | ✅ |
| `GET /api/audios/{id}.json` | Un episodio concreto | ✅ |

### Ficha del programa

| Campo | Contenido |
|---|---|
| `name` / `title` | Nombre del programa |
| `description` | Descripción en HTML (a veces `longDescription` / `shortDescription`) |
| `imgPoster` | Imagen (también aparecen `thumbnail`, `imgCol`, `image`) ⚠️ |

### Episodio (JSON)

| Campo | Contenido |
|---|---|
| `id` | Id numérico del audio, normalmente de 7 cifras |
| `title`, `longTitle` | Título corto y largo |
| `description` | HTML, a menudo con la tracklist en `<ul>` |
| `publicationDate` | Texto `"dd-mm-aaaa HH:MM:SS"` (hora de Madrid) |
| `duration` | Duración en **milisegundos** |
| `htmlUrl` | Página del episodio en rtve.es |
| URL del audio | Su posición varía según la época del contenido: `qualities[].filePath` u otros campos anidados ⚠️ |

Por eso la app no se fía de un campo fijo para el audio: recorre la respuesta y se queda con la
primera URL `.mp3` (o `.m4a` / `.m3u8`). Si no hay ninguna, consulta `/api/audios/{id}.json`
y, como último recurso, la página del episodio.

### Episodio (RSS)

Formato podcast estándar, más estable que el JSON:

| Elemento | Contenido |
|---|---|
| `<title>` | Título |
| `<enclosure url="…">` | URL del mp3 |
| `<pubDate>` | Fecha RFC 822 (`Tue, 27 Oct 2026 14:00:00 +0100`) |
| `<itunes:duration>` | `HH:MM:SS` |
| `<description>` / `<itunes:summary>` | Descripción (HTML escapado) |
| `<guid>`, `<link>` | Contienen el id del episodio (último número de la URL) |

## Vídeo ⚠️

- `GET /api/videos/{id}.json` — metadatos de un vídeo.
- `GET /api/videos/{id}/subtitulos.json` — subtítulos VTT en varios idiomas
  (`items[].src`, `items[].lang`).

Los usa la librería [rtve-go](https://pkg.go.dev/github.com/rubiojr/rtve-go) sobre `api2.rtve.es`.

## Lo que no está en la API (se saca del HTML de la web)

### Listado de programas de una cadena

`https://www.rtve.es/play/radio/a-z/radio-3/` (cambiando `radio-3` por otra cadena). No hay
un endpoint JSON fiable conocido, así que `rtve.fetch_programs()`:

1. Lee los enlaces `/play/audios/<slug>/` (un solo segmento = programa; más segmentos = episodio).
2. Toma el nombre de `title` o `aria-label` del enlace. RTVE escribe ahí cosas como
   `Ir a la página de 'Turbo 3'`, que `rtve.clean_title()` deja en `Turbo 3`.
3. Sigue los enlaces de paginación o de letras que cuelgan de la misma ruta.

### Del slug al id

Las URLs públicas usan *slug* (`/play/audios/turbo-3/`), pero la API necesita el id
numérico. Está incrustado en el HTML de la página del programa; `rtve.parse_program_page()`
lo busca en, por este orden:

- llamadas a `/api/programas/{id}`
- `"idPrograma": "{id}"`, `programId`, `id_programa`
- atributos `data-idprograma`, `data-program-id`…

La misma página trae `og:title`, `og:description` y `og:image`.

### Página de un episodio

`/play/audios/{slug-programa}/{slug-episodio}/{id}/`. El último número es el id del audio.

## Directo ⚠️

Los directos de RNE se sirven en HLS (`.m3u8`) desde `rtvelivestream.akamaized.net`. La URL
de Radio 3 (`…/rtvesec/rne/rne_r3_main.m3u8`) es la parte menos segura: por eso está en
`%APPDATA%\Radio3Player\config.json` (`live_urls`, se prueban en orden) y se puede cambiar sin
tocar el código.

## Cosas a tener en cuenta

- **Los nombres de campo varían** entre programas y épocas (`title`/`name`,
  `description`/`longDescription`, milisegundos/segundos). Lee siempre con alternativas.
- **Las descripciones vienen en HTML** y con entidades (`&amp;`, `&#39;`).
- **Fechas** sin zona horaria en el JSON: son hora peninsular española.
- **Uso razonable:** la app cachea el listado de programas 24 h y los episodios 30 min.
