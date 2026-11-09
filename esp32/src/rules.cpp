#include "rules.h"

#include <math.h>
#include <stdio.h>

namespace rules {

bool isNight(int h) { return h >= NIGHT_START_H || h < NIGHT_END_H; }

// Fórmula de Magnus (Sonntag 1990): válida de -45 a 60 °C.
float dewPointC(float t, float hr) {
  if (hr <= 0) return NAN;
  float g = logf(hr / 100.0f) + (17.62f * t) / (243.12f + t);
  return 243.12f * g / (17.62f - g);
}

void updateMold(MoldState &st, float hr, uint32_t minutes) {
  if (hr >= MOLD_ON_PCT) {
    st.humid_min += minutes;
  } else if (hr < MOLD_OFF_PCT) {
    st.humid_min = 0;
    st.history_reset = false;
  }
  // entre 65 y 70 % el contador se mantiene
}

Decision evaluate(float t, float hr, int localHour, const MoldState &st) {
  Decision d{};
  const bool night = isNight(localHour);
  const float hot = night ? HOT_NIGHT_C : HOT_DAY_C;
  const float cold = night ? COLD_NIGHT_C : COLD_DAY_C;
  d.period = night ? "noche" : "dia";

  // Confort: primera que aplique
  char why[48];
  if (t > hot) {
    d.comfort = "caluroso";
    snprintf(why, sizeof why, "T %.1f>%.0f", t, hot);
  } else if (t < cold) {
    d.comfort = "frio";
    snprintf(why, sizeof why, "T %.1f<%.0f", t, cold);
  } else if (hr > HUMID_PCT) {
    d.comfort = "humedo";
    snprintf(why, sizeof why, "HR %.0f>%.0f", hr, HUMID_PCT);
  } else if (hr < DRY_PCT) {
    d.comfort = "seco";
    snprintf(why, sizeof why, "HR %.0f<%.0f", hr, DRY_PCT);
  } else {
    d.comfort = "ok";
    snprintf(why, sizeof why, "T y HR en rango");
  }

  // Riesgo de moho por persistencia
  if (st.humid_min >= MOLD_HIGH_MIN) {
    d.mold_risk = "alto";
  } else if (st.humid_min >= MOLD_MEDIUM_MIN) {
    d.mold_risk = "medio";
  } else {
    d.mold_risk = "bajo";
  }

  // Acción: primera que aplique
  const bool moldHigh = st.humid_min >= MOLD_HIGH_MIN;
  const bool moldMed = !moldHigh && st.humid_min >= MOLD_MEDIUM_MIN;
  const char *c = d.comfort;
  if (moldHigh) {
    d.action = "deshumidificar";
  } else if (moldMed || c[0] == 'h') {  // humedo
    d.action = "ventilar";
  } else if (c[0] == 'c') {  // caluroso
    d.action = "ventilador";
  } else if (c[0] == 'f') {  // frio
    d.action = "calentar";
  } else if (c[0] == 's') {  // seco
    d.action = "humidificar";
  } else {
    d.action = "ninguna";
  }

  snprintf(d.reason, sizeof d.reason, "%s: %s; HR>=%.0f%% por %lu min%s",
           d.period, why, MOLD_ON_PCT, (unsigned long)st.humid_min,
           st.history_reset ? " (tras reinicio)" : "");

  d.dew_point_c = dewPointC(t, hr);
  d.humid_min = st.humid_min;
  d.history_reset = st.history_reset;
  return d;
}

}  // namespace rules
