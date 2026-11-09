// DualMind - ESP32
// Wi-Fi + hora NTP + lecturas del DHT22 resumidas cada minuto con el
// contrato de mensaje v1, publicadas en AWS IoT Core por MQTT/TLS (8883).
// Fase 2: cada resumen incluye la decisión de las reglas (campo "rules").

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <sys/time.h>
#include <time.h>

#include "secrets.h"
#include "aws_certs.h"  // generado con tools/gen_aws_certs.py
#include "rules.h"

// --- Configuración --------------------------------------------------------
static const char *DEVICE_ID = "esp32-01";
static const char *MQTT_CLIENT_ID = "dualmind-esp32-01";  // = nombre del thing
static const uint16_t MQTT_PORT = 8883;
static const int LED_PIN = 2;               // LED integrado del DevKit
static const int DHT_PIN = 4;               // DATA del DHT22 (VCC a 3V3)
static const uint8_t DHT_TYPE = DHT22;      // AM2302; cambiar a DHT11 si fuera el azul
static const uint32_t SAMPLE_MS = 2500;     // el DHT22 admite 1 lectura cada 2 s
static const uint32_t WINDOW_S = 60;        // resumir y publicar cada 60 s
static const uint32_t WIFI_TIMEOUT_MS = 20000;
// Si el DHT22 no da ninguna lectura válida en N ventanas seguidas, reiniciar
// el ESP32 (un reinicio recuperó al sensor el 2026-11-08).
static const uint32_t SENSOR_FAIL_RESTART_WINDOWS = 3;
// Hora local de Ensenada, B.C. (= America/Tijuana, con horario de verano de EE. UU.)
static const char *LOCAL_TZ = "PST8PDT,M3.2.0,M11.1.0";

// Contador de moho: minutos que suma cada ventana con HR alta. Para probar
// sin esperar horas: build_flags = -DMOLD_STEP_MIN=30 (1 ventana = 30 min).
#ifndef MOLD_STEP_MIN
#define MOLD_STEP_MIN 1
#endif
static const uint32_t MOLD_PERSIST_S = 600;  // guardar en NVS cada 10 min
static const uint32_t MOLD_RESTORE_MAX_S = 900;  // restaurar si tiene < 15 min

// --- Estadística por ventana ---------------------------------------------
struct Stat {
  float sum = 0, mn = INFINITY, mx = -INFINITY;
  uint32_t n = 0;
  void add(float v) {
    sum += v;
    if (v < mn) mn = v;
    if (v > mx) mx = v;
    n++;
  }
  void reset() { *this = Stat(); }
  float avg() const { return n ? sum / n : NAN; }
};

static Stat tempStat, humStat;
static uint32_t readErrors = 0;  // lecturas fallidas en la ventana actual
static uint32_t emptyWindows = 0;  // ventanas seguidas sin lecturas válidas
static DHT dht(DHT_PIN, DHT_TYPE);
static uint32_t bootCount = 0;
static uint32_t seq = 0;
static uint32_t lastSampleMs = 0;
static uint32_t windowStartMs = 0;
static uint32_t lastMqttAttemptMs = 0;
static uint32_t mqttRetryMs = 2000;  // backoff: 2 s ... 60 s

// Estado de las reglas (contador de moho persistido en NVS)
static rules::MoldState mold;
static bool moldRestored = false;    // se decide con la primera hora NTP válida
static uint32_t nvsHumidMin = 0;     // valores leídos de NVS al arrancar
static bool nvsHistoryReset = false;
static uint32_t nvsSavedAt = 0;      // epoch (s) del último guardado
static time_t lastPersistS = 0;

static WiFiClientSecure tlsClient;
static PubSubClient mqtt(tlsClient);

// --- Sensor DHT22 ---------------------------------------------------------
// Devuelve false si la lectura falló (cable suelto, sensor ocupado, etc.).
static bool sampleSensor() {
  float h = dht.readHumidity();
  float t = dht.readTemperature();  // °C
  if (isnan(h) || isnan(t)) {
    readErrors++;
    return false;
  }
  tempStat.add(t);
  humStat.add(h);
  return true;
}

// --- Utilidades -----------------------------------------------------------
static bool wifiConfigured() {
  return strcmp(WIFI_SSID, "CAMBIA_ESTO") != 0;
}

static bool timeSynced() {
  return time(nullptr) > 1700000000;  // posterior a nov-2023
}

static uint64_t nowMs() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (uint64_t)tv.tv_sec * 1000ULL + tv.tv_usec / 1000;
}

static double round2(float v) { return roundf(v * 100.0f) / 100.0; }

static void connectWiFi() {
  if (!wifiConfigured()) {
    Serial.println("[wifi] Sin configurar: llena include/secrets.h");
    return;
  }
  Serial.printf("[wifi] Conectando a \"%s\"...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    delay(250);
  }
  digitalWrite(LED_PIN, LOW);

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] Conectado. IP %s, RSSI %d dBm\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
    configTzTime(LOCAL_TZ, "pool.ntp.org", "time.google.com");  // ts sigue en UTC
  } else {
    Serial.println("[wifi] No se pudo conectar (se reintenta solo)");
  }
}

static void setupMqtt() {
  tlsClient.setCACert(AWS_ROOT_CA);
  tlsClient.setCertificate(AWS_DEVICE_CERT);
  tlsClient.setPrivateKey(AWS_PRIVATE_KEY);
  mqtt.setServer(AWS_IOT_ENDPOINT, MQTT_PORT);
  mqtt.setBufferSize(1024);
  mqtt.setKeepAlive(60);
}

static void ensureMqtt() {
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED) return;
  uint32_t now = millis();
  if (now - lastMqttAttemptMs < mqttRetryMs) return;
  lastMqttAttemptMs = now;

  Serial.printf("[mqtt] Conectando a %s:%u ...\n", AWS_IOT_ENDPOINT, MQTT_PORT);
  if (mqtt.connect(MQTT_CLIENT_ID)) {
    Serial.println("[mqtt] Conectado a AWS IoT Core");
    mqttRetryMs = 2000;
  } else {
    Serial.printf("[mqtt] Falló (estado %d). Reintento en %lu s\n",
                  mqtt.state(), (unsigned long)(mqttRetryMs / 1000));
    mqttRetryMs = min<uint32_t>(mqttRetryMs * 2, 60000);
  }
}

// --- Reglas: persistencia del contador de moho ------------------------------
static void persistMold(time_t now) {
  Preferences prefs;
  prefs.begin("dualmind", false);
  prefs.putUInt("humid_min", mold.humid_min);
  prefs.putBool("hist_reset", mold.history_reset);
  prefs.putUInt("mold_at", (uint32_t)now);
  prefs.end();
  lastPersistS = now;
}

// Primera vez con hora válida: restaura el contador si el guardado es reciente.
static void restoreMold(time_t now) {
  moldRestored = true;
  if (nvsSavedAt > 0 && now >= (time_t)nvsSavedAt &&
      now - (time_t)nvsSavedAt < (time_t)MOLD_RESTORE_MAX_S) {
    mold.humid_min = nvsHumidMin;
    mold.history_reset = nvsHistoryReset;
    Serial.printf("[rules] Contador restaurado: %lu min (guardado hace %ld s)\n",
                  (unsigned long)mold.humid_min, (long)(now - nvsSavedAt));
  } else {
    mold.humid_min = 0;
    mold.history_reset = true;
    Serial.println("[rules] Sin guardado reciente: contador en 0 (history_reset)");
  }
  lastPersistS = now;
}

static void addRules(JsonDocument &doc, float t, float h) {
  time_t now = time(nullptr);
  if (!moldRestored) restoreMold(now);

  rules::updateMold(mold, h, MOLD_STEP_MIN);
  struct tm local;
  localtime_r(&now, &local);
  rules::Decision d = rules::evaluate(t, h, local.tm_hour, mold);

  if (now - lastPersistS >= (time_t)MOLD_PERSIST_S) persistMold(now);

  JsonObject r = doc["rules"].to<JsonObject>();
  r["rules_v"] = rules::RULES_V;
  r["comfort"] = d.comfort;
  r["mold_risk"] = d.mold_risk;
  r["action"] = d.action;
  r["reason"] = d.reason;
  r["period"] = d.period;
  r["local_hour"] = local.tm_hour;
  if (!isnan(d.dew_point_c)) r["dew_point_c"] = round2(d.dew_point_c);
  r["humid_min"] = d.humid_min;
  r["history_reset"] = d.history_reset;
}

static void publishWindow() {
  if (tempStat.n == 0) {
    Serial.printf("[sensor] Sin lecturas válidas en la ventana (%lu errores). "
                  "Revisa el cableado del DHT22 (GPIO %d).\n",
                  (unsigned long)readErrors, DHT_PIN);
    if (++emptyWindows >= SENSOR_FAIL_RESTART_WINDOWS) {
      Serial.printf("[sensor] %lu ventanas seguidas sin lecturas: reiniciando
",
                    (unsigned long)emptyWindows);
      if (moldRestored && timeSynced()) persistMold(time(nullptr));
      mqtt.disconnect();
      delay(200);
      ESP.restart();
    }
    return;
  }
  emptyWindows = 0;

  if (!timeSynced()) {
    Serial.printf("[telemetry] Sin hora NTP todavía; ventana descartada "
                  "(temp avg %.2f, hum avg %.2f)\n",
                  tempStat.avg(), humStat.avg());
    return;
  }

  char msgId[48];
  snprintf(msgId, sizeof msgId, "%s-%lu-%06lu", DEVICE_ID,
           (unsigned long)bootCount, (unsigned long)seq++);

  JsonDocument doc;
  doc["v"] = 1;
  doc["device_id"] = DEVICE_ID;
  doc["msg_id"] = msgId;
  doc["ts"] = nowMs();
  doc["type"] = "telemetry";
  doc["window_s"] = WINDOW_S;

  JsonObject readings = doc["readings"].to<JsonObject>();
  JsonObject t = readings["temp_c"].to<JsonObject>();
  t["avg"] = round2(tempStat.avg());
  t["min"] = round2(tempStat.mn);
  t["max"] = round2(tempStat.mx);
  JsonObject h = readings["hum_pct"].to<JsonObject>();
  h["avg"] = round2(humStat.avg());
  h["min"] = round2(humStat.mn);
  h["max"] = round2(humStat.mx);

  // Calidad de la ventana: lecturas válidas y fallidas (campo opcional en v1)
  JsonObject q = doc["samples"].to<JsonObject>();
  q["ok"] = tempStat.n;
  q["err"] = readErrors;

  // Mente determinista: misma entrada que verá el LLM (avg del minuto)
  addRules(doc, tempStat.avg(), humStat.avg());

  char topic[64];
  snprintf(topic, sizeof topic, "dualmind/%s/telemetry", DEVICE_ID);

  char payload[768];
  size_t len = serializeJson(doc, payload, sizeof payload);

  bool sent = mqtt.connected() &&
              mqtt.publish(topic, (const uint8_t *)payload, len, false);
  Serial.printf("[telemetry] %s %s %s\n", sent ? "ENVIADO" : "NO ENVIADO",
                topic, payload);
  if (!sent) return;

  // parpadeo corto = mensaje enviado
  digitalWrite(LED_PIN, HIGH);
  delay(50);
  digitalWrite(LED_PIN, LOW);
}

// --- Arduino --------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(LED_PIN, OUTPUT);
  dht.begin();

  Preferences prefs;
  prefs.begin("dualmind", false);
  bootCount = prefs.getUInt("boot", 0) + 1;
  prefs.putUInt("boot", bootCount);
  nvsHumidMin = prefs.getUInt("humid_min", 0);
  nvsHistoryReset = prefs.getBool("hist_reset", true);
  nvsSavedAt = prefs.getUInt("mold_at", 0);
  prefs.end();

  Serial.printf("\n=== DualMind %s | arranque #%lu ===\n", DEVICE_ID,
                (unsigned long)bootCount);

  connectWiFi();

  // Lectura de prueba al arrancar, para diagnosticar el sensor de inmediato
  delay(2000);
  float t0 = dht.readTemperature(), h0 = dht.readHumidity();
  if (isnan(t0) || isnan(h0)) {
    Serial.printf("[sensor] DHT en GPIO %d: SIN RESPUESTA\n", DHT_PIN);
  } else {
    Serial.printf("[sensor] DHT en GPIO %d: %.1f °C, %.1f %%\n", DHT_PIN, t0, h0);
  }

  setupMqtt();
  windowStartMs = millis();
}

void loop() {
  ensureMqtt();
  mqtt.loop();

  uint32_t now = millis();

  if (now - lastSampleMs >= SAMPLE_MS) {
    lastSampleMs = now;
    sampleSensor();
  }

  if (now - windowStartMs >= WINDOW_S * 1000UL) {
    windowStartMs = now;
    publishWindow();
    tempStat.reset();
    humStat.reset();
    readErrors = 0;
  }

  delay(10);
}
