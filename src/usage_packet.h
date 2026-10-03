#pragma once
#include <ArduinoJson.h>
#include <string.h>
#include "usage_dashboard.h"

namespace usage {

inline uint32_t timestamp(JsonVariantConst value) {
  return value.is<uint32_t>() ? value.as<uint32_t>() : 0;
}

inline int16_t remainder(JsonVariantConst value) {
  return value.is<int>() && markerValid(value.as<int>()) ? value.as<int>() : UNAVAILABLE;
}

// Every quota packet is a complete snapshot; missing fields never imply zero.
inline Snapshot parseSnapshot(JsonVariantConst doc) {
  Snapshot s;
  const auto used = doc["secondary"];
  s.resetsAt = timestamp(doc["secondary_resets_at"]);
  s.available = used.is<int>() && used.as<int>() >= 0 && used.as<int>() <= 100 && s.resetsAt > 0;
  if (s.available) s.used = used.as<int>();
  s.observedAt = timestamp(doc["quota_observed_at"]);
  s.validUntil = timestamp(doc["quota_valid_until"]);
  const char* source = doc["quota_status"] | "";
  if (!strcmp(source, "fresh")) s.source = Source::Fresh;
  else if (!strcmp(source, "cached")) s.source = Source::Cached;
  s.remaining48h = remainder(doc["secondary_remaining_48h_bp"]);
  s.remaining14d = remainder(doc["secondary_remaining_14d_bp"]);
  s.forecastValidUntil = timestamp(doc["secondary_forecast_valid_until"]);
  const char* forecast = doc["secondary_forecast_status"] | "";
  if (!strcmp(forecast, "ready")) s.forecast = Forecast::Ready;
  else if (!strcmp(forecast, "learning")) s.forecast = Forecast::Learning;
  s.giftKnown = doc["gift_reset_expires_at"].is<uint32_t>();
  s.giftExpiresAt = timestamp(doc["gift_reset_expires_at"]);
  s.giftObservedAt = timestamp(doc["gift_observed_at"]);
  return s;
}

} // namespace usage
