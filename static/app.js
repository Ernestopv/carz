const powerSlider = document.getElementById("power");
const powerValue = document.getElementById("powerValue");
const stopButton = document.getElementById("stopButton");
const controlButtons = document.querySelectorAll("[data-direction]");
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");
const throttleTrack = document.getElementById("throttleTrack");
const osdPower = document.getElementById("osdPower");
const meterLeft = document.getElementById("meterLeft");
const meterRight = document.getElementById("meterRight");
const meterLeftValue = document.getElementById("meterLeftValue");
const meterRightValue = document.getElementById("meterRightValue");
const camera = document.getElementById("camera");
const viewport = document.getElementById("viewport");
const nightToggle = document.getElementById("nightToggle");

let activeDirection = null;
let pingTimer = null;

const pressedKeys = new Set();

// ============================================================
// POTENCIA
// ============================================================

// La barra solo cambia con raton o tactil.
// No permitimos que el teclado modifique su valor.
powerSlider.addEventListener("keydown", (event) => {
  event.preventDefault();
});

powerSlider.addEventListener("keyup", (event) => {
  event.preventDefault();
});

// Si por cualquier motivo recibe foco, se lo quitamos.
powerSlider.addEventListener("focus", () => {
  powerSlider.blur();
});

function renderPower(value) {
  powerValue.textContent = value;
  osdPower.textContent = value;
  throttleTrack.style.setProperty("--level", `${value}%`);
  throttleTrack.setAttribute("aria-valuenow", String(value));
  throttleTrack.setAttribute("aria-valuetext", `${value} por ciento`);
}

powerSlider.addEventListener("input", () => {
  renderPower(powerSlider.value);

  if (activeDirection) {
    sendMove(activeDirection);
  }
});

function currentPower() {
  return Number(powerSlider.value);
}

renderPower(powerSlider.value);

// ============================================================
// MEDIDORES POR RUEDA (datos reales de /api/move)
// ============================================================

function updateMeters(left, right) {
  const leftPercent = Math.round(left * 100);
  const rightPercent = Math.round(right * 100);

  meterLeft.style.setProperty("--fill", `${leftPercent}%`);
  meterRight.style.setProperty("--fill", `${rightPercent}%`);
  meterLeftValue.textContent = leftPercent;
  meterRightValue.textContent = rightPercent;
}

updateMeters(0, 0);

// ============================================================
// MANETA VERTICAL (escritorio)
//
// El slider nativo queda como fuente de verdad; en escritorio se
// conduce con la maneta y en movil con el deslizador horizontal.
// ============================================================

function setupVerticalLever() {
  const isVertical = () =>
    window.matchMedia("(min-width: 561px)").matches;

  let dragging = false;

  function valueFromY(clientY) {
    const rect = throttleTrack.getBoundingClientRect();
    const ratio = 1 - (clientY - rect.top) / rect.height;
    const stepped = Math.round((ratio * 100) / 5) * 5;

    return Math.max(0, Math.min(100, stepped));
  }

  function apply(value) {
    powerSlider.value = value;
    renderPower(value);

    if (activeDirection) {
      sendMove(activeDirection);
    }
  }

  throttleTrack.addEventListener("pointerdown", (event) => {
    if (!isVertical()) {
      return;
    }

    event.preventDefault();
    throttleTrack.setPointerCapture(event.pointerId);
    dragging = true;
    apply(valueFromY(event.clientY));
  });

  throttleTrack.addEventListener("pointermove", (event) => {
    if (dragging && isVertical()) {
      apply(valueFromY(event.clientY));
    }
  });

  function endDrag(event) {
    if (dragging) {
      dragging = false;

      try {
        throttleTrack.releasePointerCapture(event.pointerId);
      } catch (error) {
        // Puede no existir captura en algunos navegadores.
      }
    }
  }

  throttleTrack.addEventListener("pointerup", endDrag);
  throttleTrack.addEventListener("pointercancel", endDrag);
}

setupVerticalLever();

// ============================================================
// CAMARA
//
// Lee el MJPEG con fetch y pinta SIEMPRE el ultimo frame en un
// canvas. Evita el buffering interno del <img>, que es la mayor
// fuente de retardo en un stream MJPEG.
// ============================================================

const cameraContext = camera.getContext("2d", { alpha: false });

const JPEG_SOI = 0xffd8;
const JPEG_EOI = 0xffd9;

let latestFrame = null;
let decoding = false;
let streamActive = true;

function setSignal(available) {
  viewport.classList.toggle("no-signal", !available);
}

function findMarker(buffer, marker, from) {
  const high = marker >> 8;
  const low = marker & 0xff;

  for (let i = from; i + 1 < buffer.length; i += 1) {
    if (buffer[i] === high && buffer[i + 1] === low) {
      return i;
    }
  }

  return -1;
}

function extractFrames(buffer) {
  let offset = 0;

  while (true) {
    const start = findMarker(buffer, JPEG_SOI, offset);

    if (start < 0) {
      offset = Math.max(0, buffer.length - 1);
      break;
    }

    const end = findMarker(buffer, JPEG_EOI, start + 2);

    if (end < 0) {
      offset = start;
      break;
    }

    // Solo conservamos el frame mas reciente de este lote.
    latestFrame = buffer.slice(start, end + 2);
    offset = end + 2;
  }

  drawLatestFrame();

  return buffer.slice(offset);
}

async function drawLatestFrame() {
  if (decoding || latestFrame === null) {
    return;
  }

  decoding = true;
  const frame = latestFrame;
  latestFrame = null;

  try {
    const bitmap = await createImageBitmap(
      new Blob([frame], { type: "image/jpeg" }),
    );

    if (camera.width !== bitmap.width || camera.height !== bitmap.height) {
      camera.width = bitmap.width;
      camera.height = bitmap.height;
    }

    cameraContext.drawImage(bitmap, 0, 0);
    bitmap.close();
    setSignal(true);
  } catch (error) {
    // Frame incompleto o corrupto: se descarta.
  } finally {
    decoding = false;

    if (latestFrame !== null) {
      drawLatestFrame();
    }
  }
}

async function streamLoop() {
  try {
    const response = await fetch("/stream", { cache: "no-store" });

    if (!response.ok || !response.body) {
      throw new Error(`stream ${response.status}`);
    }

    const reader = response.body.getReader();
    let buffer = new Uint8Array(0);

    while (streamActive) {
      const { value, done } = await reader.read();

      if (done) {
        break;
      }

      const merged = new Uint8Array(buffer.length + value.length);
      merged.set(buffer, 0);
      merged.set(value, buffer.length);
      buffer = extractFrames(merged);
    }
  } catch (error) {
    console.error("Stream de camara:", error);
  }

  setSignal(false);

  if (streamActive) {
    setTimeout(streamLoop, 1000);
  }
}

window.addEventListener("pagehide", () => {
  streamActive = false;
});

streamLoop();

// ============================================================
// MODO NOCTURNO
// ============================================================

function applyNightMode(enabled) {
  document.body.classList.toggle("night", enabled);
  nightToggle.setAttribute("aria-pressed", String(enabled));
}

let nightMode = false;

try {
  nightMode = window.localStorage.getItem("carz-night") === "1";
} catch (error) {
  nightMode = false;
}

applyNightMode(nightMode);

nightToggle.addEventListener("click", () => {
  nightMode = !nightMode;
  applyNightMode(nightMode);

  try {
    window.localStorage.setItem("carz-night", nightMode ? "1" : "0");
  } catch (error) {
    // localStorage puede no estar disponible en algunos contextos.
  }
});

// ============================================================
// PETICIONES HTTP
// ============================================================

async function postJSON(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  return response.json();
}

// ============================================================
// MOVIMIENTO
// ============================================================

async function sendMove(direction) {
  try {
    const data = await postJSON("/api/move", {
      direction,
      power: currentPower(),
    });

    updateMeters(
      Number(data.motor1_speed || 0),
      Number(data.motor2_speed || 0),
    );
  } catch (error) {
    console.error("Error enviando movimiento:", error);
    setOffline();
  }
}

async function sendStop() {
  activeDirection = null;
  stopHeartbeat();
  updateMeters(0, 0);

  document.querySelectorAll(".control-button.active").forEach((button) => {
    button.classList.remove("active");
  });

  try {
    await postJSON("/api/stop");
  } catch (error) {
    console.error("Error deteniendo el coche:", error);
    setOffline();
  }
}

// ============================================================
// WATCHDOG / HEARTBEAT
// ============================================================

function startHeartbeat() {
  stopHeartbeat();

  pingTimer = setInterval(async () => {
    if (!activeDirection) {
      return;
    }

    try {
      await postJSON("/api/ping");
    } catch (error) {
      console.error("Error en heartbeat:", error);
      setOffline();
    }
  }, 300);
}

function stopHeartbeat() {
  if (pingTimer !== null) {
    clearInterval(pingTimer);
    pingTimer = null;
  }
}

// ============================================================
// DIRECCION
// ============================================================

function startDirection(direction, button = null) {
  if (!direction) {
    return;
  }

  if (activeDirection === direction) {
    return;
  }

  activeDirection = direction;

  document.querySelectorAll(".control-button.active").forEach((item) => {
    item.classList.remove("active");
  });

  if (button) {
    button.classList.add("active");
  }

  sendMove(direction);
  startHeartbeat();
}

// ============================================================
// BOTONES EN PANTALLA
// ============================================================

controlButtons.forEach((button) => {
  const direction = button.dataset.direction;

  button.addEventListener("pointerdown", (event) => {
    event.preventDefault();

    button.setPointerCapture(event.pointerId);

    startDirection(direction, button);
  });

  button.addEventListener("pointerup", (event) => {
    event.preventDefault();

    try {
      button.releasePointerCapture(event.pointerId);
    } catch (error) {
      // Puede no existir captura en algunos navegadores.
    }

    sendStop();
  });

  button.addEventListener("pointercancel", () => {
    sendStop();
  });

  button.addEventListener("lostpointercapture", () => {
    if (activeDirection === direction) {
      sendStop();
    }
  });
});

stopButton.addEventListener("click", (event) => {
  event.preventDefault();
  pressedKeys.clear();
  sendStop();
});

// ============================================================
// TECLADO
// ============================================================

const keyMap = {
  w: "forward",
  arrowup: "forward",
  s: "backward",
  arrowdown: "backward",
  a: "left",
  arrowleft: "left",
  d: "right",
  arrowright: "right",
};

function normalizeKey(key) {
  return key.toLowerCase();
}

function getDirectionFromKey(key) {
  return keyMap[normalizeKey(key)] || null;
}

function getButtonForDirection(direction) {
  return document.querySelector(`[data-direction="${direction}"]`);
}

document.addEventListener(
  "keydown",
  (event) => {
    const key = normalizeKey(event.key);
    const direction = getDirectionFromKey(key);

    if (!direction) {
      return;
    }

    // Bloquea por completo el comportamiento nativo de las flechas.
    // Asi no cambian el slider ni hacen scroll.
    event.preventDefault();
    event.stopPropagation();

    if (document.activeElement === powerSlider) {
      powerSlider.blur();
    }

    if (event.repeat) {
      return;
    }

    if (pressedKeys.has(key)) {
      return;
    }

    pressedKeys.add(key);

    const button = getButtonForDirection(direction);

    startDirection(direction, button);
  },
  { passive: false },
);

document.addEventListener(
  "keyup",
  (event) => {
    const key = normalizeKey(event.key);
    const direction = getDirectionFromKey(key);

    if (!direction) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    pressedKeys.delete(key);

    if (pressedKeys.size > 0) {
      const remainingKeys = Array.from(pressedKeys);
      const lastKey = remainingKeys[remainingKeys.length - 1];
      const remainingDirection = getDirectionFromKey(lastKey);

      if (remainingDirection) {
        const button = getButtonForDirection(remainingDirection);

        startDirection(remainingDirection, button);

        return;
      }
    }

    sendStop();
  },
  { passive: false },
);

// ============================================================
// SEGURIDAD
// ============================================================

window.addEventListener("blur", () => {
  pressedKeys.clear();
  sendStop();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    pressedKeys.clear();
    sendStop();
  }
});

window.addEventListener("beforeunload", () => {
  navigator.sendBeacon("/api/stop");
});

// ============================================================
// ESTADO DEL SERVIDOR
// ============================================================

function setOnline() {
  statusDot.classList.remove("offline");
  statusDot.classList.add("online");
  statusText.textContent = "Conectado";
}

function setOffline() {
  statusDot.classList.remove("online");
  statusDot.classList.add("offline");
  statusText.textContent = "Sin conexion";
}

async function checkHealth() {
  try {
    const response = await fetch("/api/health", {
      cache: "no-store",
    });

    if (!response.ok) {
      throw new Error(`Health check HTTP ${response.status}`);
    }

    setOnline();
  } catch (error) {
    console.error("Health check:", error);
    setOffline();
  }
}

checkHealth();

setInterval(checkHealth, 3000);
