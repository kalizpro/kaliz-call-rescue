import serial
import time
import re
import os
import requests
import csv
import wave
from datetime import datetime
from dotenv import load_dotenv

# -----------------------------
# Configuración
# -----------------------------
load_dotenv()
LOCAL_NUMBER = os.getenv("NUMBER")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = os.getenv("PORT", "/dev/ttyACM0")
BAUD = int(os.getenv("BAUD", "115200"))
MAX_RINGS = int(os.getenv("MAX_RINGS", "3"))
LOG_FILE = os.getenv("LOG_FILE", "calls_log.csv")
AUDIO_FILE = os.getenv("AUDIO_FILE", "voices/busy_lines.wav")
COUNTRY_CODE = os.getenv("COUNTRY_CODE", "598")
TRUNK_PREFIX = os.getenv("TRUNK_PREFIX", "0")

# -----------------------------
# Funciones
# -----------------------------
def log_call(number: str, local_number: str, event: str):
    """Guardar en CSV"""
    timestamp = datetime.now().isoformat()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, local_number, number, event])
    print(f"📝 Log: {timestamp} {event} {number} -> {local_number}")

def call_rescue_web_hook(number: str, local_number: str, event: str):
    """Enviar webhook"""
    if not WEBHOOK_URL:
        return
    payload = {"From": number, "To": local_number, "CallSid": event}
    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=5)
        print(f"Webhook {event}: {payload} -> {response.status_code}")
    except Exception as e:
        print(f"❌ Error enviando webhook: {e}")

def normalize_phone_number(raw_number: str) -> str:
    """Normaliza a E.164 usando COUNTRY_CODE y TRUNK_PREFIX.
    - Si ya viene con '+', se respeta.
    - Se remueve el prefijo troncal si corresponde.
    - Se antepone +COUNTRY_CODE cuando falta.
    """
    if not raw_number:
        return raw_number
    value = re.sub(r"[^0-9+]", "", raw_number.strip())
    if value.startswith("+"):
        return value
    # Quitar prefijo troncal (ej. '0')
    if TRUNK_PREFIX and value.startswith(TRUNK_PREFIX):
        value = value[len(TRUNK_PREFIX):]
    # Agregar código de país si falta
    if COUNTRY_CODE and not value.startswith(COUNTRY_CODE):
        return f"+{COUNTRY_CODE}{value}"
    # Si ya empieza con el código de país pero sin '+', agrégalo
    return f"+{value}"

def play_audio(ser: serial.Serial, audio_file: str):
    """Contesta la llamada y reproduce un archivo RAW en la línea telefónica"""
    try:
        # Cambiar a modo data para contestar
        print("🎙️ Modo data (FCLASS=0) para contestar...")
        ser.write(b'AT+FCLASS=0\r\n')
        time.sleep(0.5)
        ser.readline()  # descartar respuesta

        # Intentar ATA
        print("📞 Contestando con ATA...")
        ser.write(b'ATA\r\n')
        response = ""
        timeout = time.time() + 5
        while "CONNECT" not in response and time.time() < timeout:
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                response = line
                print(f"DEBUG(ATA): {line}")

        # Si no conecta, intentar AT+VLS=1
        if "CONNECT" not in response:
            print("⚠️ ATA no conectó, intentando AT+VLS=1...")
            ser.write(b'AT+VLS=1\r\n')
            time.sleep(1)
            response = ""
            timeout = time.time() + 5
            while "CONNECT" not in response and time.time() < timeout:
                line = ser.readline().decode(errors="ignore").strip()
                if line:
                    response = line
                    print(f"DEBUG(VLS): {line}")
            if "CONNECT" not in response:
                print("❌ No se pudo levantar la llamada. Colgando.")
                ser.write(b'ATH\r\n')
                return

        # Cambiar a modo voz
        print("🎙️ Cambiando a modo voz para reproducir audio...")
        ser.write(b'AT+FCLASS=8\r\n')
        time.sleep(0.5)
        ser.write(b'AT+VSM=128,8000\r\n')
        time.sleep(0.5)

        # Entrar en transmisión de voz
        print("➡️ Entrando en modo VTX...")
        ser.write(b'AT+VTX\r\n')
        time.sleep(1)

        # Reproducir audio RAW
        print("▶️ Reproduciendo audio...")
        with open(audio_file, "rb") as f:
            while chunk := f.read(1024):
                ser.write(chunk)
                time.sleep(0.05)

        # Terminar transmisión
        ser.write(b'\x10')  # DLE
        ser.write(b'\x03')  # ETX
        time.sleep(0.5)

        # Colgar
        print("📞 Colgando...")
        ser.write(b'ATH\r\n')
        print("✅ Audio reproducido y llamada terminada.")

    except Exception as e:
        print(f"❌ Error al reproducir audio: {e}")
        
# -----------------------------
# Inicialización del módem
# -----------------------------
try:
    ser = serial.Serial(PORT, BAUD, timeout=1)
except Exception as e:
    print(f"No se pudo abrir el puerto {PORT}: {e}")
    exit(1)

ser.write(b'AT&F\r')        # Cargar configuración de fábrica
time.sleep(0.5)
ser.write(b'ATE0\r')        # desactivar eco
time.sleep(0.2)
ser.write(b'AT+FCLASS=8\r') # modo voz (inicialmente, para detectar el Caller ID)
time.sleep(0.2)
ser.write(b'ATS0=0\r')      # no contestar automáticamente
time.sleep(0.2)
ser.write(b'AT+VCID=1\r')   # habilitar Caller ID
time.sleep(0.2)
ser.write(b'ATX4\r')        # habilitar códigos extendidos
time.sleep(0.2)
ser.write(b'ATV1\r')        # habilitar códigos de palabra completa
time.sleep(0.2)

print(f"📡 Línea configurada en {LOCAL_NUMBER}. Esperando llamadas...")

# -----------------------------
# Variables de estado
# -----------------------------
incoming_number = None
ring_count = 0
call_active = False

# -----------------------------
# Loop principal
# -----------------------------
try:
    while True:
        raw = ser.readline()
        if not raw:
            continue

        line = raw.decode(errors="ignore").strip()
        line = re.sub(r'[^\x20-\x7E]', '', line)

        if not line or line == "OK":
            continue

        # Detecta número entrante (NMBR)
        if line.startswith("NMBR"):
            incoming_number = line.split("=")[-1].strip()
            incoming_number = normalize_phone_number(incoming_number)
            call_active = True
            ring_count = 0
            print(f"📲 Número entrante detectado: {incoming_number}")

        # Detecta timbre
        elif "RING" in line or line == "R":
            if call_active:
                ring_count += 1
                print(f"📞 Ring {ring_count} de {incoming_number}")
                if ring_count >= MAX_RINGS:
                    print("📢 Contestando y reproduciendo audio...")
                    log_call(incoming_number, LOCAL_NUMBER, "answered_with_audio")
                    call_rescue_web_hook(incoming_number, LOCAL_NUMBER, "answered_with_audio")
                    # play_audio(ser, AUDIO_FILE)
                    incoming_number = None
                    call_active = False
                    ring_count = 0

        # Detecta línea ocupada o corte inmediato
        elif "BUSY" in line or "NO CARRIER" in line:
            if call_active and incoming_number:
                print("📵 Línea ocupada o llamada terminada rápido")
                log_call(incoming_number, LOCAL_NUMBER, "busy")
                call_rescue_web_hook(incoming_number, LOCAL_NUMBER, "busy")
                incoming_number = None
                call_active = False
                ring_count = 0

except KeyboardInterrupt:
    ser.close()
    print("Script detenido.")