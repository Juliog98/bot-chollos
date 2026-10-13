import time
import os
import threading
import requests
import xml.etree.ElementTree as ET
from flask import Flask

# Servidor web mini para mantener activo Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot de Chollos Activo 24/7"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= CONFIGURACIÓN =================
# Obtiene el token de forma 100% privada desde las Variables de Entorno de Render
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = "507823530"

# Filtro exclusivo de marcas de interés
MARCAS_MOVILES = ["xiaomi", "poco", "iphone", "apple", "redmi"]
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

def escanear_chollos():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
    }
    url_rss = "https://www.chollometro.com/rss/nuevos"
    
    try:
        response = requests.get(url_rss, headers=headers, timeout=10)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            for item in root.findall('.//item'):
                guid = item.find('guid').text if item.find('guid') is not None else item.find('link').text
                
                if guid in OFERTAS_VISTAS:
                    continue
                
                titulo = item.find('title').text if item.find('title') is not None else ""
                titulo_lower = titulo.lower()
                enlace = item.find('link').text if item.find('link') is not None else ""
                
                # Solo notifica si coincide con las marcas seleccionadas
                es_movil = any(m in titulo_lower for m in MARCAS_MOVILES)
                
                if es_movil:
                    OFERTAS_VISTAS.add(guid)
                    mensaje = (
                        f"📱 **OFERTA DETECTADA**\n\n"
                        f"📌 **{titulo}**\n\n"
                        f"🛒 [VER ENLACE]({enlace})"
                    )
                    enviar_alerta_telegram(mensaje)
                    print(f"Alerta enviada: {titulo}")
                    
    except Exception as e:
        print(f"Error al escanear: {e}")

def bucle_escaneo():
    print("Iniciando escáner continuo...")
    while True:
        escanear_chollos()
        time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=bucle_escaneo, daemon=True).start()
    run_web_server()
