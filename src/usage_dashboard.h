#pragma once

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <math.h>

namespace usage {

constexpr int16_t UNAVAILABLE = 32767;
constexpr uint32_t CACHE_SECONDS = 15 * 60;
constexpr uint32_t FRESH_SECONDS = 30;
enum class Source { Unavailable, Fresh, Cached };
enum class Forecast { Unavailable, Learning, Ready };
enum class Warning { None, NoLink, NoUpdate };

struct Snapshot {
  bool available = false;
  uint8_t used = 0;
  uint32_t resetsAt = 0;
  uint32_t observedAt = 0;
  uint32_t validUntil = 0;
  Source source = Source::Unavailable;
  int16_t remaining48h = UNAVAILABLE;
  int16_t remaining14d = UNAVAILABLE;
  uint32_t forecastValidUntil = 0;
  Forecast forecast = Forecast::Unavailable;
  bool giftKnown = false;
  uint32_t giftExpiresAt = 0;
  uint32_t giftObservedAt = 0;
};

struct View {
  bool linked = false;
  bool usable = false;
  Warning warning = Warning::None;
  uint8_t left = 0;
  uint32_t now = 0;
  uint32_t observedAt = 0;
  uint32_t resetSeconds = 0;
  uint32_t giftSeconds = 0;
  bool giftAvailable = false;
  int16_t remaining48h = UNAVAILABLE;
  int16_t remaining14d = UNAVAILABLE;
  bool learning = false;
};

inline bool recent(uint32_t observed, uint32_t now, uint32_t ttl) {
  return observed != 0 && observed <= now && now - observed < ttl;
}

// Availability belongs to the observation, not to the transport heartbeat.
inline View derive(const Snapshot& s, uint32_t now, bool linked) {
  View v;
  v.linked = linked;
  v.now = now;
  v.observedAt = s.observedAt <= now ? s.observedAt : 0;
  v.usable = s.available && s.used <= 100 && s.source != Source::Unavailable
      && recent(s.observedAt, now, CACHE_SECONDS)
      && now < s.validUntil && now < s.resetsAt;
  if (!v.usable) return v;
  v.left = 100 - s.used;
  v.resetSeconds = s.resetsAt - now;
  v.warning = !linked ? Warning::NoLink
      : s.source != Source::Fresh || !recent(s.observedAt, now, FRESH_SECONDS)
          ? Warning::NoUpdate : Warning::None;
  v.giftAvailable = s.giftKnown && recent(s.giftObservedAt, now, CACHE_SECONDS)
      && s.giftExpiresAt > now;
  if (v.giftAvailable) v.giftSeconds = s.giftExpiresAt - now;
  if (now < s.forecastValidUntil && s.forecast == Forecast::Ready) {
    v.remaining48h = s.remaining48h;
    v.remaining14d = s.remaining14d;
  }
  v.learning = s.forecast == Forecast::Learning;
  return v;
}

inline bool markerValid(int value) { return value >= -5100 && value <= 5100; }

// Signed log1p: negative on the left, positive on the right, linear near zero.
inline int forecastPosition(int x, int width, int remainingBp) {
  const int left = x + 3, right = x + width - 4;
  const int magnitude = remainingBp < 0 ? -remainingBp : remainingBp;
  const float pp = (magnitude > 5000 ? 5000 : magnitude) / 100.0f;
  const float offset = log1pf(pp) / log1pf(50.0f) * (right - left) / 2.0f;
  return lroundf((left + right) / 2.0f + (remainingBp < 0 ? -offset : offset));
}

inline void durationText(uint32_t seconds, char* out, size_t size) {
  if (seconds >= 86400) snprintf(out, size, "%lud %02luh",
      (unsigned long)(seconds / 86400), (unsigned long)((seconds / 3600) % 24));
  else if (seconds >= 3600) snprintf(out, size, "%luh %02lum",
      (unsigned long)(seconds / 3600), (unsigned long)((seconds / 60) % 60));
  else if (seconds >= 60) snprintf(out, size, "%lum", (unsigned long)(seconds / 60));
  else snprintf(out, size, "<1m");
}

inline void ageText(const View& v, char* out, size_t size) {
  if (!v.observedAt || v.observedAt > v.now) { snprintf(out, size, "--"); return; }
  const uint32_t age = v.now - v.observedAt;
  if (age < 60) snprintf(out, size, "<1m AGO");
  else if (age < 3600) snprintf(out, size, "%lum AGO", (unsigned long)(age / 60));
  else if (age < 86400) snprintf(out, size, "%luh AGO", (unsigned long)(age / 3600));
  else snprintf(out, size, "%lud AGO", (unsigned long)(age / 86400));
}

} // namespace usage
