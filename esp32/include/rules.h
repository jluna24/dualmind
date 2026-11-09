// DualMind - reglas deterministas (mente "edge").
// Lógica pura, sin dependencias de Arduino: recibe el resumen del minuto y
// la hora local, y devuelve la decisión con el contrato común (comfort,
// mold_risk, action, reason). Umbrales para una recámara en Ensenada, B.C.
// (ver docs/fase-2-reglas-vs-llm.md, sección 6).
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace rules {

constexpr int RULES_V = 1;

// --- Umbrales v1 -----------------------------------------------------------
constexpr int NIGHT_START_H = 23;  // noche: 23:00-06:59 hora local
constexpr int NIGHT_END_H = 7;

constexpr float HOT_DAY_C = 26.0f, HOT_NIGHT_C = 24.0f;    // caluroso si T >
constexpr float COLD_DAY_C = 20.0f, COLD_NIGHT_C = 18.0f;  // frio si T <
constexpr float HUMID_PCT = 60.0f;                         // humedo si HR >
constexpr float DRY_PCT = 30.0f;                           // seco si HR <

constexpr float MOLD_ON_PCT = 70.0f;   // el contador sube con HR >=
constexpr float MOLD_OFF_PCT = 65.0f;  // y se reinicia con HR < (histéresis)
constexpr uint32_t MOLD_MEDIUM_MIN = 120;  // >= 2 h -> medio
constexpr uint32_t MOLD_HIGH_MIN = 360;    // >= 6 h -> alto

// --- Estado y resultado ------------------------------------------------------
// Minutos consecutivos con HR alta. history_reset = el contador se perdió en
// un reinicio y puede estar subestimado; se limpia cuando HR baja de
// MOLD_OFF_PCT (a partir de ahí el conteo vuelve a ser exacto).
struct MoldState {
  uint32_t humid_min = 0;
  bool history_reset = false;
};

struct Decision {
  const char *comfort;    // ok | frio | caluroso | seco | humedo
  const char *mold_risk;  // bajo | medio | alto
  const char *action;     // ninguna | ventilar | ventilador | deshumidificar | humidificar | calentar
  const char *period;     // dia | noche
  char reason[121];
  float dew_point_c;
  uint32_t humid_min;
  bool history_reset;
};

bool isNight(int localHour);
float dewPointC(float tempC, float humPct);

// Actualiza el contador de moho con `minutes` minutos de lectura `humPct`.
void updateMold(MoldState &st, float humPct, uint32_t minutes = 1);

// Evalúa sin modificar el estado (llamar después de updateMold).
Decision evaluate(float tempC, float humPct, int localHour, const MoldState &st);

}  // namespace rules
