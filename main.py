import os
import re
import time
import html
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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "507823530")

INTERVALO_ESCANEO = 60
TIEMPO_ESPERA = 10
MAX_TRABAJADORES = 8
RUTA_BD = os.environ.get("DB_PATH", "ofertas.db")

# Nuevo umbral general: 80% o más de descuento
UMBRAL_DESCUENTO_GENERAL = 80

# Palabras clave del modelo específico solicitado
MODELOS_ESPECIFICOS = ["poco x8 pro", "poco x8pro"]

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
    level=logging.INFO,
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
        conexion.commit()

def oferta_ya_vista(guid):
    with bloqueo_bd:
        with sqlite3.connect(RUTA_BD, timeout=30) as conexion:
            resultado = conexion.execute(
                "SELECT 1 FROM ofertas_vistas WHERE guid = ?",
                (guid,)
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

    try:
        respuesta = requests.post(url, json=payload, timeout=10)
        if respuesta.status_code != 200:
            logger.error("Telegram HTTP %s: %s", respuesta.status_code, respuesta.text[:300])
            return False
        return respuesta.json().get("ok", False)
    except requests.RequestException as error:
        logger.error("Error de conexión con Telegram: %s", error)
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
            if titulo and enlace:
                items.append({
                    "guid": guid,
                    "titulo": titulo,
                    "enlace": enlace,
                    "descripcion": descripcion,
                })
    return items

# =========================================================
# FILTRADO DE ALERTAS MEJORADO
# =========================================================

def detectar_descuento(texto):
    if not texto:
        return 0
    
    texto = html.unescape(texto).lower()

    # 1. Eliminar frases de marketing engañosas antes de buscar números
    patrones_falsos = [
        r'hasta\s*(el|un)?\s*\d{1,2}\s*%',
        r'hasta\s*\d{1,2}\s*%',
        r'en toda la tienda',
        r'en la web',
        r'en seleccionados'
    ]
    texto_limpio = texto
    for patron in patrones_falsos:
        texto_limpio = re.sub(patron, '', texto_limpio)

    # 2. Extraer porcentajes reales del texto limpio
    coincidencias = re.findall(r'-?(\d{1,2})\s*%', texto_limpio)
    nums = [int(n) for n in coincidencias if 1 <= int(n) <= 99]
    
    return max(nums, default=0)

def evaluar_oferta(item):
    titulo = item["titulo"]
    titulo_lower = titulo.lower()
    descripcion = item["descripcion"] or ""
    
    es_modelo_deseado = any(m in titulo_lower for m in MODELOS_ESPECIFICOS)

    # Buscar descuento PRIMERO en el título (donde está el dato real)
    descuento = detectar_descuento(titulo)

    # Si no hay descuento en el título, buscar en la descripción limpia
    if descuento == 0:
        descuento = detectar_descuento(descripcion)

    # Si NO es el POCO X8 Pro y tampoco alcanza el 80% real, se descarta
    if not es_modelo_deseado and descuento < UMBRAL_DESCUENTO_GENERAL:
        return None

    # Asignar etiqueta según el tipo de oferta
    if es_modelo_deseado and descuento >= 50:
        etiqueta = f"💥 <b>[CHOLLO MÁXIMO POCO X8 PRO -{descuento}%]</b>"
    elif es_modelo_deseado:
        etiqueta = f"📱 <b>[OFERTA POCO X8 PRO]</b>"
    else:
        etiqueta = f"🚨 <b>[SUPER MEGA CHOLLO GENERAL -{descuento}%]</b>"

    titulo_seguro = html.escape(titulo)
    enlace_seguro = html.escape(item["enlace"], quote=True)

    mensaje = (
        f"{etiqueta}\n\n"
        f"📌 <b>{titulo_seguro}</b>\n\n"
        f'🛒 <a href="{enlace_seguro}">VER ENLACE EN TIENDA</a>'
    )

    if "amazon" in item["enlace"].lower() or "amazon" in titulo_lower:
        busqueda = quote_plus(titulo[:40])
        mensaje += f' | 📊 <a href="https://es.camelcamelcamel.com/search?sq={busqueda}">HISTÓRICO</a>'

    return mensaje

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
    if oferta_ya_vista(guid):
        return

    mensaje = evaluar_oferta(item)
    if mensaje is None:
        return

    if enviar_alerta_telegram(mensaje):
        guardar_oferta(guid, item["titulo"])
        logger.info("Alerta enviada desde %s: %s", fuente, item["titulo"])

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
            procesar_oferta(item, fuente)
            total += 1

    logger.info("Ciclo finalizado | %s ofertas analizadas | %.2f s", total, time.monotonic() - inicio)

def bucle_escaneo():
    logger.info("Iniciando escáner de ofertas directo con nuevos filtros.")
    while True:
        inicio = time.monotonic()
        try:
            escanear_chollos()
        except Exception:
            logger.exception("Error en el bucle principal de escaneo.")
        duracion = time.monotonic() - inicio
        espera = max(0, INTERVALO_ESCANEO - duracion)
        time.sleep(espera)

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
