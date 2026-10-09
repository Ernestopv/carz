"""Pruebas de la logica del rover sin Raspberry Pi.

Los motores y la camara se sustituyen por dobles que implementan los
mismos protocolos que el hardware real, de modo que se ejercita la
logica completa (calibracion, watchdog, rutas HTTP) de forma aislada.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask.testing import FlaskClient

import app as carz

# ============================================================
# DOBLES DE HARDWARE
# ============================================================


class FakeMotor:
    """Motor en memoria que registra cada llamada recibida."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.closed = False

    def forward(self, speed: float = 0.0) -> None:
        self.calls.append(("forward", speed))

    def backward(self, speed: float = 0.0) -> None:
        self.calls.append(("backward", speed))

    def stop(self) -> None:
        self.calls.append(("stop", 0.0))

    def close(self) -> None:
        self.closed = True

    @property
    def last(self) -> tuple[str, float]:
        return self.calls[-1]


class FakeCamera:
    """Camara simulada con disponibilidad y error configurables."""

    def __init__(
        self,
        available: bool = True,
        error: str | None = None,
    ) -> None:
        self.available = available
        self.error = error

    def frames(self) -> Iterator[bytes]:
        yield b"fake-frame"


class ManualClock:
    """Reloj controlable para probar el watchdog sin esperas reales."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakePicam:
    """Dispositivo Picamera2 simulado que registra el cierre."""

    def __init__(self) -> None:
        self.stopped = False
        self.closed = False

    def stop_recording(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


# ============================================================
# FIXTURES
# ============================================================


@pytest.fixture
def settings() -> carz.Settings:
    return carz.Settings()


@pytest.fixture
def left() -> FakeMotor:
    return FakeMotor()


@pytest.fixture
def right() -> FakeMotor:
    return FakeMotor()


@pytest.fixture
def controller(
    left: FakeMotor,
    right: FakeMotor,
    settings: carz.Settings,
) -> carz.MotorController:
    return carz.MotorController(left, right, settings)


@pytest.fixture
def client(controller: carz.MotorController) -> FlaskClient:
    flask_app = carz.create_app(controller, FakeCamera(available=True))
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


# ============================================================
# NORMALIZACION DE POTENCIA
# ============================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (50, 50),
        (100, 100),
        (150, 100),
        (-20, 0),
        (49.9, 49),
        ("75", 75),
    ],
)
def test_normalize_power_clamps_and_parses(raw: object, expected: int) -> None:
    assert carz.normalize_power(raw) == expected


@pytest.mark.parametrize("raw", ["abc", None, object()])
def test_normalize_power_rejects_non_numeric(raw: object) -> None:
    with pytest.raises(ValueError):
        carz.normalize_power(raw)


@pytest.mark.parametrize(
    ("power", "calibration", "expected"),
    [
        (100, 1.0, 1.0),
        (100, 0.6, 0.6),
        (50, 1.0, 0.5),
        (0, 1.0, 0.0),
        (200, 2.0, 1.0),
    ],
)
def test_calibrated_speed(
    power: int,
    calibration: float,
    expected: float,
) -> None:
    assert carz.calibrated_speed(power, calibration) == pytest.approx(expected)


# ============================================================
# MOVIMIENTO
# ============================================================


def test_move_forward_uses_calibrated_speeds(
    controller: carz.MotorController,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    applied = controller.move("forward", 100)

    assert applied == 100
    assert left.last == ("forward", 0.6)
    assert right.last == ("forward", 1.0)


def test_move_backward(
    controller: carz.MotorController,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    controller.move("backward", 50)

    assert left.last[0] == "backward"
    assert left.last[1] == pytest.approx(0.3)
    assert right.last[0] == "backward"
    assert right.last[1] == pytest.approx(0.5)


def test_move_left_spins_on_axis(
    controller: carz.MotorController,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    controller.move("left", 100)

    assert left.last[0] == "backward"
    assert right.last[0] == "forward"


def test_move_right_spins_on_axis(
    controller: carz.MotorController,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    controller.move("right", 100)

    assert left.last[0] == "forward"
    assert right.last[0] == "backward"


def test_move_stop_direction_stops(
    controller: carz.MotorController,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    controller.move("stop", 50)

    assert left.last == ("stop", 0.0)
    assert right.last == ("stop", 0.0)


def test_move_zero_power_stops(
    controller: carz.MotorController,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    controller.move("forward", 0)

    assert left.last == ("stop", 0.0)
    assert right.last == ("stop", 0.0)


def test_move_invalid_direction_raises(
    controller: carz.MotorController,
) -> None:
    with pytest.raises(ValueError):
        controller.move("sideways", 50)


def test_move_invalid_direction_with_zero_power_is_ignored(
    controller: carz.MotorController,
) -> None:
    # La parada tiene prioridad sobre la validacion, igual que antes.
    assert controller.move("sideways", 0) == 0


# ============================================================
# MOTOR INDIVIDUAL
# ============================================================


def test_set_motor_only_moves_requested_motor(
    controller: carz.MotorController,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    controller.set_motor(1, "forward", 100)

    assert left.last == ("forward", 0.6)
    assert right.calls == []


def test_set_motor_zero_power_stops(
    controller: carz.MotorController,
    left: FakeMotor,
) -> None:
    controller.set_motor(1, "forward", 0)

    assert left.last == ("stop", 0.0)


def test_set_motor_two_moves_right(
    controller: carz.MotorController,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    controller.set_motor(2, "forward", 100)

    assert right.last == ("forward", 1.0)
    assert left.calls == []


def test_set_motor_backward(
    controller: carz.MotorController,
    left: FakeMotor,
) -> None:
    controller.set_motor(1, "backward", 50)

    assert left.last[0] == "backward"
    assert left.last[1] == pytest.approx(0.3)


def test_set_motor_backward_zero_power_stops(
    controller: carz.MotorController,
    left: FakeMotor,
) -> None:
    controller.set_motor(1, "backward", 0)

    assert left.last == ("stop", 0.0)


def test_set_motor_stop_direction(
    controller: carz.MotorController,
    left: FakeMotor,
) -> None:
    controller.set_motor(1, "stop", 50)

    assert left.last == ("stop", 0.0)


def test_set_motor_invalid_direction_raises(
    controller: carz.MotorController,
) -> None:
    with pytest.raises(ValueError, match="Direccion invalida"):
        controller.set_motor(1, "sideways", 50)


def test_set_motor_unknown_motor_raises(
    controller: carz.MotorController,
) -> None:
    with pytest.raises(ValueError, match="Motor inexistente"):
        controller.set_motor(3, "forward", 50)


def test_close_releases_motors(
    controller: carz.MotorController,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    controller.close()

    assert left.closed is True
    assert right.closed is True


# ============================================================
# WATCHDOG
# ============================================================


def test_watchdog_stops_after_timeout(
    left: FakeMotor,
    right: FakeMotor,
    settings: carz.Settings,
) -> None:
    clock = ManualClock()
    controller = carz.MotorController(left, right, settings, clock=clock)

    controller.move("forward", 50)
    clock.now += settings.motor_timeout + 0.5
    controller.tick()

    assert left.last == ("stop", 0.0)
    assert right.last == ("stop", 0.0)


def test_watchdog_keeps_motors_before_timeout(
    left: FakeMotor,
    right: FakeMotor,
    settings: carz.Settings,
) -> None:
    clock = ManualClock()
    controller = carz.MotorController(left, right, settings, clock=clock)

    controller.move("forward", 50)
    clock.now += settings.motor_timeout / 2
    controller.tick()

    assert left.last[0] == "forward"


def test_watchdog_does_not_restop_while_timed_out(
    left: FakeMotor,
    right: FakeMotor,
    settings: carz.Settings,
) -> None:
    clock = ManualClock()
    controller = carz.MotorController(left, right, settings, clock=clock)

    controller.move("forward", 50)
    clock.now += settings.motor_timeout + 0.5
    controller.tick()

    stops = left.calls.count(("stop", 0.0))

    clock.now += 1.0
    controller.tick()

    assert left.calls.count(("stop", 0.0)) == stops


# ============================================================
# CAMARA
# ============================================================


def test_mjpeg_part_wraps_frame() -> None:
    part = carz.mjpeg_part(b"jpeg")

    assert part.startswith(b"--frame\r\n")
    assert b"Content-Type: image/jpeg" in part
    assert b"Content-Length: 4" in part
    assert part.endswith(b"jpeg\r\n")


def test_camera_stream_yields_frames(settings: carz.Settings) -> None:
    camera = carz.CameraStream(settings)
    camera._available = True
    camera._output.frame = b"jpegdata"
    camera._output.frame_id = 1

    generator = camera.frames()
    part = next(generator)
    assert b"jpegdata" in part

    # Al dejar de estar disponible, el generador se agota solo.
    camera._available = False
    with pytest.raises(StopIteration):
        next(generator)


def test_camera_stream_not_available_by_default(
    settings: carz.Settings,
) -> None:
    camera = carz.CameraStream(settings)

    assert camera.available is False
    assert camera.error is None


def test_streaming_output_keeps_latest_frame() -> None:
    output = carz.StreamingOutput()

    assert output.write(b"one") == 3
    assert output.write(b"two") == 3
    assert output.frame == b"two"
    assert output.frame_id == 2


def test_camera_stream_stop_releases_device(
    settings: carz.Settings,
) -> None:
    camera = carz.CameraStream(settings)
    device = FakePicam()
    camera._picam = device
    camera._available = True

    camera.stop()

    assert device.stopped is True
    assert device.closed is True
    assert camera.available is False


def test_camera_stream_stop_without_device(
    settings: carz.Settings,
) -> None:
    # No debe lanzar si nunca se abrio la camara.
    carz.CameraStream(settings).stop()


# ============================================================
# RUTAS HTTP
# ============================================================


def test_index_serves_interface(client: FlaskClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert b"Carz" in response.data


def test_health_reports_state(
    client: FlaskClient,
    controller: carz.MotorController,
) -> None:
    payload = client.get("/api/health").get_json()

    assert payload["status"] == "ok"
    assert payload["camera"] == "ok"
    assert payload["motors"] == "ok"
    assert payload["calibration"]["motor1"] == controller.left_calibration
    assert payload["motor_timeout"] == controller.timeout


def test_move_endpoint(
    client: FlaskClient,
    left: FakeMotor,
    right: FakeMotor,
) -> None:
    response = client.post("/api/move", json={"direction": "forward", "power": 100})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["power"] == 100
    assert payload["motor1_speed"] == pytest.approx(0.6)
    assert payload["motor2_speed"] == pytest.approx(1.0)
    assert left.last[0] == "forward"


def test_move_endpoint_requires_json(client: FlaskClient) -> None:
    response = client.post("/api/move")

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_move_endpoint_rejects_bad_direction(client: FlaskClient) -> None:
    response = client.post("/api/move", json={"direction": "up", "power": 50})

    assert response.status_code == 400


def test_motor_endpoint(client: FlaskClient, left: FakeMotor) -> None:
    response = client.post("/api/motor/1", json={"direction": "forward", "power": 100})

    assert response.status_code == 200
    assert response.get_json()["motor"] == 1
    assert left.last == ("forward", 0.6)


def test_motor_endpoint_unknown_motor(client: FlaskClient) -> None:
    response = client.post("/api/motor/5", json={"direction": "forward", "power": 50})

    assert response.status_code == 400


def test_motor_endpoint_requires_json(client: FlaskClient) -> None:
    response = client.post("/api/motor/1")

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_stop_endpoint(client: FlaskClient, left: FakeMotor) -> None:
    response = client.post("/api/stop")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert left.last == ("stop", 0.0)


def test_ping_endpoint(client: FlaskClient) -> None:
    assert client.post("/api/ping").get_json() == {"ok": True}


def test_stream_unavailable_returns_503(
    controller: carz.MotorController,
) -> None:
    flask_app = carz.create_app(controller, FakeCamera(available=False, error="no cam"))
    flask_app.config.update(TESTING=True)

    response = flask_app.test_client().get("/stream")

    assert response.status_code == 503
    assert response.get_json()["details"] == "no cam"


def test_stream_available_returns_mjpeg(
    controller: carz.MotorController,
) -> None:
    flask_app = carz.create_app(controller, FakeCamera(available=True))
    flask_app.config.update(TESTING=True)

    response = flask_app.test_client().get("/stream")

    assert response.status_code == 200
    assert response.mimetype == "multipart/x-mixed-replace"
    assert b"fake-frame" in response.data
