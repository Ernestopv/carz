# Robot Camera Control, (Raspberry PI Zero W)

Proyecto Flask para Raspberry Pi con:

- Stream MJPEG de cámara CSI usando Picamera2.
- Control de dos motores mediante gpiozero.
- Botones Forward / Backward / Left / Right / Stop.
- Control de potencia de 0 a 100%.
- Control con teclado W/A/S/D o flechas.
- Watchdog de seguridad: si deja de recibir comandos durante 1 segundo, detiene los motores.

## Pines

Motor izquierdo:
- forward: GPIO 24
- backward: GPIO 27
- enable: GPIO 5

Motor derecho:
- forward: GPIO 6
- backward: GPIO 22
- enable: GPIO 17

## Dependencias del sistema

```bash
sudo apt update
sudo apt install python3-picamera2 python3-gpiozero python3-lgpio python3-flask
```

En Raspberry Pi OS es recomendable instalar Picamera2 mediante apt.

## Ejecutar

```bash
cd robot_camera_control
python3 app.py
```

Después abre desde otro dispositivo:

```text
http://IP_DE_LA_RASPBERRY:5000
```

Para conocer la IP:

```bash
hostname -I
```

## API

Movimiento:

```bash
curl -X POST http://IP:5000/api/move \
  -H "Content-Type: application/json" \
  -d '{"direction":"forward","power":50}'
```

Direcciones:
- forward
- backward
- left
- right
- stop

Parar:

```bash
curl -X POST http://IP:5000/api/stop
```

Stream:

```text
http://IP:5000/stream
```

Health:

```text
http://IP:5000/api/health
```

## Importante

Prueba primero con las ruedas levantadas. Si un motor gira al revés, invierte sus GPIO `forward` y `backward` en `app.py`.
