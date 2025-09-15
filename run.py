import serial
import time
import re
import os
import requests
import csv
import wave
import audioop
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
HANGUP_DELAY_MS = int(os.getenv("HANGUP_DELAY_MS", "1200"))
PLAY_AUDIO = os.getenv("PLAY_AUDIO", "1") in ("1", "true", "TRUE", "yes", "YES")
VSM_CODEC = int(os.getenv("VSM_CODEC", "130"))  # 130: μ-law, 129: A-law, 128: 8-bit PCM
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "8000"))

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
    """Contesta la llamada y reproduce un archivo de audio en la línea telefónica.

    - Si la extensión es .wav, lee frames con el módulo wave y envía PCM.
    - Caso contrario, envía el archivo como RAW (u-Law/PCM según VSM).
    Requiere que el módem soporte AT+VTX.
    """
    try:
        # Preparar y contestar en modo voz
        print("🎙️ Preparando modo voz para contestar...")
        ser.write(b'ATM0\r\n')  # silenciar speaker local
        time.sleep(0.2)
        ser.readline()
        ser.write(b'AT+FCLASS=8\r\n')
        time.sleep(0.4)

        # Intentar ATA en clase 8
        print("📞 Contestando (ATA) en modo voz...")
        ser.write(b'ATA\r\n')
        response = ""
        timeout = time.time() + 8
        while ("CONNECT" not in response and "VCON" not in response) and time.time() < timeout:
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                response = line
                print(f"DEBUG(ATA): {line}")

        # Si no conecta, intentar seleccionar línea de voz
        if ("CONNECT" not in response and "VCON" not in response):
            print("⚠️ ATA no conectó, intentando AT+VLS=1...")
            ser.write(b'AT+VLS=1\r\n')
            time.sleep(0.8)
            response = ""
            timeout = time.time() + 5
            while ("CONNECT" not in response and "VCON" not in response) and time.time() < timeout:
                line = ser.readline().decode(errors="ignore").strip()
                if line:
                    response = line
                    print(f"DEBUG(VLS): {line}")
            if ("CONNECT" not in response and "VCON" not in response):
                print("❌ No se pudo levantar la llamada en modo voz. Colgando.")
                ser.write(b'ATH\r\n')
                return

        # Cambiar a modo voz y formato
        print("🎙️ Cambiando a modo voz para reproducir audio...")
        # Nos aseguramos de clase 8 y configuramos códec y muestreo
        ser.write(b'AT+FCLASS=8\r\n')
        time.sleep(0.3)
        ser.write(f"AT+VSM={VSM_CODEC},{SAMPLE_RATE}\r\n".encode())
        time.sleep(0.5)

        # Entrar en transmisión de voz
        print("➡️ Entrando en modo VTX...")
        ser.write(b'AT+VTX\r\n')
        # Algunos módems responden CONNECT o VCON al entrar a VTX
        start = time.time()
        while time.time() - start < 2:
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                print(f"DEBUG(VTX): {line}")
                if ("CONNECT" in line) or ("VCON" in line) or ("OK" in line):
                    break

        # Reproducir audio
        print("▶️ Reproduciendo audio...")
        if audio_file.lower().endswith('.wav'):
            try:
                w = wave.open(audio_file, 'rb')
                channels = getattr(w, 'getnchannels', lambda:1)()
                framerate = getattr(w, 'getframerate', lambda:8000)()
                sampwidth = getattr(w, 'getsampwidth', lambda:2)()

                print(f"ℹ️ WAV: canales={channels}, hz={framerate}, sampwidth={sampwidth}")
                if channels != 1 or framerate != SAMPLE_RATE or sampwidth not in (1, 2, 4):
                    print("⚠️ Ajustando audio a mono, 8kHz y formato esperado del módem...")

                # Conversión incremental por bloques
                # Mantener estado para rate conversion
                rate_state = None
                bytes_per_sample_out = 1  # μ-law/A-law/8-bit PCM => 1 byte por muestra

                while True:
                    frames = w.readframes(1024)
                    if not frames:
                        break

                    data = frames

                    # Asegurar formato lineal 16-bit para las conversiones siguientes
                    if sampwidth != 2:
                        try:
                            data = audioop.lin2lin(data, sampwidth, 2)
                        except Exception:
                            # Si no se puede convertir, saltar bloque
                            continue

                    # Convertir a mono si hace falta
                    if channels and channels != 1:
                        data = audioop.tomono(data, 2, 0.5, 0.5)

                    # Resamplear a SAMPLE_RATE si es necesario
                    if framerate and framerate != SAMPLE_RATE:
                        data, rate_state = audioop.ratecv(data, 2, 1, framerate, SAMPLE_RATE, rate_state)

                    # Convertir al códec elegido
                    if VSM_CODEC == 130:  # μ-law
                        out = audioop.lin2ulaw(data, 2)
                    elif VSM_CODEC == 129:  # A-law
                        out = audioop.lin2alaw(data, 2)
                    elif VSM_CODEC == 128:  # 8-bit PCM (firmas varían; asumimos signed -> unsigned bias)
                        pcm8_signed = audioop.lin2lin(data, 2, 1)
                        # Convertir de signed [-128,127] a unsigned [0,255]
                        out = bytes((b + 128) % 256 for b in pcm8_signed)
                    else:
                        # Por defecto μ-law
                        out = audioop.lin2ulaw(data, 2)

                    ser.write(out)
                    # Pacing basado en duración del bloque
                    block_seconds = len(out) / float(SAMPLE_RATE)
                    time.sleep(max(block_seconds * 0.95, 0.001))

                w.close()
            except Exception as e:
                print(f"❌ Error leyendo/convirtiendo WAV: {e}. Intentando como RAW...")
                with open(audio_file, 'rb') as f:
                    while True:
                        chunk = f.read(1024)
                        if not chunk:
                            break
                        ser.write(chunk)
                        # Pacing aproximado si asumimos 8k/μ-law
                        time.sleep(max((len(chunk) / float(SAMPLE_RATE)) * 0.95, 0.001))
        else:
            with open(audio_file, 'rb') as f:
                while True:
                    chunk = f.read(1024)
                    if not chunk:
                        break
                    ser.write(chunk)
                    time.sleep(0.02)

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
        
def answer_and_hangup(ser: serial.Serial):
    """Toma la línea en modo voz de forma silenciosa y cuelga con un pequeño delay.

    Estrategia para evitar 'beep' audible:
    - Silenciar parlante del módem (ATM0) para evitar tonos locales.
    - Usar clase de voz (FCLASS=8) y tomar la línea (VLS=1).
    - Esperar unos milisegundos para estabilizar y luego colgar (ATH).
    """
    try:
        print("🎙️ Preparando para contestar y colgar (silencioso)...")
        # Silenciar el speaker del módem (local)
        ser.write(b'ATM0\r\n')
        time.sleep(0.2)
        ser.readline()

        # Modo voz y tomar línea
        ser.write(b'AT+FCLASS=8\r\n')
        time.sleep(0.3)
        ser.write(b'AT+VLS=1\r\n')
        time.sleep(0.5)

        # Pequeño delay configurable antes de colgar
        time.sleep(max(HANGUP_DELAY_MS, 0) / 1000.0)

        # Colgar
        print("📞 Colgando...")
        ser.write(b'ATH\r\n')
        time.sleep(0.3)
        print("✅ Llamada colgada.")
    except Exception as e:
        print(f"❌ Error en answer_and_hangup: {e}")

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
                    if PLAY_AUDIO:
                        print("📢 Alcanzado MAX_RINGS. Enviando webhook y reproduciendo audio...")
                        log_call(incoming_number, LOCAL_NUMBER, "answered_with_audio")
                        call_rescue_web_hook(incoming_number, LOCAL_NUMBER, "answered_with_audio")
                        play_audio(ser, AUDIO_FILE)
                    else:
                        print("📢 Alcanzado MAX_RINGS. Enviando webhook y colgando...")
                        log_call(incoming_number, LOCAL_NUMBER, "hangup_after_webhook")
                        call_rescue_web_hook(incoming_number, LOCAL_NUMBER, "hangup_after_webhook")
                        answer_and_hangup(ser)
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