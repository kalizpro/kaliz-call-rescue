import serial
import time
import re
import os
import requests
import csv
import wave
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
import audioop
from datetime import datetime
from dotenv import load_dotenv

# -----------------------------
# Configuración
# -----------------------------
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
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
AUTO_VSM = os.getenv("AUTO_VSM", "1") in ("1", "true", "TRUE", "yes", "YES")
TX_GAIN = os.getenv("TX_GAIN")
PCM8_SIGNED = os.getenv("PCM8_SIGNED", "0") in ("1", "true", "TRUE", "yes", "YES")
NORMALIZE_RMS = os.getenv("NORMALIZE_RMS", "1") in ("1", "true", "TRUE", "yes", "YES")
TARGET_RMS = int(os.getenv("TARGET_RMS", "5000"))  # objetivo RMS en PCM 16-bit
REMOVE_DC = os.getenv("REMOVE_DC", "1") in ("1", "true", "TRUE", "yes", "YES")
PRE_SILENCE_MS = int(os.getenv("PRE_SILENCE_MS", "100"))
PLAY_ONLY = os.getenv("PLAY_ONLY", "0") in ("1", "true", "TRUE", "yes", "YES")

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

def escape_dle(payload: bytes) -> bytes:
    """Duplica los bytes DLE (0x10) según protocolo V.253 para VTX."""
    if not payload:
        return payload
    return payload.replace(b'\x10', b'\x10\x10')

def get_silence_byte(codec: int) -> int:
    """Devuelve el byte de silencio para el códec dado.
    μ-law: 0xFF, A-law: 0xD5, PCM8 unsigned: 0x80 (o 0x00 si signed).
    """
    if codec == 130:  # μ-law
        return 0xFF
    if codec == 129:  # A-law
        return 0xD5
    # 8-bit PCM
    return 0x80 if not PCM8_SIGNED else 0x00

def process_pcm16_block(data: bytes) -> bytes:
    """Post-procesa un bloque PCM 16-bit mono: elimina DC y normaliza RMS si está habilitado.
    Devuelve bytes PCM16 procesados.
    """
    try:
        if not data:
            return data
        processed = data
        # Eliminar componente DC
        if REMOVE_DC:
            avg = audioop.avg(processed, 2)
            if avg:
                processed = audioop.bias(processed, 2, -avg)
        # Normalización RMS simple hacia TARGET_RMS
        if NORMALIZE_RMS and TARGET_RMS > 0:
            rms = audioop.rms(processed, 2)
            if rms > 0:
                # Limitar factor para evitar clipping duro
                factor = min(3.0, max(0.3, TARGET_RMS / float(rms)))
                processed = audioop.mul(processed, 2, factor)
                # Soft-clip: limitar picos a evitar saturación (aprox)
                max_amp = 32760
                # audioop no tiene clip directo; convertimos a 16-bit y recortamos
                # Pero mantenerlo simple: confiar en factor limitado
        return processed
    except Exception:
        return data

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
            # Algunos módems solo responden OK aquí, pero la línea queda en voz
            # Continuamos de todas formas a configurar VSM/VTX
            start = time.time()
            while time.time() - start < 2:
                line = ser.readline().decode(errors="ignore").strip()
                if line:
                    print(f"DEBUG(VLS): {line}")

        # Cambiar a modo voz y formato
        print("🎙️ Cambiando a modo voz para reproducir audio...")
        # Nos aseguramos de clase 8
        ser.write(b'AT+FCLASS=8\r\n')
        time.sleep(0.3)
        # Activar control de flujo por hardware (si el módem lo soporta)
        try:
            ser.write(b'AT+IFC=2,2\r\n')
            time.sleep(0.2)
        except Exception:
            pass

        # Autodetección de códec y tasa si está habilitada
        effective_codec = VSM_CODEC
        effective_rate = SAMPLE_RATE
        if AUTO_VSM:
            try:
                ser.write(b'AT+VSM=?\r\n')
                time.sleep(0.25)
                supported_line = ""
                start = time.time()
                while time.time() - start < 1.5:
                    line = ser.readline().decode(errors="ignore").strip()
                    if line:
                        if "+VSM" in line or line.startswith("("):
                            supported_line = line
                            break
                codecs = []
                rates = []
                groups = re.findall(r"\(([^)]*)\)", supported_line)
                if groups:
                    try:
                        codecs = [int(x) for x in re.split(r"[, ]+", groups[0].strip()) if x]
                    except Exception:
                        codecs = []
                    if len(groups) > 1:
                        try:
                            rates = [int(x) for x in re.split(r"[, ]+", groups[1].strip()) if x]
                        except Exception:
                            rates = []
                for preferred in (130, 129, 128):
                    if preferred in codecs:
                        effective_codec = preferred
                        break
                if 8000 in rates:
                    effective_rate = 8000
                elif rates:
                    effective_rate = rates[0]
                print(f"ℹ️ VSM autodetectado: codec={effective_codec}, rate={effective_rate}")
            except Exception:
                print("⚠️ No se pudo autodetectar VSM; usando configuración por defecto")
        else:
            # Validación de códec/tasa contra capacidades reales del módem
            try:
                ser.write(b'AT+VSM=?\r\n')
                time.sleep(0.25)
                supported_line = ""
                start = time.time()
                while time.time() - start < 1.5:
                    line = ser.readline().decode(errors="ignore").strip()
                    if line:
                        if "+VSM" in line or line.startswith("("):
                            supported_line = line
                            break
                codecs = []
                rates = []
                groups = re.findall(r"\(([^)]*)\)", supported_line)
                if groups:
                    try:
                        codecs = [int(x) for x in re.split(r"[, ]+", groups[0].strip()) if x]
                    except Exception:
                        codecs = []
                    if len(groups) > 1:
                        try:
                            rates = [int(x) for x in re.split(r"[, ]+", groups[1].strip()) if x]
                        except Exception:
                            rates = []
                # Si el códec forzado no está soportado, elegir el mejor disponible
                if codecs and effective_codec not in codecs:
                    for preferred in (130, 129, 128):
                        if preferred in codecs:
                            print(f"⚠️ VSM_CODEC forzado {VSM_CODEC} no soportado. Usando {preferred}.")
                            effective_codec = preferred
                            break
                    else:
                        print("⚠️ Lista de códecs vacía o no interpretable; se mantiene configuración actual")
                # Elegir una tasa soportada (preferir 8000 Hz)
                if rates:
                    if 8000 in rates:
                        effective_rate = 8000
                    else:
                        effective_rate = rates[0]
                print(f"ℹ️ VSM validado: codec={effective_codec}, rate={effective_rate}")
            except Exception:
                print("⚠️ No se pudo validar VSM contra el módem; usando configuración por defecto")

        ser.write(f"AT+VSM={effective_codec},{effective_rate}\r\n".encode())
        time.sleep(0.4)

        # Ganancia de transmisión si está configurada
        if TX_GAIN is not None and TX_GAIN != "":
            try:
                ser.write(f"AT+VGT={TX_GAIN}\r\n".encode())
                time.sleep(0.2)
            except Exception:
                pass

        # Intento de desactivar AGC/ruido si existe
        try:
            ser.write(b'AT+VRA=0\r\n')  # AGC off (si soporta)
            time.sleep(0.1)
            ser.write(b'AT+VRN=0\r\n')  # Noise reduction off (si soporta)
            time.sleep(0.1)
        except Exception:
            pass

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

        # Enviar 100 ms de silencio inicial para estabilizar
        silence_ms = max(PRE_SILENCE_MS, 0)
        silence_samples = int(effective_rate * (silence_ms / 1000.0))
        silence_byte = get_silence_byte(effective_codec)
        if silence_samples > 0:
            pre_silence = bytes([silence_byte]) * silence_samples
            ser.write(escape_dle(pre_silence))
            time.sleep(silence_ms / 1000.0)

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

                # Framing de 20 ms
                samples_per_frame = max(int(effective_rate * 0.02), 1)
                frame_bytes = samples_per_frame  # μ-law/A-law/PCM8 = 1 byte por muestra
                out_buffer = b''

                # Temporización por reloj monotónico
                next_deadline = time.monotonic()
                while True:
                    frames = w.readframes(1024)
                    if not frames:
                        break

                    data = frames

                    # Asegurar formato lineal 16-bit para las conversiones siguientes
                    if sampwidth != 2:
                        try:
                            src_width = sampwidth
                            src_data = data
                            # WAV PCM 8-bit suele ser unsigned. Si no marcamos PCM8_SIGNED, convertimos a signed.
                            if sampwidth == 1 and not PCM8_SIGNED:
                                src_data = audioop.bias(src_data, 1, -128)
                                src_width = 1
                            data = audioop.lin2lin(src_data, src_width, 2)
                        except Exception:
                            # Si no se puede convertir, saltar bloque
                            continue

                    # Convertir a mono si hace falta
                    if channels and channels != 1:
                        data = audioop.tomono(data, 2, 0.5, 0.5)

                    # Resamplear a la tasa efectiva si es necesario
                    if framerate and framerate != effective_rate:
                        data, rate_state = audioop.ratecv(data, 2, 1, framerate, effective_rate, rate_state)

                    # Post-procesado PCM16: DC y RMS
                    data = process_pcm16_block(data)

                    # Convertir al códec elegido
                    if effective_codec == 130:  # μ-law
                        out = audioop.lin2ulaw(data, 2)
                    elif effective_codec == 129:  # A-law
                        out = audioop.lin2alaw(data, 2)
                    elif effective_codec == 128:  # 8-bit PCM
                        pcm8_signed = audioop.lin2lin(data, 2, 1)
                        if PCM8_SIGNED:
                            out = pcm8_signed
                        else:
                            # Convertir de signed [-128,127] a unsigned [0,255]
                            out = bytes((b + 128) % 256 for b in pcm8_signed)
                    else:
                        # Por defecto μ-law
                        out = audioop.lin2ulaw(data, 2)

                    # Acumular y enviar en frames de 20 ms con escape DLE
                    out_buffer += out
                    while len(out_buffer) >= frame_bytes:
                        chunk = out_buffer[:frame_bytes]
                        out_buffer = out_buffer[frame_bytes:]
                        ser.write(escape_dle(chunk))
                        next_deadline += 0.02
                        now = time.monotonic()
                        delay = next_deadline - now
                        if delay > 0:
                            time.sleep(delay)

                w.close()
                # Enviar remanente
                if out_buffer:
                    ser.write(escape_dle(out_buffer))
                    time.sleep(0.01)
            except Exception as e:
                print(f"❌ Error leyendo/convirtiendo WAV: {e}. Intentando como RAW...")
                with open(audio_file, 'rb') as f:
                    samples_per_frame = max(int(effective_rate * 0.02), 1)
                    frame_bytes = samples_per_frame
                    out_buffer = b''
                    next_deadline = time.monotonic()
                    while True:
                        chunk = f.read(1024)
                        if not chunk:
                            break
                        out_buffer += chunk
                        while len(out_buffer) >= frame_bytes:
                            frame = out_buffer[:frame_bytes]
                            out_buffer = out_buffer[frame_bytes:]
                            ser.write(escape_dle(frame))
                            next_deadline += 0.02
                            now = time.monotonic()
                            delay = next_deadline - now
                            if delay > 0:
                                time.sleep(delay)
                    if out_buffer:
                        ser.write(escape_dle(out_buffer))
                        time.sleep(0.01)
        else:
            with open(audio_file, 'rb') as f:
                samples_per_frame = max(int(SAMPLE_RATE * 0.02), 1)
                frame_bytes = samples_per_frame
                out_buffer = b''
                next_deadline = time.monotonic()
                while True:
                    chunk = f.read(1024)
                    if not chunk:
                        break
                    out_buffer += chunk
                    while len(out_buffer) >= frame_bytes:
                        frame = out_buffer[:frame_bytes]
                        out_buffer = out_buffer[frame_bytes:]
                        ser.write(escape_dle(frame))
                        next_deadline += 0.02
                        now = time.monotonic()
                        delay = next_deadline - now
                        if delay > 0:
                            time.sleep(delay)
                if out_buffer:
                    ser.write(escape_dle(out_buffer))
                    time.sleep(0.01)

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
    ser = serial.Serial(PORT, BAUD, timeout=1, rtscts=True, xonxoff=False)
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

# Modo prueba: solo reproducir audio y salir
if PLAY_ONLY:
    print("🎧 Modo solo reproducción activado (PLAY_ONLY=1). Reproduciendo y saliendo...")
    try:
        play_audio(ser, AUDIO_FILE)
    finally:
        try:
            ser.close()
        except Exception:
            pass
    exit(0)

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