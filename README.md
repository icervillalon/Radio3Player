# Radio 3 Player

Reproductor de escritorio ligero para los programas de Radio 3 (RTVE): sin navegador,
con reanudación automática de cada episodio y ocupando 40-60 MB de RAM frente a los más de 200 de escuchar desde la web.

## Instalar (Windows)

### Requisitos
- Python 3.10 o superior
- VLC 

### Instalación

1. Doble clic en **`instalar.bat`**. Instala `python-vlc` y los controles multimedia de Windows, comprueba que tienes **VLC de 64 bits**
   (si no, intenta instalarlo con `winget`) y crea un acceso directo «Radio 3» en el escritorio.
2. Abre la app desde el escritorio o con **`Radio3.bat`**.

Si algo no carga, ejecuta **`diagnostico.bat`** (o `diagnostico.bat el-sotano` para otro programa)
y copia lo que salga: dice qué paso falla (listado, programa, episodios o VLC).

## Qué hace

- **Todos los programas de Radio 3** (listado A-Z de rtve.es) con buscador y **favoritos** arriba.
- **Descripción del programa** y de cada episodio (fecha, duración, tracklist si lo publica RTVE).
- **Reanuda donde lo dejaste**: guarda la posición cada 5 s y al cerrar. Estado en la lista:
  `◐ 34%` a medias, `✓` escuchado, `♪` sonando.
- **Continuar escuchando**: todos los episodios a medias, del más reciente al más antiguo.
- **Radio 3 en directo**.
- Velocidad 0.75×–2×, ±15/30 s, volumen. Clic derecho en un episodio: empezar de cero,
  marcar como escuchado, abrir en rtve.es o copiar el enlace del mp3.
- Si un programa no aparece en el listado: **+ Añadir por URL** y pega su página
  (`https://www.rtve.es/play/audios/<programa>/`).

**Ir a un minuto concreto**: haz clic en cualquier punto de la barra (al pasar el ratón ves el
tiempo exacto) o arrástrala. También puedes hacer clic en el tiempo actual, o pulsar `Ctrl+G`,
y escribir `45` (minuto 45), `12:30` o `1:05:00`. La rueda del ratón sobre la barra salta ±30/15 s.

**Integración con Windows**:

- **Teclas multimedia** del teclado: ⏯ reproducir/pausa, ⏹ pausa (en el directo, corta la
  emisión), ⏭ +30 s y ⏮ −15 s. Funcionan aunque la ventana no esté en primer plano.
- **Panel multimedia de Windows**: muestra el episodio, el programa y su imagen, con sus botones y su
  barra de posición.
- **Botones en la miniatura** de la barra de tareas: −15 s, reproducir/pausa y +30 s.

Si algo de esto no aparece, `diagnostico.bat` dice qué modo de teclas multimedia está activo y
los errores quedan en `%APPDATA%\Radio3Player\app.log`.

Atajos: `Espacio` reproducir/pausa · `Ctrl+→` +30 s · `Ctrl+←` −15 s · `Ctrl+G` ir a… · `Ctrl+F` buscar.

## Cambiar el icono

Arrastra cualquier imagen (SVG, PNG, JPG, WEBP, ICO) encima de **`cambiar_icono.bat`**, o guárdala
como `logo.svg` o `logo.png` en esta carpeta y haz doble clic en el .bat. La recorta, la centra en un
cuadrado, genera `icono.ico` en todos los tamaños y actualiza el acceso directo del escritorio.
Para volver al icono de la app: `cambiar_icono.bat original`.

## Consumo

Interfaz Tkinter + motor de audio de VLC: en torno a 60–90 MB de RAM reproduciendo,
frente a los 300–500 MB habituales de una pestaña de RTVE Play en Chrome.

## Datos y ajustes

Se guardan en `%APPDATA%\Radio3Player\`:

- `estado.json`: progreso de escucha, favoritos, volumen y velocidad.
- `config.json`: `live_urls` (URLs del directo, se prueban en orden) y `network_caching_ms`.
- `cache\`: listado de programas (24 h) y episodios (30 min). El botón ↻ fuerza la recarga.

## Aviso

Detalles de la API en [API.md](API.md).

Usa la API pública no documentada de rtve.es, la misma que usa su web. Si RTVE la cambia,
puede dejar de funcionar alguna parte; `diagnostico.bat` indica cuál. Uso personal.
