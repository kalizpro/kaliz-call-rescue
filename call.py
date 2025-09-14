import serial
import wave
import time

def main():
    # Inicializar el puerto serial
    phone = serial.Serial('/dev/ttyACM0', 112500, timeout=5)
    
    # Configurar módem para transmisión de voz
    phone.write('AT\r\n'.encode())
    phone.write('AT+FCLASS=8\r\n'.encode())   # Modo voz
    phone.write('AT+VSM=0,8000\r\n'.encode()) # Formato PCM 8kHz
    
    # Abrir archivo de audio
    music = wave.open('voices/busy_lines.wav', 'rb')

    # Marcar el número
    number = '099837840'
    command = f'ATDT{number}\r\n'
    phone.write(command.encode())
    time.sleep(10)  # Esperar a que se conecte

    # Iniciar transmisión de voz
    phone.write('AT+VTX\r\n'.encode())
    
    cont = True
    while cont:
        frame = music.readframes(1024)
        if not frame:  # readframes devuelve bytes vacíos al final
            cont = False
        else:
            phone.write(frame)  # enviar datos al módem

if __name__ == '__main__':
    main()
