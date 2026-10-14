import time
import os
import re
import threading
import requests
import xml.etree.ElementTree as ET
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot de Chollos Tech Activo 24/7"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= CONFIGURACIÓN =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = "507823530"

MARCAS_MOVILES = ["xiaomi", "poco", "iphone", "apple", "redmi"]
TIENDAS_TECH = ["amazon", "pccomponentes", "pc componentes", "aliexpress", "mediamarkt", "mi.com"]

# Lista ampliada de fuentes RSS directas (Chollometro, Portales Tech y Agregadores de Amazon/Tech)
FEEDS_RSS = [
    # Chollometro (Nuevos, Tendencias, Populares y Sección Electrónica)
    "https://www.chollometro.com/rss/nuevos",
    "https://www.chollometro.com/rss/tendencias",
    "https://www.chollometro.com/rss/populares",
    "https://www.chollometro.com/rss/categorias/electronica",
    
    # Portales especializados en ofertas de Amazon, PcComponentes y AliExpress en España
    "https://www.blogdechollos.com/feed/",
    "https://www.ofertitas.es/feed/",
    "https://www.chollodeals.es/feed/",
    "https://www.compradiccion.com/index.xml"
]

OFERTAS_VISTAS = set()
# ==================================================

def enviar_alerta_telegram(mensaje):
    if not TELEGRAM_TOKEN:
        print("Error: No se ha configurado la variable TELEGRAM_TOKEN en Render.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Error en Telegram: {e}")

def es_descuento_mayor_o_igual_50(texto):
    coincidencias = re.findall(r'-?(\d+)\s*%', texto)
    for porcentaje_str in coincidencias:
        porcentaje = int(porcentaje_str)
        if porcentaje >= 50:
            return True, porcentaje
    return False, 0

def escanear_chollos():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
    }
    
    for url_rss in FEEDS_RSS:
        try:
            response = requests.get(url_rss, headers=headers, timeout=10)
            if response.status_code == 200:
                root = ET.fromstring(response.content)
                
                # Soporte para formatos RSS (item) y Atom (entry)
                items = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
                
                for item in items:
                    guid_elem = item.find('guid') or item.find('{http://www.w3.org/2005/Atom}id') or item.find('link')
                    guid = guid_elem.text if guid_elem is not None else ""
                    
                    if not guid or guid in OFERTAS_VISTAS:
                        continue
                    
                    title_elem = item.find('title') or item.find('{http://www.w3.org/2005/Atom}title')
                    titulo = title_elem.text if title_elem is not None else ""
                    titulo_lower = titulo.lower()
                    
                    link_elem = item.find('link') or item.find('{http://www.w3.org/2005/Atom}link')
                    if link_elem is not None:
                        enlace = link_elem.attrib.get('href') if 'href' in link_elem.attrib else link_elem.text
                    else:
                        enlace = ""
                    
                    es_marca = any(m in titulo_lower for m in MARCAS_MOVILES)
                    es_tienda_tech = any(t in titulo_lower for t in TIENDAS_TECH)
                    tiene_gran_descuento, porcentaje = es_descuento_mayor_o_igual_50(titulo_lower)
                    
                    if es_marca or (es_tienda_tech and tiene_gran_descuento) or tiene_gran_descuento:
                        OFERTAS_VISTAS.add(guid)
                        
                        if es_marca and tiene_gran_descuento:
                            etiqueta = f"🔥 **CHOLLO {porcentaje}% EN MÓVILES**"
                        elif es_marca:
                            etiqueta = "📱 **OFERTA EN MÓVILES**"
                        else:
                            etiqueta = f"🚨 **CHOLLO TECH ({porcentaje}%)**"
                        
                        mensaje = (
                            f"{etiqueta}\n\n"
                            f"📌 **{titulo}**\n\n"
                            f"🛒 [VER ENLACE]({enlace})"
                        )
                        enviar_alerta_telegram(mensaje)
                        print(f"Alerta enviada: {titulo}")
                        
        except Exception as e:
            print(f"Error escaneando {url_rss}: {e}")

def bucle_escaneo():
    print("Iniciando escáner multicanal 24/7...")
    while True:
        escanear_chollos()
        time.sleep(15)

if __name__ == "__main__":
    threading.Thread(target=bucle_escaneo, daemon=True).start()
    run_web_server()
