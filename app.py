from flask import Flask, request, jsonify, Response, render_template
from gpiozero import Motor
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

import io
import threading
import time


app = Flask(__name__)


# ============================================================
# CONFIGURACION
# ============================================================

# Tiempo maximo sin recibir comandos antes de detener motores.
MOTOR_TIMEOUT = 1.0

# ------------------------------------------------------------
# CALIBRACION DE MOTORES
# ------------------------------------------------------------
#
# 1.00 = 100 %
# 0.95 = 95 %
# 0.90 = 90 %
#
# Si el coche se va hacia la IZQUIERDA:
# normalmente el motor derecho empuja demasiado.
# Baja MOTOR2_CALIBRATION.
#
# Si el coche se va hacia la DERECHA:
# normalmente el motor izquierdo empuja demasiado.
# Baja MOTOR1_CALIBRATION.

MOTOR1_CALIBRATION = 0.60
MOTOR2_CALIBRATION = 1.00


# ============================================================
# MOTORES - CAMJAM EDUKIT 3
# ============================================================

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


last_command_time = time.monotonic()
command_lock = threading.Lock()


# ============================================================
# FUNCIONES DE MOTORES
# ============================================================

def normalize_power(power):
    """
    Convierte power a entero y lo limita entre 0 y 100.
    """

    try:
        power = int(power)

    except (ValueError, TypeError):
        raise ValueError(
            "power debe ser un numero entre 0 y 100"
        )

    return max(0, min(100, power))


def calibrated_speed(power, calibration):
    """
    Convierte potencia 0-100 a velocidad gpiozero 0.0-1.0
    aplicando calibracion.
    """

    power = normalize_power(power)

    speed = (power / 100.0) * calibration

    return max(0.0, min(1.0, speed))


def touch_command():
    """
    Actualiza el instante del ultimo comando recibido.
    """

    global last_command_time

    with command_lock:
        last_command_time = time.monotonic()


def stop_motors():
    """
    Detiene inmediatamente los dos motores.
    """

    motor1.stop()
    motor2.stop()


def set_motor(motor_id, direction, power):
    """
    Control individual de motor con calibracion.
    """

    power = normalize_power(power)

    if motor_id == 1:
        motor = motor1
        calibration = MOTOR1_CALIBRATION

    elif motor_id == 2:
        motor = motor2
        calibration = MOTOR2_CALIBRATION

    else:
        raise ValueError("Motor inexistente")

    speed = calibrated_speed(
        power,
        calibration
    )

    if direction == "forward":

        if power == 0:
            motor.stop()
        else:
            motor.forward(speed)

    elif direction == "backward":

        if power == 0:
            motor.stop()
        else:
            motor.backward(speed)

    elif direction == "stop":

        motor.stop()

    else:

        raise ValueError(
            "Direccion invalida. "
            "Usa forward, backward o stop"
        )

    touch_command()

    return power


def move_robot(direction, power):
    """
    Control de movimiento completo del coche.
    """

    power = normalize_power(power)

    speed1 = calibrated_speed(
        power,
        MOTOR1_CALIBRATION
    )

    speed2 = calibrated_speed(
        power,
        MOTOR2_CALIBRATION
    )

    # Si power es cero detenemos directamente.
    if power == 0:
        stop_motors()
        touch_command()
        return power

    if direction == "forward":

        motor1.forward(speed1)
        motor2.forward(speed2)

    elif direction == "backward":

        motor1.backward(speed1)
        motor2.backward(speed2)

    elif direction == "left":

        # Giro sobre su propio eje
        motor1.backward(speed1)
        motor2.forward(speed2)

    elif direction == "right":

        # Giro sobre su propio eje
        motor1.forward(speed1)
        motor2.backward(speed2)

    elif direction == "stop":

        stop_motors()

    else:

        raise ValueError(
            "Direccion invalida. "
            "Usa forward, backward, left, right o stop"
        )

    touch_command()

    return power


def motor_watchdog():
    """
    Si durante MOTOR_TIMEOUT segundos no llega ningun comando,
    detiene automaticamente el coche.
    """

    global last_command_time

    motors_stopped = False

    while True:

        time.sleep(0.1)

        with command_lock:
            elapsed = (
                time.monotonic()
                - last_command_time
            )

        if elapsed > MOTOR_TIMEOUT:

            if not motors_stopped:
                stop_motors()
                motors_stopped = True

        else:

            motors_stopped = False


# ============================================================
# CAMARA
# ============================================================

class StreamingOutput(io.BufferedIOBase):

    def __init__(self):

        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):

        with self.condition:

            self.frame = bytes(buf)

            self.condition.notify_all()

        return len(buf)


camera_output = StreamingOutput()

picam2 = None
camera_available = False
camera_error = None


def initialize_camera():
    """
    Inicializa la camara.
    Si no existe ninguna camara, Flask sigue funcionando.
    """

    global picam2
    global camera_available
    global camera_error

    try:

        cameras = Picamera2.global_camera_info()

        if not cameras:

            camera_available = False
            camera_error = "No cameras available"

            print(
                "[CAMERA] No se ha detectado ninguna camara."
            )

            return

        print(
            f"[CAMERA] Camaras detectadas: {cameras}"
        )

        picam2 = Picamera2(0)

        camera_config = (
            picam2.create_video_configuration(
                main={
                    "size": (640, 480),

                    # MJPEGEncoder funciona correctamente
                    # utilizando YUV420 como entrada.
                    "format": "YUV420",
                },
                controls={
                    "FrameRate": 30
                },
            )
        )

        picam2.configure(
            camera_config
        )

        picam2.start_recording(
            MJPEGEncoder(),
            FileOutput(camera_output),
        )

        camera_available = True
        camera_error = None

        print(
            "[CAMERA] Camara iniciada correctamente."
        )

    except Exception as e:

        camera_available = False
        camera_error = str(e)

        picam2 = None

        print(
            "[CAMERA] Error inicializando camara:"
        )

        print(e)


initialize_camera()


def generate_frames():
    """
    Generador MJPEG para el navegador.
    """

    while camera_available:

        with camera_output.condition:

            camera_output.condition.wait(
                timeout=5
            )

            frame = camera_output.frame

        if frame is None:
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: "
            + str(len(frame)).encode()
            + b"\r\n\r\n"
            + frame
            + b"\r\n"
        )


# ============================================================
# UI
# ============================================================

@app.route("/")
def index():

    return render_template(
        "index.html"
    )


@app.route("/stream")
def stream():

    if not camera_available:

        return jsonify({
            "error": "Camara no disponible",
            "details": camera_error,
        }), 503

    return Response(
        generate_frames(),
        mimetype=(
            "multipart/x-mixed-replace; "
            "boundary=frame"
        ),
    )


# ============================================================
# API MOTOR INDIVIDUAL
# ============================================================

@app.route(
    "/api/motor/<int:motor_id>",
    methods=["POST"]
)
def control_motor(motor_id):

    data = request.get_json(
        silent=True
    )

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
        0
    )

    try:

        power = set_motor(
            motor_id,
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


# ============================================================
# API MOVIMIENTO COMPLETO
# ============================================================

@app.route(
    "/api/move",
    methods=["POST"]
)
def move():

    data = request.get_json(
        silent=True
    )

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
        "motor1_speed": calibrated_speed(
            power,
            MOTOR1_CALIBRATION
        ),
        "motor2_speed": calibrated_speed(
            power,
            MOTOR2_CALIBRATION
        ),
    })


# ============================================================
# PARADA
# ============================================================

@app.route(
    "/api/stop",
    methods=["POST"]
)
def stop_all():

    stop_motors()
    touch_command()

    return jsonify({
        "ok": True,
        "status": "Todos los motores detenidos",
    })


# ============================================================
# KEEP ALIVE / WATCHDOG
# ============================================================

@app.route(
    "/api/ping",
    methods=["POST"]
)
def ping():

    touch_command()

    return jsonify({
        "ok": True
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/api/health")
def health():

    return jsonify({

        "status": "ok",

        "camera": (
            "ok"
            if camera_available
            else "not_available"
        ),

        "camera_error": (
            None
            if camera_available
            else camera_error
        ),

        "motors": "ok",

        "calibration": {
            "motor1": MOTOR1_CALIBRATION,
            "motor2": MOTOR2_CALIBRATION,
        },

        "motor_timeout": MOTOR_TIMEOUT,
    })


# ============================================================
# MAIN
# ============================================================

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
            use_reloader=False,
        )

    except KeyboardInterrupt:

        print(
            "\n[APP] Cerrando servidor..."
        )

    finally:

        print(
            "[MOTOR] Deteniendo motores..."
        )

        stop_motors()

        if picam2 is not None:

            try:

                picam2.stop_recording()

            except Exception:

                pass

            try:

                picam2.close()

            except Exception:

                pass

        motor1.close()
        motor2.close()

        print(
            "[APP] Aplicacion cerrada."
        )
