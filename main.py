import os
import re
import time
import html
import hashlib
import sqlite3
import logging
import threading
import requests
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote_plus
from flask import Flask

# =========================================================
# CONFIGURACIÓN
# =========================================================

app = Flask(__name__)


def _env_float(nombre, defecto):
    try:
        return float(os.environ.get(nombre, defecto))
    except ValueError:
        return float(defecto)


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "507823530")

INTERVALO_ESCANEO = 60
TIEMPO_ESPERA = 10
MAX_TRABAJADORES = 8
RUTA_BD = os.environ.get("DB_PATH", "ofertas.db")
DIAS_HISTORIAL = 60

# --- Reglas para considerar un chollo "real" -------------------------------
# Descuento mínimo (calculado con los PRECIOS, no con el % que dice el título)
UMBRAL_DESCUENTO_GENERAL = _env_float("UMBRAL_DESCUENTO", 80)
# Por encima de esto se marca como "posible error de precio" (puede cancelarse)
UMBRAL_ERROR_PRECIO = 90
# Por encima de esto se asume que el precio se ha leído mal o es un PVP falso
DESCUENTO_MAXIMO_CREIBLE = 97
# Ahorro mínimo en euros (evita "90% de descuento" en un producto de 3 €)
AHORRO_MINIMO = _env_float("AHORRO_MINIMO", 10)
# Precio actual mínimo (evita cupones, gratis, precios mal parseados)
PRECIO_MINIMO = _env_float("PRECIO_MINIMO", 1)
# Diferencia máxima (en puntos) entre el % que dice el título y el calculado
TOLERANCIA_PORCENTAJE = 15
# Si es True, descarta toda oferta cuyo descuento no se pueda comprobar con precios
EXIGIR_PRECIOS_VERIFICABLES = os.environ.get("EXIGIR_PRECIOS", "1") == "1"

# --- Modelo específico -----------------------------------------------------
MODELOS_ESPECIFICOS = [r"poco\s*x8\s*pro"]
DESCUENTO_MIN_MODELO = _env_float("DESCUENTO_MIN_MODELO", 20)
# Si lo defines (ej. 300), avisa siempre que el precio sea <= a ese valor. 0 = desactivado
PRECIO_MAX_MODELO = _env_float("PRECIO_MAX_MODELO", 0)

FEEDS_RSS = [
    "https://www.chollometro.com/rss/nuevos",
    "https://www.chollometro.com/rss/tendencias",
    "https://www.chollometro.com/rss/populares",
    "https://www.chollometro.com/rss/categorias/electronica",
    "https://www.blogdechollos.com/feed/",
    "https://www.ofertitas.es/feed/",
    "https://www.chollodeals.es/feed/",
    "https://www.compradiccion.com/index.xml",
]

bloqueo_bd = threading.Lock()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# =========================================================
# SERVIDOR WEB PARA UPTIMEROBOT & RENDER
# =========================================================

@app.route("/")
def home():
    return "OK - Bot de Chollos Activo", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200

# =========================================================
# BASE DE DATOS Y DEDUPLICACIÓN
# =========================================================

def inicializar_bd():
    with sqlite3.connect(RUTA_BD, timeout=30) as conexion:
        conexion.execute("""
            CREATE TABLE IF NOT EXISTS ofertas_vistas (
                guid TEXT PRIMARY KEY,
                titulo TEXT NOT NULL,
                fecha_procesado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conexion.execute(
            "DELETE FROM ofertas_vistas WHERE fecha_procesado < datetime('now', ?)",
            (f"-{DIAS_HISTORIAL} days",),
        )
        conexion.commit()


def oferta_ya_vista(*claves):
    marcadores = ",".join("?" for _ in claves)
    with bloqueo_bd:
        with sqlite3.connect(RUTA_BD, timeout=30) as conexion:
            resultado = conexion.execute(
                f"SELECT 1 FROM ofertas_vistas WHERE guid IN ({marcadores})",
                claves,
            ).fetchone()
    return resultado is not None


def guardar_oferta(guid, titulo):
    with bloqueo_bd:
        with sqlite3.connect(RUTA_BD, timeout=30) as conexion:
            conexion.execute("""
                INSERT OR IGNORE INTO ofertas_vistas (guid, titulo)
                VALUES (?, ?)
            """, (guid, titulo))
            conexion.commit()


def huella_titulo(titulo):
    """Huella del título normalizado: evita alertas duplicadas entre webs distintas."""
    normalizado = re.sub(r"[^a-z0-9]+", "", titulo.lower())
    return "h:" + hashlib.sha1(normalizado.encode()).hexdigest()

# =========================================================
# TELEGRAM
# =========================================================

def enviar_alerta_telegram(mensaje):
    if not TELEGRAM_TOKEN:
        logger.error("Falta la variable TELEGRAM_TOKEN en Render.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    for intento in range(2):
        try:
            respuesta = requests.post(url, json=payload, timeout=10)
            if respuesta.status_code == 429 and intento == 0:
                espera = respuesta.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(min(int(espera), 30))
                continue
            if respuesta.status_code != 200:
                logger.error("Telegram HTTP %s: %s", respuesta.status_code, respuesta.text[:300])
                return False
            return respuesta.json().get("ok", False)
        except requests.RequestException as error:
            logger.error("Error de conexión con Telegram: %s", error)
            return False
    return False

# =========================================================
# EXTRACCIÓN DE FEEDS
# =========================================================

def nombre_local(etiqueta):
    return etiqueta.rsplit("}", 1)[-1].split(":")[-1]


def buscar_hijo(elemento, nombre):
    for hijo in elemento:
        if nombre_local(hijo.tag) == nombre:
            return hijo
    return None


def texto_hijo(elemento, nombre):
    hijo = buscar_hijo(elemento, nombre)
    return hijo.text.strip() if hijo is not None and hijo.text else ""


def obtener_enlace(item):
    enlace = buscar_hijo(item, "link")
    if enlace is None:
        return ""
    if enlace.attrib.get("href"):
        return enlace.attrib["href"].strip()
    return (enlace.text or "").strip()


def obtener_guid(item, enlace, titulo):
    guid = texto_hijo(item, "guid") or texto_hijo(item, "id") or enlace or f"{titulo}|{enlace}"
    return guid.strip()


def extraer_items(contenido):
    raiz = ET.fromstring(contenido)
    items = []
    for elemento in raiz.iter():
        if nombre_local(elemento.tag) in ("item", "entry"):
            titulo = texto_hijo(elemento, "title")
            enlace = obtener_enlace(elemento)
            descripcion = (
                texto_hijo(elemento, "description")
                or texto_hijo(elemento, "summary")
                or texto_hijo(elemento, "content")
            )
            guid = obtener_guid(elemento, enlace, titulo)

            # Los feeds de Chollometro/Pepper suelen traer el precio actual
            # en <pepper:merchant price="..."/>. Es opcional.
            precio_feed = None
            comerciante = buscar_hijo(elemento, "merchant")
            if comerciante is not None and comerciante.attrib.get("price"):
                coincidencia = RE_PRECIO.search(comerciante.attrib["price"])
                if coincidencia:
                    precio_feed = _valor_precio(coincidencia)

            if titulo and enlace:
                items.append({
                    "guid": guid,
                    "titulo": titulo,
                    "enlace": enlace,
                    "descripcion": descripcion,
                    "precio_feed": precio_feed,
                })
    return items

# =========================================================
# ANÁLISIS DE PRECIOS (el corazón del filtro)
# =========================================================
# Idea clave: NO nos fiamos del "-80%" que escribe el publicador.
# Calculamos el descuento nosotros con precio actual y precio anterior.

RE_PRECIO = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:€|eur\b|euros?\b)"
    r"|€\s*(\d+(?:[.,]\d{1,2})?)",
    re.I,
)

# Palabras justo antes de un precio que indican que es el precio ANTERIOR/PVP
RE_ANTERIOR = re.compile(
    r"(?:\bantes|\bpvp|\bp\.v\.p\.?|\brrp|\bvalorad[oa]\s+en|\bvalor"
    r"|\bprecio\s+(?:original|anterior|habitual|recomendado|normal|medio|de\s+lista)"
    r"|\bhabitual|\ben\s+vez\s+de|\brebajado\s+de|\bnormalmente|\bantiguo)"
    r"\s*[:\-–]?\s*\(?\s*$",
    re.I,
)
RE_DE = re.compile(r"\bde\s*$", re.I)
RE_A = re.compile(r"\s*(?:a|→|->|=>|⇒)\s*", re.I)

# Frases de marketing que no representan un descuento real de un producto
RE_ENGANOSO = re.compile(
    r"\bhasta\s+(?:el\s+|un\s+)?-?\d{1,2}\s*%"
    r"|\bdesde\s+(?:el\s+|un\s+)?-?\d{1,2}\s*%"
    r"|cashback|2[ªa]\s*unidad|segunda\s+unidad|\b[23]\s*x\s*[12]\b"
    r"|financiaci[oó]n|sin\s+intereses|\bpuntos\b|sorteo|prueba\s+gratis",
    re.I,
)

# Porcentajes que NO son un descuento sobre el producto
RE_PORC_RUIDO = re.compile(
    r"\d{1,2}\s*%\s*(?:extra|adicional|de\s+cup[oó]n|cup[oó]n|iva|de\s+bater[ií]a|de\s+inter[eé]s|algod[oó]n|poli[eé]ster)"
    r"|(?:cup[oó]n|c[oó]digo)\s*\S*\s*-?\d{1,2}\s*%",
    re.I,
)

RE_ACCESORIO = re.compile(
    r"\b(funda|carcasa|protector|cristal|templado|cable|cargador|soporte|correa|pulsera|bolsa)\b",
    re.I,
)


def _a_float(texto):
    texto = texto.strip()
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", texto):
        texto = texto.replace(".", "")
    return float(texto)


def _valor_precio(coincidencia):
    return _a_float(coincidencia.group(1) or coincidencia.group(2))


def formatear_precio(valor):
    texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if texto.endswith(",00"):
        texto = texto[:-3]
    return f"{texto}€"


def limpiar_html(texto):
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def precios_en_titulo(titulo):
    """Devuelve (precio_actual, precio_anterior); None si no es inequívoco."""
    precios = [(m.start(), m.end(), _valor_precio(m)) for m in RE_PRECIO.finditer(titulo)]
    if not precios:
        return None, None

    # Patrón explícito: "de 400€ a 79€"
    for i in range(len(precios) - 1):
        inicio, _, valor = precios[i]
        previo = titulo[max(0, inicio - 10):inicio]
        medio = titulo[precios[i][1]:precios[i + 1][0]]
        if RE_DE.search(previo) and RE_A.fullmatch(medio):
            return precios[i + 1][2], valor

    marcados, libres = [], []
    for inicio, _, valor in precios:
        previo = titulo[max(0, inicio - 30):inicio]
        (marcados if RE_ANTERIOR.search(previo) else libres).append(round(valor, 2))

    libres = sorted(set(libres))
    marcados = sorted(set(marcados))
    actual = libres[0] if len(libres) == 1 else None
    anterior = marcados[0] if len(marcados) == 1 else None
    return actual, anterior


def precio_anterior_en_descripcion(descripcion_cruda):
    """Busca SOLO precios marcados como anteriores (PVP, antes, tachados)."""
    crudo = html.unescape(descripcion_cruda or "")
    texto = limpiar_html(crudo)[:2000]
    candidatos = []

    for tachado in re.findall(r"<(?:del|s|strike)\b[^>]*>(.*?)</(?:del|s|strike)>", crudo, re.I | re.S):
        for m in RE_PRECIO.finditer(tachado):
            candidatos.append(round(_valor_precio(m), 2))

    for m in RE_PRECIO.finditer(texto):
        previo = texto[max(0, m.start() - 30):m.start()]
        if RE_ANTERIOR.search(previo):
            candidatos.append(round(_valor_precio(m), 2))

    unicos = sorted(set(candidatos))
    return unicos[0] if len(unicos) == 1 else None


def porcentaje_declarado(titulo):
    """% que anuncia el título (ignorando cupones, extras, 'hasta', etc.)."""
    texto = RE_PORC_RUIDO.sub(" ", titulo.lower())
    nums = [int(n) for n in re.findall(r"(?<!\d)(\d{1,2})\s*%", texto)]
    nums = [n for n in nums if 5 <= n <= 99]
    return max(nums, default=None)

# =========================================================
# EVALUACIÓN DE OFERTAS
# =========================================================

def evaluar_oferta(item, fuente=""):
    """Devuelve (mensaje, motivo). Si mensaje es None, motivo explica el descarte."""
    titulo = item["titulo"]
    titulo_lower = titulo.lower()

    es_modelo = (
        any(re.search(m, titulo_lower) for m in MODELOS_ESPECIFICOS)
        and not RE_ACCESORIO.search(titulo_lower)
    )

    if not es_modelo and RE_ENGANOSO.search(titulo_lower):
        return None, "frase de marketing engañosa (hasta X%, cashback, 2ª unidad...)"

    # 1) Precios
    precio, anterior = precios_en_titulo(titulo)
    if precio is None:
        precio = item.get("precio_feed")
    if anterior is None:
        anterior = precio_anterior_en_descripcion(item.get("descripcion"))

    declarado = porcentaje_declarado(titulo)

    # 2) Descuento calculado y comprobaciones de coherencia
    descuento = None
    if precio is not None and anterior is not None:
        if precio < PRECIO_MINIMO:
            return None, "precio actual demasiado bajo (cupón/gratis/mal leído)"
        if anterior <= precio:
            return None, "precio anterior no es mayor que el actual"

        descuento = (1 - precio / anterior) * 100
        ahorro = anterior - precio

        if descuento > DESCUENTO_MAXIMO_CREIBLE:
            return None, f"descuento no creíble ({descuento:.0f}%), probable error de lectura"
        if not es_modelo and ahorro < AHORRO_MINIMO:
            return None, f"ahorro ridículo ({ahorro:.2f}€)"
        if declarado is not None and abs(declarado - descuento) > TOLERANCIA_PORCENTAJE:
            return None, f"% del título ({declarado}%) no cuadra con los precios ({descuento:.0f}%)"

    # 3) Decisión
    verificado = descuento is not None
    sin_verificar = False

    if es_modelo:
        bajo_precio_max = bool(PRECIO_MAX_MODELO and precio is not None and precio <= PRECIO_MAX_MODELO)
        if verificado and descuento < DESCUENTO_MIN_MODELO and not bajo_precio_max:
            return None, f"POCO X8 Pro con descuento real bajo ({descuento:.0f}%)"
        if not verificado and not bajo_precio_max:
            if PRECIO_MAX_MODELO and precio is not None:
                return None, f"POCO X8 Pro por encima de {PRECIO_MAX_MODELO:.0f}€"
            sin_verificar = True
        etiqueta = "📱 <b>[OFERTA POCO X8 PRO]</b>"
        if verificado and descuento >= 50:
            etiqueta = f"💥 <b>[CHOLLO MÁXIMO POCO X8 PRO -{descuento:.0f}%]</b>"
    else:
        if verificado:
            if descuento < UMBRAL_DESCUENTO_GENERAL:
                return None, f"descuento real {descuento:.0f}% < umbral"
        else:
            if EXIGIR_PRECIOS_VERIFICABLES:
                return None, "descuento no verificable con precios"
            if declarado is None or declarado < UMBRAL_DESCUENTO_GENERAL:
                return None, "sin precios y sin % suficiente"
            sin_verificar = True

        if sin_verificar:
            etiqueta = f"❓ <b>[DESCUENTO -{declarado}% SIN VERIFICAR]</b>"
        elif descuento >= UMBRAL_ERROR_PRECIO:
            etiqueta = f"⚠️ <b>[POSIBLE ERROR DE PRECIO -{descuento:.0f}%]</b>"
        else:
            etiqueta = f"🚨 <b>[CHOLLO REAL -{descuento:.0f}%]</b>"

    # 4) Mensaje
    titulo_seguro = html.escape(titulo)
    enlace = item["enlace"]
    enlace_seguro = html.escape(enlace, quote=True)

    partes = [f"{etiqueta}\n", f"📌 <b>{titulo_seguro}</b>\n"]

    if verificado:
        partes.append(
            f"💶 <b>{formatear_precio(precio)}</b> <s>{formatear_precio(anterior)}</s> "
            f"(ahorras {formatear_precio(anterior - precio)})"
        )
    elif precio is not None:
        partes.append(f"💶 <b>{formatear_precio(precio)}</b> (precio anterior no verificado)")
    if sin_verificar:
        partes.append("🔎 Comprueba el precio antes de comprar.")
    if verificado and descuento >= UMBRAL_ERROR_PRECIO:
        partes.append("⏱️ Descuento extremo: puede ser un error y cancelarse el pedido.")

    enlaces = [f'🛒 <a href="{enlace_seguro}">VER OFERTA</a>']

    if "amazon" in enlace.lower() or "amazon" in titulo_lower:
        asin = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", enlace)
        if asin:
            enlaces.append(f'📊 <a href="https://es.camelcamelcamel.com/product/{asin.group(1)}">HISTÓRICO</a>')
            enlaces.append(f'📈 <a href="https://keepa.com/#!product/9-{asin.group(1)}">KEEPA</a>')
        else:
            busqueda = quote_plus(titulo[:40])
            enlaces.append(f'📊 <a href="https://es.camelcamelcamel.com/search?sq={busqueda}">HISTÓRICO</a>')

    partes.append("\n" + " | ".join(enlaces))

    if fuente:
        partes.append(f"\n<i>Fuente: {html.escape(urlparse(fuente).netloc)}</i>")

    return "\n".join(partes), "ok"

# =========================================================
# ESCANEO CONCURRENTE
# =========================================================

def descargar_feed(url):
    inicio = time.monotonic()
    try:
        respuesta = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=TIEMPO_ESPERA,
        )
        respuesta.raise_for_status()
        items = extraer_items(respuesta.content)
        logger.info("Feed OK [%s] | %s ofertas | %.2f s", url, len(items), time.monotonic() - inicio)
        return url, items
    except Exception as error:
        logger.warning("Fallo en feed [%s]: %s", url, error)
        return url, []


def procesar_oferta(item, fuente):
    guid = item["guid"]
    huella = huella_titulo(item["titulo"])
    if oferta_ya_vista(guid, huella):
        return

    mensaje, motivo = evaluar_oferta(item, fuente)
    if mensaje is None:
        logger.debug("Descartada (%s): %s", motivo, item["titulo"])
        return

    if enviar_alerta_telegram(mensaje):
        guardar_oferta(guid, item["titulo"])
        guardar_oferta(huella, item["titulo"])
        logger.info("Alerta enviada desde %s: %s", fuente, item["titulo"])
        time.sleep(0.5)


def escanear_chollos():
    inicio = time.monotonic()
    resultados = []

    with ThreadPoolExecutor(max_workers=MAX_TRABAJADORES) as executor:
        tareas = {executor.submit(descargar_feed, url): url for url in FEEDS_RSS}
        for tarea in as_completed(tareas):
            try:
                fuente, items = tarea.result()
                resultados.append((fuente, items))
            except Exception:
                logger.exception("Error procesando tarea de feed.")

    total = 0
    for fuente, items in resultados:
        for item in items:
            try:
                procesar_oferta(item, fuente)
            except Exception:
                logger.exception("Error evaluando oferta: %s", item.get("titulo"))
            total += 1

    logger.info("Ciclo finalizado | %s ofertas analizadas | %.2f s", total, time.monotonic() - inicio)


def bucle_escaneo():
    logger.info("Iniciando escáner de ofertas con verificación por precios.")
    while True:
        inicio = time.monotonic()
        try:
            escanear_chollos()
        except Exception:
            logger.exception("Error en el bucle principal de escaneo.")
        duracion = time.monotonic() - inicio
        time.sleep(max(0, INTERVALO_ESCANEO - duracion))

# =========================================================
# EJECUCIÓN PRINCIPAL
# =========================================================

inicializar_bd()

# Iniciar el escáner de chollos en segundo plano
threading.Thread(target=bucle_escaneo, daemon=True).start()

# El proceso principal ejecuta Flask en el puerto asignado por Render
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
