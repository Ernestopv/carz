from flask import Flask, request, jsonify, Response, render_template
from gpiozero import Motor
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

import io
import threading
import time

app = Flask(__name__)


# -----------------------------
# MOTORES - CAMJAM EDUKIT 3
# -----------------------------
# Motor A -> GPIO 10 / GPIO 9
# Motor B -> GPIO 8 / GPIO 7
#
# Si una rueda gira al reves, intercambia forward/backward
# unicamente en ese motor.

# Motor izquierdo
motor1 = Motor(
    forward=9,
    backward=10,
    pwm=True
)

# Motor derecho
motor2 = Motor(
    forward=8,
    backward=7,
    pwm=True
)

# Guarda el instante del ultimo comando recibido.
last_command_time = time.monotonic()
command_lock = threading.Lock()

# Si durante este tiempo no llegan comandos,
# los motores se detienen.
MOTOR_TIMEOUT = 1.0


# -----------------------------
# FUNCIONES DE MOTORES
# -----------------------------
def normalize_power(power):
    try:
        power = int(power)
    except (ValueError, TypeError):
        raise ValueError("power debe ser un numero entre 0 y 100")

    return max(0, min(100, power))


def touch_command():
    global last_command_time

    with command_lock:
        last_command_time = time.monotonic()


def stop_motors():
    motor1.stop()
    motor2.stop()


def set_motor(motor, direction, power):
    power = normalize_power(power)

    # gpiozero Motor espera un valor entre 0.0 y 1.0
    speed = power / 100.0

    if direction == "forward":
        motor.forward(speed)

    elif direction == "backward":
        motor.backward(speed)

    elif direction == "stop":
        motor.stop()

    else:
        raise ValueError("Direccion invalida")

    touch_command()

    return power


def move_robot(direction, power):
    power = normalize_power(power)

    # Convierte 0-100 por ciento a 0.0-1.0
    speed = power / 100.0

    if direction == "forward":
        motor1.forward(speed)
        motor2.forward(speed)

    elif direction == "backward":
        motor1.backward(speed)
        motor2.backward(speed)

    elif direction == "left":
        # Giro sobre su propio eje
        motor2.forward(speed)
        motor1.backward(speed)
        
    elif direction == "right":
        # Giro sobre su propio eje
        motor2.backward(speed)
        motor1.forward(speed)
     
    elif direction == "stop":
        stop_motors()

    else:
        raise ValueError("Direccion invalida")

    touch_command()

    return power


def motor_watchdog():
    while True:
        time.sleep(0.1)

        with command_lock:
            elapsed = time.monotonic() - last_command_time

        if elapsed > MOTOR_TIMEOUT:
            stop_motors()


# -----------------------------
# CAMARA
# -----------------------------
class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


camera_output = StreamingOutput()
picam2 = Picamera2()

camera_config = picam2.create_video_configuration(
    main={
        "size": (640, 480),
        "format": "RGB888",
    },
    controls={
        "FrameRate": 30,
    },
)

picam2.configure(camera_config)

picam2.start_recording(
    MJPEGEncoder(),
    FileOutput(camera_output),
)


def generate_frames():
    while True:
        with camera_output.condition:
            camera_output.condition.wait()
            frame = camera_output.frame

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: "
            + str(len(frame)).encode()
            + b"\r\n\r\n"
            + frame
            + b"\r\n"
        )


# -----------------------------
# UI
# -----------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream")
def stream():
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


# -----------------------------
# API
# -----------------------------
@app.route("/api/motor/<int:motor_id>", methods=["POST"])
def control_motor(motor_id):
    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "error": "Falta JSON"
        }), 400

    direction = data.get("direction", "stop")
    power = data.get("power", 0)

    if motor_id == 1:
        motor = motor1

    elif motor_id == 2:
        motor = motor2

    else:
        return jsonify({
            "error": "Motor inexistente"
        }), 404

    try:
        power = set_motor(
            motor,
            direction,
            power
        )

    except ValueError as e:
        return jsonify({
            "error": str(e)
        }), 400

    return jsonify({
        "ok": True,
        "motor": motor_id,
        "direction": direction,
        "power": power,
    })


@app.route("/api/move", methods=["POST"])
def move():
    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "error": "Falta JSON"
        }), 400

    direction = data.get(
        "direction",
        "stop"
    )

    power = data.get(
        "power",
        50
    )

    try:
        power = move_robot(
            direction,
            power
        )

    except ValueError as e:
        return jsonify({
            "error": str(e)
        }), 400

    return jsonify({
        "ok": True,
        "direction": direction,
        "power": power,
    })


@app.route("/api/stop", methods=["POST"])
def stop_all():
    stop_motors()
    touch_command()

    return jsonify({
        "ok": True,
        "status": "Todos los motores detenidos",
    })


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "camera": "ok",
        "motors": "ok",
    })


@app.route("/api/ping", methods=["POST"])
def ping():
    # Mantiene vivo el movimiento mientras
    # el usuario mantiene un boton pulsado.
    touch_command()

    return jsonify({
        "ok": True
    })


# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    watchdog_thread = threading.Thread(
        target=motor_watchdog,
        daemon=True,
    )

    watchdog_thread.start()

    try:
        app.run(
            host="0.0.0.0",
            port=5000,
            debug=False,
            threaded=True,
        )

    finally:
        stop_motors()
        picam2.stop_recording()