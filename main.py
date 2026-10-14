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
# CONFIGURACIÓN Y UMBRALES
# =========================================================

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "507823530")

INTERVALO_ESCANEO = 60
TIEMPO_ESPERA = 12
MAX_TRABAJADORES = 8
RUTA_BD = os.environ.get("DB_PATH", "ofertas.db")

UMBRAL_DESCUENTO_GENERAL = 50   # Mínimo 50% para tech general
UMBRAL_DESCUENTO_APPLE = 25     # Mínimo 25% para Apple
UMBRAL_DESCUENTO_ANDROID = 35   # Mínimo 35% para Xiaomi, POCO, Redmi

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

MARCAS_APPLE = ["iphone", "apple", "ipad", "macbook"]
MARCAS_ANDROID = ["xiaomi", "poco", "redmi"]

bloqueo_bd = threading.Lock()
PRIMER_ARRANQUE = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# =========================================================
# SERVIDOR WEB Y MONITOREO
# =========================================================

@app.route("/")
def home():
    return "Bot de Chollos Tech Activo 24/7", 200

@app.route("/health")
def health():
    return {"status": "ok", "feeds_configurados": len(FEEDS_RSS)}, 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

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
# EXTRACCIÓN Y LIMPIEZA XML
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
# DETECCIÓN DE DESCUENTOS Y CLASIFICACIÓN
# =========================================================

def detectar_descuento_real(texto):
    """
    Detecta exclusivamente descuentos de rebaja real (-50%, 50% DTO, dto. 50%)
    e ignora menciones de cashback, cupones pequeños o comisiones.
    """
    texto = html.unescape(texto or "").lower()
    
    # Ignorar contexto de cashback o cupones secundarios en la frase
    texto_limpio = re.sub(r'(cashback|monedero|cupon|cupón)\s*(\d{1,2}%|\d{1,2}\s*€)', '', texto)

    patrones = [
        r"-\s*(\d{1,2})\s*%",                   # -50%
        r"(\d{1,2})\s*%\s*(?:de\s+)?descuento", # 50% de descuento
        r"(\d{1,2})\s*%\s*dto",                 # 50% dto
        r"dto\.?\s*(\d{1,2})\s*%",               # dto. 50%
        r"ahorras?\s*(\d{1,2})\s*%"             # ahorra 50%
    ]

    porcentajes = []
    for p in patrones:
        coincidencias = re.findall(p, texto_limpio)
        for val in coincidencias:
            num = int(val)
            if 1 <= num <= 99:
                porcentajes.append(num)

    return max(porcentajes, default=0)

def obtener_enlace_historico(titulo, enlace):
    if "amazon" in enlace.lower() or "amazon" in titulo.lower():
        busqueda = quote_plus(titulo[:50])
        return f"https://es.camelcamelcamel.com/search?sq={busqueda}"
    return None

def evaluar_oferta(item):
    titulo = item["titulo"]
    titulo_lower = titulo.lower()
    texto_completo = f"{titulo} {item['descripcion']}"
    descuento = detectar_descuento_real(texto_completo)

    es_apple = any(m in titulo_lower for m in MARCAS_APPLE)
    es_android = any(m in titulo_lower for m in MARCAS_ANDROID)

    tipo_alerta = None

    # Regla 1: Apple con descuento o mención directa
    if es_apple:
        if descuento >= UMBRAL_DESCUENTO_APPLE or descuento == 0:
            tipo_alerta = "📱 <b>[MÓVIL / APPLE]</b>"
    # Regla 2: Android con descuento o mención directa
    elif es_android:
        if descuento >= UMBRAL_DESCUENTO_ANDROID or descuento == 0:
            tipo_alerta = "📱 <b>[MÓVIL / ANDROID]</b>"
    # Regla 3: Ofertas generales de tecnología (SOLO si tienen descuento explícito >= 50%)
    elif descuento >= UMBRAL_DESCUENTO_GENERAL:
        tipo_alerta = "🚨 <b>[CHOLLO TECH GENERAL]</b>"

    # CORRECCIÓN 1: Si no coincide con las reglas o es general sin % real, se descarta.
    if not tipo_alerta:
        return None

    titulo_seguro = html.escape(titulo)
    enlace_seguro = html.escape(item["enlace"], quote=True)
    desc_str = f" ({descuento}% desc.)" if descuento > 0 else ""

    mensaje = f"{tipo_alerta}{desc_str}\n\n📌 <b>{titulo_seguro}</b>\n\n🛒 <a href=\"{enlace_seguro}\">VER OFERTA</a>"

    url_historico = obtener_enlace_historico(titulo, item["enlace"])
    if url_historico:
        mensaje += f' | 📊 <a href="{url_historico}">HISTÓRICO PRECIOS</a>'

    return mensaje

# =========================================================
# ESCANEO CONCURRENTE
# =========================================================

def descargar_feed(url):
    inicio = time.monotonic()
    try:
        respuesta = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ChollosBot/3.0)"},
            timeout=TIEMPO_ESPERA,
        )
        respuesta.raise_for_status()
        items = extraer_items(respuesta.content)
        logger.info("Feed OK [%s] | %s ofertas | %.2f s", url, len(items), time.monotonic() - inicio)
        return url, items
    except Exception as error:
        logger.warning("Fallo en feed [%s]: %s", url, error)
        return url, []

def procesar_oferta(item, fuente, silencioso=False):
    guid = item["guid"]
    if oferta_ya_vista(guid):
        return

    # CORRECCIÓN 3: Si es el primer arranque, guardamos sin notificar en Telegram
    if silencioso:
        guardar_oferta(guid, item["titulo"])
        return

    mensaje = evaluar_oferta(item)
    if mensaje is None:
        return

    if enviar_alerta_telegram(mensaje):
        guardar_oferta(guid, item["titulo"])
        logger.info("Alerta enviada desde %s: %s", fuente, item["titulo"])

def escanear_chollos():
    global PRIMER_ARRANQUE
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
    silencioso = PRIMER_ARRANQUE

    for fuente, items in resultados:
        for item in items:
            procesar_oferta(item, fuente, silencioso=silencioso)
            total += 1

    if PRIMER_ARRANQUE:
        logger.info("Primer arranque completado. Pre-cargadas %s ofertas sin notificar para evitar spam.", total)
        PRIMER_ARRANQUE = False
    else:
        logger.info("Ciclo finalizado | %s ofertas analizadas | %.2f s", total, time.monotonic() - inicio)

# =========================================================
# BUCLE PRINCIPAL
# =========================================================

def bucle_escaneo():
    logger.info("Iniciando escáner de ofertas optimizado.")
    while True:
        inicio = time.monotonic()
        try:
            escanear_chollos()
        except Exception:
            logger.exception("Error en el bucle principal de escaneo.")
        duracion = time.monotonic() - inicio
        espera = max(0, INTERVALO_ESCANEO - duracion)
        time.sleep(espera)

if __name__ == "__main__":
    inicializar_bd()
    threading.Thread(target=bucle_escaneo, daemon=True).start()
    run_web_server()
