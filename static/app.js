const powerSlider = document.getElementById("power");
const powerValue = document.getElementById("powerValue");
const stopButton = document.getElementById("stopButton");
const controlButtons = document.querySelectorAll("[data-direction]");
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");

let activeDirection = null;
let pingTimer = null;

powerSlider.addEventListener("input", () => {
    powerValue.textContent = powerSlider.value;

    if (activeDirection) {
        sendMove(activeDirection);
    }
});

function currentPower() {
    return Number(powerSlider.value);
}

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

async function sendMove(direction) {
    try {
        await postJSON("/api/move", {
            direction,
            power: currentPower(),
        });
    } catch (error) {
        console.error(error);
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
        console.error(error);
        setOffline();
    }
}

function startHeartbeat() {
    stopHeartbeat();

    pingTimer = setInterval(async () => {
        if (!activeDirection) {
            return;
        }

        try {
            await postJSON("/api/ping");
        } catch (error) {
            console.error(error);
            setOffline();
        }
    }, 300);
}

function stopHeartbeat() {
    if (pingTimer) {
        clearInterval(pingTimer);
        pingTimer = null;
    }
}

function startDirection(direction, button = null) {
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

controlButtons.forEach((button) => {
    const direction = button.dataset.direction;

    button.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        button.setPointerCapture(event.pointerId);
        startDirection(direction, button);
    });

    button.addEventListener("pointerup", (event) => {
        event.preventDefault();
        sendStop();
    });

    button.addEventListener("pointercancel", sendStop);
    button.addEventListener("lostpointercapture", sendStop);
});

stopButton.addEventListener("click", sendStop);

const keyMap = {
    w: "forward",
    ArrowUp: "forward",
    s: "backward",
    ArrowDown: "backward",
    a: "left",
    ArrowLeft: "left",
    d: "right",
    ArrowRight: "right",
};

document.addEventListener("keydown", (event) => {
    if (event.repeat) {
        return;
    }

    const direction = keyMap[event.key];

    if (!direction) {
        return;
    }

    event.preventDefault();

    const button = document.querySelector(
        `[data-direction="${direction}"]`
    );

    startDirection(direction, button);
});

document.addEventListener("keyup", (event) => {
    if (!keyMap[event.key]) {
        return;
    }

    event.preventDefault();
    sendStop();
});

window.addEventListener("blur", sendStop);
window.addEventListener("beforeunload", () => {
    navigator.sendBeacon("/api/stop");
});

function setOnline() {
    statusDot.classList.remove("offline");
    statusDot.classList.add("online");
    statusText.textContent = "Conectado";
}

function setOffline() {
    statusDot.classList.remove("online");
    statusDot.classList.add("offline");
    statusText.textContent = "Sin conexión";
}

async function checkHealth() {
    try {
        const response = await fetch("/api/health", {
            cache: "no-store",
        });

        if (!response.ok) {
            throw new Error("Health check failed");
        }

        setOnline();
    } catch (error) {
        setOffline();
    }
}

checkHealth();
setInterval(checkHealth, 3000);
