"""Consola de teleoperacion para un rover con Raspberry Pi.

Servicio Flask que combina tres piezas:

* un stream MJPEG de la camara CSI mediante Picamera2;
* el control de dos motores DC a traves de gpiozero;
* un watchdog que detiene los motores si se pierde el enlace.

La aplicacion se arranca con ``python3 app.py``. El hardware se inyecta
en :func:`create_app`, de modo que la logica se puede probar sin una
Raspberry Pi real (ver ``tests/``).
"""

from __future__ import annotations

import contextlib
import io
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, cast

from flask import Flask, Response, jsonify, render_template, request

# ============================================================
# CONTRATOS DE HARDWARE
# ============================================================


class MotorLike(Protocol):
    """Contrato minimo que necesita un motor DC controlable."""

    def forward(self, speed: float = 0.0) -> None:
        """Gira el motor hacia delante a la velocidad indicada."""

    def backward(self, speed: float = 0.0) -> None:
        """Gira el motor hacia atras a la velocidad indicada."""

    def stop(self) -> None:
        """Detiene el motor."""

    def close(self) -> None:
        """Libera los recursos asociados al motor."""


class CameraLike(Protocol):
    """Contrato minimo que necesita la camara para servirse por HTTP."""

    @property
    def available(self) -> bool:
        """Indica si hay un stream de camara operativo."""

    @property
    def error(self) -> str | None:
        """Ultimo error de inicializacion de la camara, si lo hubo."""

    def frames(self) -> Iterator[bytes]:
        """Genera las partes multipart del stream MJPEG."""


class Picamera2Like(Protocol):
    """Subconjunto de Picamera2 que usa :meth:`CameraStream.stop`."""

    def stop_recording(self) -> None:
        """Detiene la grabacion en curso."""

    def close(self) -> None:
        """Cierra el dispositivo de camara."""


# ============================================================
# CONFIGURACION
# ============================================================


@dataclass(frozen=True, slots=True)
class Settings:
    """Parametros de ejecucion del rover.

    Attributes:
        motor_timeout: Segundos sin comandos antes de frenar por seguridad.
        motor1_calibration: Factor de correccion del motor izquierdo.
        motor2_calibration: Factor de correccion del motor derecho.
        camera_width: Ancho del stream en pixeles.
        camera_height: Alto del stream en pixeles.
        camera_fps: Fotogramas por segundo objetivo.
        camera_index: Indice de la camara a abrir.
        left_forward_pin: GPIO forward del motor izquierdo.
        left_backward_pin: GPIO backward del motor izquierdo.
        right_forward_pin: GPIO forward del motor derecho.
        right_backward_pin: GPIO backward del motor derecho.
        host: Interfaz de red donde escucha Flask.
        port: Puerto TCP donde escucha Flask.
    """

    motor_timeout: float = 1.0
    motor1_calibration: float = 0.60
    motor2_calibration: float = 1.00

    camera_width: int = 480
    camera_height: int = 360
    camera_fps: int = 24
    camera_index: int = 0

    left_forward_pin: int = 9
    left_backward_pin: int = 10
    right_forward_pin: int = 8
    right_backward_pin: int = 7

    host: str = "0.0.0.0"
    port: int = 5000


# ============================================================
# MATEMATICA DE MOTORES
# ============================================================


def normalize_power(power: object) -> int:
    """Normaliza una potencia a un entero entre 0 y 100.

    Args:
        power: Potencia de entrada; admite enteros, flotantes o cadenas
            numericas.

    Returns:
        La potencia limitada al rango ``[0, 100]``.

    Raises:
        ValueError: Si ``power`` no se puede interpretar como un numero.
    """
    try:
        if isinstance(power, (int, float, str)):
            value = int(power)
        else:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("power debe ser un numero entre 0 y 100") from exc

    return max(0, min(100, value))


def calibrated_speed(power: object, calibration: float) -> float:
    """Convierte una potencia 0-100 en velocidad gpiozero 0.0-1.0.

    Args:
        power: Potencia normalizada o normalizable.
        calibration: Factor de correccion del motor (0.0 a 1.0).

    Returns:
        Velocidad resultante limitada al rango ``[0.0, 1.0]``.
    """
    normalized = normalize_power(power)
    speed = (normalized / 100.0) * calibration
    return max(0.0, min(1.0, speed))


# ============================================================
# CONTROL DE MOTORES
# ============================================================


class MotorController:
    """Aplica movimiento, calibracion y watchdog sobre dos motores."""

    def __init__(
        self,
        left: MotorLike,
        right: MotorLike,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Inicializa el controlador.

        Args:
            left: Motor izquierdo.
            right: Motor derecho.
            settings: Configuracion con calibraciones y timeout.
            clock: Fuente de tiempo inyectable para el watchdog.
        """
        self._left = left
        self._right = right
        self._left_calibration = settings.motor1_calibration
        self._right_calibration = settings.motor2_calibration
        self._timeout = settings.motor_timeout
        self._clock = clock
        self._last_command = clock()
        self._lock = threading.Lock()
        self._stopped = False

    @property
    def left_calibration(self) -> float:
        """Factor de calibracion del motor izquierdo."""
        return self._left_calibration

    @property
    def right_calibration(self) -> float:
        """Factor de calibracion del motor derecho."""
        return self._right_calibration

    @property
    def timeout(self) -> float:
        """Segundos sin comandos antes de frenar por seguridad."""
        return self._timeout

    def touch(self) -> None:
        """Marca el instante del ultimo comando recibido."""
        with self._lock:
            self._last_command = self._clock()

    def stop(self) -> None:
        """Detiene ambos motores sin actualizar el watchdog."""
        self._left.stop()
        self._right.stop()

    def close(self) -> None:
        """Detiene y libera ambos motores."""
        self.stop()
        self._left.close()
        self._right.close()

    def move(self, direction: str, power: object) -> int:
        """Mueve el coche completo en la direccion indicada.

        Args:
            direction: ``forward``, ``backward``, ``left``, ``right`` o
                ``stop``.
            power: Potencia objetivo entre 0 y 100.

        Returns:
            La potencia normalizada que se ha aplicado.

        Raises:
            ValueError: Si la direccion no es valida.
        """
        normalized = normalize_power(power)
        left_speed = calibrated_speed(normalized, self._left_calibration)
        right_speed = calibrated_speed(normalized, self._right_calibration)

        if normalized == 0:
            self.stop()
            self.touch()
            return normalized

        if direction == "forward":
            self._left.forward(left_speed)
            self._right.forward(right_speed)
        elif direction == "backward":
            self._left.backward(left_speed)
            self._right.backward(right_speed)
        elif direction == "left":
            # Giro sobre su propio eje.
            self._left.backward(left_speed)
            self._right.forward(right_speed)
        elif direction == "right":
            self._left.forward(left_speed)
            self._right.backward(right_speed)
        elif direction == "stop":
            self.stop()
        else:
            raise ValueError(
                "Direccion invalida. Usa forward, backward, left, right o stop"
            )

        self.touch()
        return normalized

    def set_motor(
        self,
        motor_id: int,
        direction: str,
        power: object,
    ) -> int:
        """Controla un motor concreto de forma independiente.

        Args:
            motor_id: ``1`` para el izquierdo, ``2`` para el derecho.
            direction: ``forward``, ``backward`` o ``stop``.
            power: Potencia objetivo entre 0 y 100.

        Returns:
            La potencia normalizada que se ha aplicado.

        Raises:
            ValueError: Si el motor o la direccion no son validos.
        """
        if motor_id == 1:
            motor = self._left
            calibration = self._left_calibration
        elif motor_id == 2:
            motor = self._right
            calibration = self._right_calibration
        else:
            raise ValueError("Motor inexistente")

        normalized = normalize_power(power)
        speed = calibrated_speed(normalized, calibration)

        if direction == "forward":
            if normalized == 0:
                motor.stop()
            else:
                motor.forward(speed)
        elif direction == "backward":
            if normalized == 0:
                motor.stop()
            else:
                motor.backward(speed)
        elif direction == "stop":
            motor.stop()
        else:
            raise ValueError("Direccion invalida. Usa forward, backward o stop")

        self.touch()
        return normalized

    def tick(self) -> None:
        """Comprueba el watchdog una vez y frena si expiro el timeout."""
        with self._lock:
            elapsed = self._clock() - self._last_command

        if elapsed > self._timeout:
            if not self._stopped:
                self.stop()
                self._stopped = True
        else:
            self._stopped = False

    def watchdog(self, interval: float = 0.1) -> None:  # pragma: no cover
        """Vigila el enlace de forma continua hasta que el proceso termina.

        Args:
            interval: Segundos entre comprobaciones.
        """
        while True:
            time.sleep(interval)
            self.tick()


# ============================================================
# CAMARA
# ============================================================


def mjpeg_part(frame: bytes) -> bytes:
    """Envuelve un frame JPEG en una parte multipart MJPEG.

    Args:
        frame: Bytes del JPEG codificado.

    Returns:
        La parte multipart lista para escribirse en el stream.
    """
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Cache-Control: no-cache, no-store, must-revalidate\r\n"
        b"Pragma: no-cache\r\n"
        b"Expires: 0\r\n"
        b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
    )


class StreamingOutput(io.BufferedIOBase):
    """Buffer que conserva unicamente el frame JPEG mas reciente."""

    def __init__(self) -> None:
        """Inicializa el buffer sin ningun frame."""
        super().__init__()
        self.frame: bytes | None = None
        self.frame_id = 0
        self.condition = threading.Condition()

    def write(self, b: bytes, /) -> int:  # type: ignore[override]
        """Almacena el frame recibido y despierta a los lectores.

        Args:
            b: Bytes del frame JPEG.

        Returns:
            El numero de bytes consumidos.
        """
        frame = bytes(b)
        with self.condition:
            self.frame = frame
            self.frame_id += 1
            self.condition.notify_all()
        return len(frame)


class CameraStream:
    """Camara CSI basada en Picamera2 con entrega de baja latencia."""

    def __init__(self, settings: Settings) -> None:
        """Prepara la camara sin abrir el dispositivo.

        Args:
            settings: Configuracion de resolucion y FPS.
        """
        self._settings = settings
        self._output = StreamingOutput()
        self._picam: object | None = None
        self._available = False
        self._error: str | None = None

    @property
    def available(self) -> bool:
        """Indica si la camara esta grabando."""
        return self._available

    @property
    def error(self) -> str | None:
        """Ultimo error de inicializacion, si lo hubo."""
        return self._error

    def start(self) -> None:  # pragma: no cover - requiere hardware
        """Inicializa Picamera2 y comienza a grabar en MJPEG."""
        try:
            from picamera2 import Picamera2
            from picamera2.encoders import MJPEGEncoder
            from picamera2.outputs import FileOutput

            if not Picamera2.global_camera_info():
                self._available = False
                self._error = "No cameras available"
                print("[CAMERA] No se ha detectado ninguna camara.")
                return

            picam = Picamera2(self._settings.camera_index)
            config = picam.create_video_configuration(
                main={
                    "size": (
                        self._settings.camera_width,
                        self._settings.camera_height,
                    ),
                    "format": "YUV420",
                },
                controls={"FrameRate": self._settings.camera_fps},
                buffer_count=2,
            )
            picam.configure(config)
            picam.start_recording(MJPEGEncoder(), FileOutput(self._output))

            self._picam = picam
            self._available = True
            self._error = None
            print(
                "[CAMERA] Camara iniciada: "
                f"{self._settings.camera_width}x"
                f"{self._settings.camera_height} @ "
                f"{self._settings.camera_fps} FPS"
            )
        except Exception as exc:
            self._available = False
            self._error = str(exc)
            self._picam = None
            print("[CAMERA] Error inicializando camara:")
            print(exc)

    def stop(self) -> None:
        """Detiene la grabacion y libera la camara."""
        picam = self._picam
        if picam is None:
            return

        device = cast(Picamera2Like, picam)
        with contextlib.suppress(Exception):
            device.stop_recording()
        with contextlib.suppress(Exception):
            device.close()

        self._picam = None
        self._available = False

    def frames(self) -> Iterator[bytes]:
        """Genera las partes multipart del stream MJPEG.

        Yields:
            Cada frame disponible como parte multipart MJPEG.
        """
        last_frame_id = -1

        while self._available:
            with self._output.condition:

                def has_new_frame(current: int = last_frame_id) -> bool:
                    return not self._available or self._output.frame_id != current

                self._output.condition.wait_for(
                    has_new_frame,
                    timeout=1.0,
                )

                if not self._available:
                    break  # pragma: no cover - depende de una carrera

                frame = self._output.frame
                last_frame_id = self._output.frame_id

            if frame is None:
                continue  # pragma: no cover - depende de una carrera

            yield mjpeg_part(frame)


# ============================================================
# APLICACION FLASK
# ============================================================


def create_app(controller: MotorController, camera: CameraLike) -> Flask:
    """Construye la aplicacion Flask con sus dependencias inyectadas.

    Args:
        controller: Controlador de motores.
        camera: Fuente de video.

    Returns:
        La aplicacion Flask lista para ejecutarse.
    """
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        """Devuelve la interfaz de control."""
        return render_template("index.html")

    @app.get("/stream")
    def stream() -> Response | tuple[Response, int]:
        """Sirve el stream MJPEG de la camara."""
        if not camera.available:
            return (
                jsonify(
                    {
                        "error": "Camara no disponible",
                        "details": camera.error,
                    }
                ),
                503,
            )

        response = Response(
            camera.frames(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )
        response.headers["Cache-Control"] = (
            "no-cache, no-store, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.post("/api/motor/<int:motor_id>")
    def control_motor(motor_id: int) -> Response | tuple[Response, int]:
        """Controla un motor individual."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Falta JSON"}), 400

        direction = data.get("direction", "stop")
        power = data.get("power", 0)

        try:
            applied = controller.set_motor(motor_id, direction, power)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(
            {
                "ok": True,
                "motor": motor_id,
                "direction": direction,
                "power": applied,
            }
        )

    @app.post("/api/move")
    def move() -> Response | tuple[Response, int]:
        """Mueve el coche completo."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Falta JSON"}), 400

        direction = data.get("direction", "stop")
        power = data.get("power", 50)

        try:
            applied = controller.move(direction, power)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(
            {
                "ok": True,
                "direction": direction,
                "power": applied,
                "motor1_speed": calibrated_speed(applied, controller.left_calibration),
                "motor2_speed": calibrated_speed(applied, controller.right_calibration),
            }
        )

    @app.post("/api/stop")
    def stop_all() -> Response:
        """Detiene todos los motores."""
        controller.stop()
        controller.touch()
        return jsonify({"ok": True, "status": "Todos los motores detenidos"})

    @app.post("/api/ping")
    def ping() -> Response:
        """Mantiene vivo el watchdog del servidor."""
        controller.touch()
        return jsonify({"ok": True})

    @app.get("/api/health")
    def health() -> Response:
        """Devuelve el estado de camara, motores y calibracion."""
        return jsonify(
            {
                "status": "ok",
                "camera": "ok" if camera.available else "not_available",
                "camera_error": None if camera.available else camera.error,
                "motors": "ok",
                "calibration": {
                    "motor1": controller.left_calibration,
                    "motor2": controller.right_calibration,
                },
                "motor_timeout": controller.timeout,
            }
        )

    return app


# ============================================================
# CONSTRUCCION DEL HARDWARE REAL
# ============================================================


def build_motor_controller(settings: Settings) -> MotorController:  # pragma: no cover
    """Crea los dos motores gpiozero y su controlador.

    Args:
        settings: Pines y calibraciones a usar.

    Returns:
        Un controlador conectado a los motores reales.
    """
    from gpiozero import Motor

    left = cast(
        "MotorLike",
        Motor(
            forward=settings.left_forward_pin,
            backward=settings.left_backward_pin,
            pwm=True,
        ),
    )
    right = cast(
        "MotorLike",
        Motor(
            forward=settings.right_forward_pin,
            backward=settings.right_backward_pin,
            pwm=True,
        ),
    )
    return MotorController(left, right, settings)


def main() -> None:  # pragma: no cover
    """Arranca la camara, el watchdog y el servidor Flask."""
    settings = Settings()
    controller = build_motor_controller(settings)
    camera = CameraStream(settings)
    camera.start()

    watchdog = threading.Thread(target=controller.watchdog, daemon=True)
    watchdog.start()

    app = create_app(controller, camera)

    print(f"[APP] Servidor en http://{settings.host}:{settings.port}")

    try:
        app.run(
            host=settings.host,
            port=settings.port,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
    except KeyboardInterrupt:
        print("\n[APP] Cerrando servidor...")
    finally:
        print("[MOTOR] Deteniendo motores...")
        controller.close()
        camera.stop()
        print("[APP] Aplicacion cerrada.")


if __name__ == "__main__":  # pragma: no cover
    main()
