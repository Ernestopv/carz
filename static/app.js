const powerSlider = document.getElementById("power");
const powerValue = document.getElementById("powerValue");
const stopButton = document.getElementById("stopButton");
const controlButtons = document.querySelectorAll("[data-direction]");
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");

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

powerSlider.addEventListener("input", () => {
  powerValue.textContent = powerSlider.value;

  if (activeDirection) {
    sendMove(activeDirection);
  }
});

function currentPower() {
  return Number(powerSlider.value);
}

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
    await postJSON("/api/move", {
      direction,
      power: currentPower(),
    });
  } catch (error) {
    console.error("Error enviando movimiento:", error);
    setOffline();
  }
}

async function sendStop() {
  activeDirection = null;
  stopHeartbeat();

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
