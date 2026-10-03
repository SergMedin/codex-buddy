"""Host checks of the production quota parser, view model, and display renderer.

The real headers and packet handler are compiled against ArduinoJson. Hardware
primitives are stubbed; the canvas records pixels and measured text boxes at the
hardware's actual sizes so layout failures are visible without a connected StickS3.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
ARDUINO_JSON = ROOT / ".pio/libdeps/m5stack-sticks3/ArduinoJson/src"

SOURCE = r'''
#include <ArduinoJson.h>
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>
#include "usage_dashboard.h"
#include "usage_packet.h"
constexpr int TL_DATUM = 0;
#include "usage_display.h"

#define REQUIRE(condition) do { if (!(condition)) { \
  std::cerr << __LINE__ << ": " << #condition << "\n"; std::exit(1); \
} } while (false)

constexpr uint32_t NOW = 1800000000;
constexpr int UNTOUCHED = -1;
constexpr uint16_t BACKGROUND = 0;

usage::Snapshot snapshot() {
  usage::Snapshot s;
  s.available = true;
  s.used = 49;
  s.resetsAt = NOW + 4 * 86400;
  s.observedAt = NOW;
  s.validUntil = NOW + 900;
  s.source = usage::Source::Fresh;
  s.remaining48h = -125;
  s.remaining14d = 3600;
  s.forecastValidUntil = NOW + 900;
  s.forecast = usage::Forecast::Ready;
  s.giftKnown = true;
  s.giftExpiresAt = NOW + 5 * 86400;
  s.giftObservedAt = NOW;
  return s;
}

usage::Snapshot parse(const std::string& json) {
  JsonDocument doc;
  REQUIRE(!deserializeJson(doc, json));
  return usage::parseSnapshot(doc.as<JsonVariantConst>());
}

struct Text {
  std::string value;
  int x, y, width, height, size, color;
};
struct Rect { int x, y, width, height, color; };
struct Box {
  int left = 1000, top = 1000, right = -1, bottom = -1;
  bool empty() const { return right < left; }
  int width() const { return empty() ? 0 : right - left + 1; }
  int height() const { return empty() ? 0 : bottom - top + 1; }
};
struct Canvas {
  int width, height, size = 1, color = 0;
  std::vector<int> pixels;
  std::vector<Text> texts;
  std::vector<Rect> rects;
  explicit Canvas(bool wide): width(wide ? 240 : 135), height(wide ? 135 : 240),
      pixels(width * height, UNTOUCHED) {}
  void pixel(int x, int y, int value) {
    REQUIRE(x >= 0 && x < width && y >= 0 && y < height);
    pixels[y * width + x] = value;
  }
  int pixel(int x, int y) const { return pixels.at(y * width + x); }
  void fillRect(int x, int y, int w, int h, int value) {
    REQUIRE(w > 0 && h > 0);
    REQUIRE(x >= 0 && y >= 0 && x + w <= width && y + h <= height);
    rects.push_back({x, y, w, h, value});
    for (int yy = y; yy < y + h; ++yy)
      for (int xx = x; xx < x + w; ++xx) pixel(xx, yy, value);
  }
  void drawLine(int x, int y, int endX, int endY, int value) {
    const int dx = abs(endX - x), sx = x < endX ? 1 : -1;
    const int dy = -abs(endY - y), sy = y < endY ? 1 : -1;
    int error = dx + dy;
    while (true) {
      pixel(x, y, value);
      if (x == endX && y == endY) break;
      const int twice = 2 * error;
      if (twice >= dy) { error += dy; x += sx; }
      if (twice <= dx) { error += dx; y += sy; }
    }
  }
  void setTextSize(int value) { size = value; }
  void setTextColor(int value, int) { color = value; }
  void setTextDatum(int value) { REQUIRE(value == TL_DATUM); }
  int textWidth(const char* text) const { return static_cast<int>(strlen(text)) * 6 * size; }
  void drawString(const char* value, int x, int y) {
    texts.push_back({value, x, y, textWidth(value), 8 * size, size, color});
  }
  const Text& text(const std::string& value) const {
    for (const auto& t : texts) if (t.value == value) return t;
    std::cerr << "Missing text: " << value << "\n";
    std::exit(1);
  }
  bool hasText(const std::string& value) const {
    return std::any_of(texts.begin(), texts.end(), [&](const Text& t) { return t.value == value; });
  }
  Box colorBounds(int value) const {
    Box box;
    for (int y = 0; y < height; ++y) for (int x = 0; x < width; ++x) {
      if (pixel(x, y) != value) continue;
      box.left = std::min(box.left, x); box.right = std::max(box.right, x);
      box.top = std::min(box.top, y); box.bottom = std::max(box.bottom, y);
    }
    return box;
  }
  void assertLayout() const {
    for (const auto& t : texts) {
      if (t.x < 0 || t.y < 0 || t.x + t.width > width || t.y + t.height > height) {
        std::cerr << "Text outside " << width << "x" << height << ": " << t.value << "\n";
        std::exit(1);
      }
    }
    for (size_t i = 0; i < texts.size(); ++i) for (size_t j = i + 1; j < texts.size(); ++j) {
      const auto& a = texts[i]; const auto& b = texts[j];
      if (a.x < b.x + b.width && a.x + a.width > b.x &&
          a.y < b.y + b.height && a.y + a.height > b.y) {
        std::cerr << "Overlapping text in " << width << "x" << height
                  << ": " << a.value << " / " << b.value << "\n";
        std::exit(1);
      }
    }
    // A dashboard repaint must never erase an unchanged animation frame.
    const int petWidth = width == 240 ? 104 : 135;
    const int petHeight = width == 240 ? 92 : 120;
    for (int y = 0; y < petHeight; ++y) for (int x = 0; x < petWidth; ++x)
      REQUIRE(pixel(x, y) == UNTOUCHED);
  }
};

Canvas render(const usage::View& view, bool wide) {
  Canvas canvas(wide);
  usage::Display<Canvas> display(canvas, BACKGROUND);
  if (wide) display.landscape(view); else display.portrait(view);
  return canvas;
}

void parserChecks() {
  const auto empty = parse("{}");
  REQUIRE(!empty.available && !empty.giftKnown);
  REQUIRE(empty.remaining48h == usage::UNAVAILABLE && empty.remaining14d == usage::UNAVAILABLE);
  REQUIRE(!usage::derive(empty, NOW, true).usable);
  auto good = parse(R"({"secondary":49,"secondary_resets_at":1800345600,
    "quota_observed_at":1800000000,"quota_valid_until":1800000900,"quota_status":"fresh",
    "secondary_remaining_48h_bp":-125,"secondary_remaining_14d_bp":3600,
    "secondary_forecast_valid_until":1800000900,"secondary_forecast_status":"ready",
    "gift_reset_expires_at":1800432000,"gift_observed_at":1800000000})");
  auto view = usage::derive(good, NOW, true);
  REQUIRE(view.usable && view.left == 51 && view.warning == usage::Warning::None);
  REQUIRE(view.remaining48h == -125 && view.remaining14d == 3600 && view.giftAvailable);

  // JSON types cannot masquerade as percentages, timestamps, or forecasts.
  const std::vector<std::string> invalid = {"null", "true", "false", "\"85\"", "85.5",
      "{}", "[]", "9999999999999999", "1e50"};
  for (const auto& value : invalid) {
    auto s = parse("{\"secondary\":" + value + ",\"secondary_resets_at\":1800003600}");
    REQUIRE(!s.available);
    JsonDocument doc;
    REQUIRE(!deserializeJson(doc, "{\"v\":" + value + "}"));
    REQUIRE(usage::remainder(doc["v"]) == usage::UNAVAILABLE);
    REQUIRE(usage::timestamp(doc["v"]) == 0);
    auto gift = parse("{\"gift_reset_expires_at\":" + value + "}");
    REQUIRE(!gift.giftKnown);
  }
  for (int value : {-1, 101, 255, 10000}) {
    auto s = parse("{\"secondary\":" + std::to_string(value) + ",\"secondary_resets_at\":1800003600}");
    REQUIRE(!s.available);
  }
  for (int value : {0, 49, 100}) {
    auto s = parse("{\"secondary\":" + std::to_string(value) + ",\"secondary_resets_at\":1800003600}");
    REQUIRE(s.available && s.used == value);
    REQUIRE(!usage::derive(s, NOW, true).usable); // Unknown age is not fresh.
  }
  for (int value : {-5100, -5000, -1, 0, 1, 5000, 5100}) {
    JsonDocument doc; doc["v"] = value;
    REQUIRE(usage::remainder(doc["v"]) == value);
  }
  for (int value : {-32768, -5101, 5101, 32767}) {
    JsonDocument doc; doc["v"] = value;
    REQUIRE(usage::remainder(doc["v"]) == usage::UNAVAILABLE);
  }
  for (const char* value : {"-1", "4294967296", "\"1800000000\""}) {
    JsonDocument doc; REQUIRE(!deserializeJson(doc, std::string("{\"v\":") + value + "}"));
    REQUIRE(usage::timestamp(doc["v"]) == 0);
  }
  // Each packet is a replacement snapshot, with no zero or old horizon inferred.
  auto oneHorizon = parse(R"({"secondary_remaining_48h_bp":0})");
  REQUIRE(oneHorizon.remaining48h == 0 && oneHorizon.remaining14d == usage::UNAVAILABLE);
  auto legacy = parse(R"({"secondary_forecast_48h":101,"secondary_forecast_14d":80})");
  REQUIRE(legacy.remaining48h == usage::UNAVAILABLE && legacy.remaining14d == usage::UNAVAILABLE);
  REQUIRE(parse(R"({"quota_status":"unexpected"})").source == usage::Source::Unavailable);
  REQUIRE(parse(R"({"quota_status":true})").source == usage::Source::Unavailable);
  REQUIRE(parse(R"({"secondary_forecast_status":42})").forecast == usage::Forecast::Unavailable);
  REQUIRE(parse(R"({"gift_reset_expires_at":0})").giftKnown);
  REQUIRE(!parse(R"({"gift_reset_expires_at":-1})").giftKnown);
}

void lifecycleChecks() {
  auto s = snapshot();
  REQUIRE(usage::derive(s, NOW + 29, true).warning == usage::Warning::None);
  REQUIRE(usage::derive(s, NOW + 30, true).warning == usage::Warning::NoUpdate);
  auto disconnected = usage::derive(s, NOW + 31, false);
  REQUIRE(disconnected.usable && disconnected.warning == usage::Warning::NoLink);
  REQUIRE(disconnected.remaining48h == -125 && disconnected.remaining14d == 3600);
  REQUIRE(disconnected.observedAt == NOW && disconnected.resetSeconds == 4 * 86400 - 31);
  s.source = usage::Source::Cached;
  REQUIRE(usage::derive(s, NOW, true).warning == usage::Warning::NoUpdate);
  REQUIRE(usage::derive(s, NOW + 899, true).usable);
  REQUIRE(!usage::derive(s, NOW + 900, true).usable);
  // Even a malformed producer deadline cannot extend the maximum cache age.
  s.validUntil = NOW + 86400;
  REQUIRE(!usage::derive(s, NOW + 900, true).usable);
  s = snapshot(); s.validUntil = NOW + 100;
  REQUIRE(usage::derive(s, NOW + 99, true).usable);
  REQUIRE(!usage::derive(s, NOW + 100, true).usable);
  s = snapshot(); s.resetsAt = NOW + 60;
  REQUIRE(usage::derive(s, NOW + 59, false).usable);
  REQUIRE(!usage::derive(s, NOW + 60, false).usable);
  s = snapshot(); s.observedAt = 0;
  REQUIRE(!usage::derive(s, NOW, true).usable);
  s.observedAt = NOW + 1;
  auto future = usage::derive(s, NOW, true);
  REQUIRE(!future.usable && future.observedAt == 0);
  s = snapshot(); s.source = usage::Source::Unavailable;
  REQUIRE(!usage::derive(s, NOW, true).usable);
  s = snapshot(); s.available = false;
  REQUIRE(!usage::derive(s, NOW, true).usable);
  s = snapshot(); s.used = 101;
  REQUIRE(!usage::derive(s, NOW, true).usable);
  s = snapshot(); s.used = 0;
  REQUIRE(usage::derive(s, NOW, true).left == 100);
  s.used = 100;
  REQUIRE(usage::derive(s, NOW, true).left == 0);
  // Rendering or connection heartbeats never mutate the authoritative snapshot.
  s = snapshot();
  for (uint32_t now : {NOW, NOW + 30, NOW + 300, NOW + 899, NOW + 900}) {
    const auto v = usage::derive(s, now, true);
    REQUIRE(s.observedAt == NOW && s.validUntil == NOW + 900);
    REQUIRE(v.observedAt == NOW);
  }
}

void metadataChecks() {
  auto s = snapshot();
  s.forecastValidUntil = NOW + 60;
  REQUIRE(usage::derive(s, NOW + 59, true).remaining48h == -125);
  auto expiredForecast = usage::derive(s, NOW + 60, true);
  REQUIRE(expiredForecast.usable && expiredForecast.remaining48h == usage::UNAVAILABLE);
  REQUIRE(expiredForecast.remaining14d == usage::UNAVAILABLE && !expiredForecast.learning);
  s = snapshot(); s.forecast = usage::Forecast::Learning;
  auto learning = usage::derive(s, NOW, true);
  REQUIRE(learning.usable && learning.learning && learning.remaining48h == usage::UNAVAILABLE);
  s.forecast = usage::Forecast::Unavailable;
  REQUIRE(!usage::derive(s, NOW, true).learning);
  s = snapshot(); s.remaining14d = usage::UNAVAILABLE;
  auto partial = usage::derive(s, NOW, true);
  REQUIRE(partial.remaining48h == -125 && partial.remaining14d == usage::UNAVAILABLE);
  s.remaining48h = usage::UNAVAILABLE; s.remaining14d = 0;
  partial = usage::derive(s, NOW, true);
  REQUIRE(partial.remaining48h == usage::UNAVAILABLE && partial.remaining14d == 0);
  s = snapshot(); s.giftExpiresAt = NOW + 10;
  REQUIRE(usage::derive(s, NOW + 9, true).giftSeconds == 1);
  REQUIRE(!usage::derive(s, NOW + 10, true).giftAvailable);
  s = snapshot(); s.giftExpiresAt = 0;
  REQUIRE(!usage::derive(s, NOW, true).giftAvailable);
  s = snapshot(); s.giftKnown = false;
  REQUIRE(!usage::derive(s, NOW, true).giftAvailable);
  s = snapshot(); s.giftObservedAt = NOW - 899;
  REQUIRE(usage::derive(s, NOW, true).giftAvailable);
  REQUIRE(!usage::derive(s, NOW + 1, true).giftAvailable);
  s.giftObservedAt = NOW + 1;
  REQUIRE(!usage::derive(s, NOW, true).giftAvailable);
  s.giftObservedAt = 0;
  REQUIRE(!usage::derive(s, NOW, true).giftAvailable);
}

void geometryChecks() {
  for (bool wide : {false, true}) {
    const int x = 8, width = wide ? 224 : 119, axis = wide ? 112 : 215;
    const int left = 11, right = wide ? 228 : 123;
    REQUIRE(usage::forecastPosition(x, width, -5000) == left);
    REQUIRE(usage::forecastPosition(x, width, 5000) == right);
    REQUIRE(usage::forecastPosition(x, width, -5100) == left);
    REQUIRE(usage::forecastPosition(x, width, 5100) == right);
    for (int bp = -5000; bp < 5000; ++bp)
      REQUIRE(usage::forecastPosition(x, width, bp) <= usage::forecastPosition(x, width, bp + 1));
    for (int bp : {100, 500, 1000, 2000, 5000}) {
      REQUIRE(abs(usage::forecastPosition(x, width, bp) + usage::forecastPosition(x, width, -bp)
                  - left - right) <= 1);
    }
    REQUIRE(usage::forecastPosition(x, width, 100) - usage::forecastPosition(x, width, 0) >
            usage::forecastPosition(x, width, 5000) - usage::forecastPosition(x, width, 4900));
    for (int bp : {-5100, -5001, -5000, -1000, -100, -1, 0, 1, 100, 1000, 5000, 5001, 5100}) {
      auto v = usage::derive(snapshot(), NOW, true);
      v.remaining48h = bp; v.remaining14d = bp;
      auto canvas = render(v, wide);
      canvas.assertLayout();
      for (bool upper : {false, true}) {
        const int color = upper ? usage::RECENT : usage::HISTORY;
        const auto box = canvas.colorBounds(color);
        const bool overflow = abs(bp) > 5000;
        REQUIRE(box.width() == (overflow ? 10 : 5));
        REQUIRE(box.height() == (overflow ? 5 : 10));
        REQUIRE(upper ? box.bottom < axis : box.top > axis);
        if (overflow) {
          const int tip = bp < 0 ? left - 5 : right + 5;
          const int cy = axis + (upper ? -7 : 7);
          REQUIRE(canvas.pixel(tip, cy) == color);
          REQUIRE(canvas.pixel(tip, cy - 1) != color && canvas.pixel(tip, cy + 1) != color);
        } else {
          const int px = usage::forecastPosition(x, width, bp);
          const int tipY = axis + (upper ? -2 : 2);
          REQUIRE(canvas.pixel(px, tipY) == color);
          REQUIRE(canvas.pixel(px - 1, tipY) != color && canvas.pixel(px + 1, tipY) != color);
        }
      }
    }
    // Unknown forecast markers are absent, never fabricated at the zero tick.
    for (bool upper : {false, true}) {
      auto v = usage::derive(snapshot(), NOW, true);
      if (upper) v.remaining48h = usage::UNAVAILABLE; else v.remaining14d = usage::UNAVAILABLE;
      auto canvas = render(v, wide);
      REQUIRE(canvas.colorBounds(upper ? usage::RECENT : usage::HISTORY).empty());
      REQUIRE(!canvas.colorBounds(upper ? usage::HISTORY : usage::RECENT).empty());
    }
    auto v = usage::derive(snapshot(), NOW, true);
    v.remaining48h = 5100; v.remaining14d = -5100; // Keep markers away from tick columns.
    auto canvas = render(v, wide);
    for (int pp : {-50, -40, -30, -20, -10, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 10, 20, 30, 40, 50}) {
      const int px = usage::forecastPosition(x, width, pp * 100);
      const int half = pp == 0 || abs(pp) == 10 || abs(pp) == 50 ? 6 : 3;
      const int color = pp == 0 ? usage::WHITE : pp == -50 ? usage::RED : pp == 50 ? usage::GREEN : usage::DIM;
      REQUIRE(std::any_of(canvas.rects.begin(), canvas.rects.end(), [&](const Rect& r) {
        return r.x == px && r.y == axis - half && r.width == 1 && r.height == 2 * half + 1
            && r.color == color;
      }));
    }
    REQUIRE(canvas.text("-50").color == usage::RED);
    REQUIRE(canvas.text("+50").color == usage::GREEN);
    REQUIRE(canvas.text("0").color == usage::WHITE);
    for (const char* label : {"-10", "+10"}) {
      const auto& tickLabel = canvas.text(label);
      if (wide) REQUIRE(tickLabel.y == canvas.text("0").y);
      else REQUIRE(tickLabel.y > axis);
    }
    REQUIRE(canvas.text("LEFT AT RESET (pp)").color == usage::WHITE);
  }
}

void layoutChecks() {
  for (bool wide : {false, true}) {
    for (int used : {0, 1, 49, 99, 100}) for (bool linked : {false, true})
      for (usage::Source source : {usage::Source::Fresh, usage::Source::Cached}) {
        auto s = snapshot(); s.used = used; s.source = source;
        for (uint32_t age : {0u, 29u, 30u, 59u, 60u, 599u, 899u, 900u, 86400u, 100000000u}) {
          auto v = usage::derive(s, NOW + age, linked);
          auto canvas = render(v, wide);
          canvas.assertLayout();
          REQUIRE(!canvas.hasText("LIVE"));
          if (v.usable) {
            REQUIRE(canvas.hasText("WEEK LEFT"));
            REQUIRE(canvas.hasText(std::to_string(100 - used)));
            REQUIRE(!canvas.hasText("NO DATA"));
            REQUIRE(canvas.text("WEEK LEFT").y < canvas.text("RESET IN").y);
            REQUIRE(canvas.text("RESET IN").y < canvas.text("GIFT EXP").y);
            REQUIRE(canvas.text("GIFT EXP").y + canvas.text("GIFT EXP").height
                    <= canvas.text("LEFT AT RESET (pp)").y);
            if (v.warning == usage::Warning::None) {
              REQUIRE(!canvas.hasText("NO LINK") && !canvas.hasText("NO UPDATE"));
              for (const auto& t : canvas.texts) REQUIRE(t.value.find("AGO") == std::string::npos);
            } else REQUIRE(canvas.hasText(linked ? "NO UPDATE" : "NO LINK"));
          } else {
            REQUIRE(!canvas.hasText("WEEK LEFT") && !canvas.hasText("LEFT AT RESET (pp)"));
            REQUIRE(canvas.hasText(linked ? "NO DATA" : "NO LINK"));
            REQUIRE(canvas.hasText(linked ? "MAC CONNECTED" : "MAC DISCONNECTED"));
            REQUIRE(canvas.hasText("LAST DATA"));
            REQUIRE(canvas.colorBounds(usage::RECENT).empty() && canvas.colorBounds(usage::HISTORY).empty());
          }
        }
      }
    for (auto state : {usage::Forecast::Unavailable, usage::Forecast::Learning}) {
      auto s = snapshot(); s.forecast = state;
      auto canvas = render(usage::derive(s, NOW, true), wide);
      canvas.assertLayout();
      REQUIRE(canvas.hasText("WEEK LEFT") && canvas.hasText("NO FORECAST"));
      REQUIRE(canvas.hasText(state == usage::Forecast::Learning ? "COLLECTING HISTORY" : "FORECAST UNAVAILABLE"));
      REQUIRE(!canvas.hasText("LEFT AT RESET (pp)"));
      REQUIRE(canvas.text("NO FORECAST").y >= canvas.text("GIFT EXP").y + canvas.text("GIFT EXP").height);
    }
    for (bool linked : {false, true}) {
      auto canvas = render(usage::derive(usage::Snapshot{}, NOW, linked), wide);
      canvas.assertLayout();
      REQUIRE(canvas.hasText("--"));
      REQUIRE(canvas.hasText(wide ? (linked ? "CHECK CODEX ON YOUR MAC" : "CHECK BLUETOOTH ON YOUR MAC")
                                  : (linked ? "CHECK CODEX" : "CHECK BLUETOOTH")));
    }
    // Check remaining bar, percentage size and both timer threshold colors.
    for (int used : {0, 49, 100}) {
      auto s = snapshot(); s.used = used;
      auto canvas = render(usage::derive(s, NOW, true), wide);
      const int x = wide ? 108 : 8, y = wide ? 39 : 148, width = wide ? 124 : 119;
      const int fill = width * (100 - used) / 100;
      for (int xx = 0; xx < width; ++xx)
        REQUIRE(canvas.pixel(x + xx, y) == (xx < fill ? usage::rgb(0xC5C5C5) : usage::rgb(0x303030)));
      REQUIRE(std::any_of(canvas.texts.begin(), canvas.texts.end(), [&](const Text& t) {
        return t.value == std::to_string(100 - used) && t.size == (wide ? 3 : 2);
      }));
    }
    for (uint32_t remaining : {30u, 60u, 3600u, 86400u, 2 * 86400u, 2 * 86400u + 1,
                               4 * 86400u, 4 * 86400u + 1, 7 * 86400u}) {
      auto s = snapshot(); s.resetsAt = s.giftExpiresAt = NOW + remaining;
      auto canvas = render(usage::derive(s, NOW, true), wide);
      canvas.assertLayout();
      const int expected = remaining > 4 * 86400u ? usage::GREEN : remaining > 2 * 86400u ? usage::AMBER : usage::RED;
      char value[24]; usage::durationText(remaining, value, sizeof(value));
      int found = 0;
      for (const auto& t : canvas.texts) if (t.value == value) { REQUIRE(t.color == expected); ++found; }
      REQUIRE(found == 2);
    }
  }
}

void textChecks() {
  for (const auto& item : std::vector<std::pair<uint32_t, std::string>>{
      {0, "<1m"}, {59, "<1m"}, {60, "1m"}, {3599, "59m"},
      {3600, "1h 00m"}, {86399, "23h 59m"}, {86400, "1d 00h"}, {7 * 86400, "7d 00h"}}) {
    char out[24]; usage::durationText(item.first, out, sizeof(out)); REQUIRE(out == item.second);
  }
  usage::View v; v.now = NOW;
  for (const auto& item : std::vector<std::pair<uint32_t, std::string>>{
      {0, "<1m AGO"}, {59, "<1m AGO"}, {60, "1m AGO"}, {3599, "59m AGO"},
      {3600, "1h AGO"}, {86399, "23h AGO"}, {86400, "1d AGO"}, {3 * 86400, "3d AGO"}}) {
    char out[24]; v.observedAt = NOW - item.first;
    usage::ageText(v, out, sizeof(out)); REQUIRE(out == item.second);
  }
  char out[24]; v.observedAt = 0; usage::ageText(v, out, sizeof(out)); REQUIRE(std::string(out) == "--");
  v.observedAt = NOW + 1; usage::ageText(v, out, sizeof(out)); REQUIRE(std::string(out) == "--");
}

int main(int argc, char** argv) {
  REQUIRE(argc == 2);
  const std::string check = argv[1];
  if (check == "parser") parserChecks();
  else if (check == "lifecycle") lifecycleChecks();
  else if (check == "metadata") metadataChecks();
  else if (check == "geometry") geometryChecks();
  else if (check == "layout") layoutChecks();
  else if (check == "text") textChecks();
  else REQUIRE(false);
}
'''


INTEGRATION_PRELUDE = r'''
#include <ArduinoJson.h>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <iostream>
#include <string>
#include "usage_packet.h"
static uint32_t fakeMs = 1000;
uint32_t millis() { return fakeMs; }
bool xferCommand(JsonDocument& doc) { return doc["test_transfer"] | false; }
void appRtcSynced(time_t) {}
void statsOnBridgeTokens(uint32_t) {}
#define REQUIRE(condition) do { if (!(condition)) { \
  std::cerr << __LINE__ << ": " << #condition << "\n"; std::exit(1); \
} } while (false)
'''

INTEGRATION_CHECKS = r'''
int main() {
  constexpr uint32_t now = 1800000000;
  TamaState state{};
  const char* fresh = R"({"state":"busy","tokens":42,"now":1800000000,
    "secondary":49,"secondary_resets_at":1800345600,
    "quota_observed_at":1800000000,"quota_valid_until":1800000900,"quota_status":"fresh",
    "secondary_remaining_48h_bp":-125,"secondary_remaining_14d_bp":3600,
    "secondary_forecast_valid_until":1800000900,"secondary_forecast_status":"ready"})";
  _applyJson(fresh, &state);
  REQUIRE(state.quotaRevision == 1 && state.quota.used == 49);
  REQUIRE(state.codexTokens == 42 && state.sessionsRunning == 1);
  REQUIRE(dataConnected() && usage::derive(state.quota, now, dataConnected()).usable);
  uint32_t utc = 0;
  REQUIRE(dataUtcNow(&utc) && utc == now);

  fakeMs += 31000;
  REQUIRE(!dataConnected());
  REQUIRE(dataUtcNow(&utc) && utc == now + 31);
  auto view = usage::derive(state.quota, utc, dataConnected());
  REQUIRE(view.usable && view.warning == usage::Warning::NoLink);
  _applyJson("invalid JSON", &state);
  REQUIRE(!dataConnected() && state.quotaRevision == 1);

  // RTC sync, task activity, and transfer traffic refresh link, not quota age.
  _applyJson(R"({"time":[1800000031,7200]})", &state);
  REQUIRE(dataConnected() && dataRtcValid());
  REQUIRE(state.quota.observedAt == now && state.quotaRevision == 1);
  view = usage::derive(state.quota, utc, dataConnected());
  REQUIRE(view.usable && view.warning == usage::Warning::NoUpdate);
  fakeMs += 60000;
  _applyJson(R"({"total":2,"running":1,"entries":["Working"]})", &state);
  REQUIRE(dataConnected() && state.sessionsTotal == 2 && state.nLines == 1);
  REQUIRE(state.quota.observedAt == now && state.quota.validUntil == now + 900);
  REQUIRE(state.quotaRevision == 1);
  fakeMs += 60000;
  _applyJson(R"({"test_transfer":true})", &state);
  REQUIRE(dataConnected() && state.quotaRevision == 1);
  REQUIRE(dataUtcNow(&utc) && utc == now + 151);

  // Repeated cached packets retain the producer observation and fixed expiry.
  const char* cached = R"({"quota_status":"cached","now":1800000151,
    "secondary":49,"secondary_resets_at":1800345600,
    "quota_observed_at":1800000000,"quota_valid_until":1800000900,
    "secondary_remaining_48h_bp":-125,"secondary_remaining_14d_bp":3600,
    "secondary_forecast_valid_until":1800000900,"secondary_forecast_status":"ready"})";
  _applyJson(cached, &state);
  REQUIRE(state.quotaRevision == 2 && state.quota.observedAt == now);
  REQUIRE(std::string(state.codexState) == "busy");
  view = usage::derive(state.quota, utc, dataConnected());
  REQUIRE(view.usable && view.warning == usage::Warning::NoUpdate);
  REQUIRE(view.remaining48h == -125 && view.remaining14d == 3600);
  char age[24]; usage::ageText(view, age, sizeof(age));
  REQUIRE(std::string(age) == "2m AGO");
  fakeMs += (900 - 151) * 1000;
  _applyJson(R"({"test_transfer":true})", &state);
  REQUIRE(dataConnected() && dataUtcNow(&utc) && utc == now + 900);
  REQUIRE(!usage::derive(state.quota, utc, dataConnected()).usable);
  REQUIRE(state.quota.observedAt == now && state.quota.validUntil == now + 900);

  // An explicit unavailable snapshot clears old graph values but can keep age.
  _applyJson(R"({"state":"idle","quota_status":"unavailable","quota_observed_at":1800000000})", &state);
  REQUIRE(state.quotaRevision == 3 && !state.quota.available);
  REQUIRE(state.quota.observedAt == now);
  REQUIRE(state.quota.remaining48h == usage::UNAVAILABLE && state.quota.remaining14d == usage::UNAVAILABLE);
  REQUIRE(!usage::derive(state.quota, utc, true).usable);
  _applyJson(R"({"state":"idle"})", &state);
  REQUIRE(state.quota.observedAt == 0 && !state.quota.available);
  TamaState initial{};
  _applyJson(cached, &initial);
  REQUIRE(std::string(initial.codexState) == "idle");
}
'''


class ForecastFirmwareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("c++") or not ARDUINO_JSON.exists():
            raise unittest.SkipTest("Install a C++ compiler and run pio run -e m5stack-sticks3 first")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.binary = cls.compile("check", SOURCE)

        # The hardware-free parser section is included verbatim. Extract only
        # because data.h's BLE/USB includes require Arduino device libraries.
        data = (ROOT / "src/data.h").read_text()
        data = data[data.index("struct TamaState {"):data.index("template<size_t N>")]
        cls.integration_binary = cls.compile("integration", INTEGRATION_PRELUDE + data + INTEGRATION_CHECKS)

    @classmethod
    def compile(cls, name, code):
        source = Path(cls.tmp.name) / f"{name}.cpp"
        source.write_text(code)
        binary = Path(cls.tmp.name) / name
        compiled = subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
             "-fno-omit-frame-pointer", "-I", str(ARDUINO_JSON), "-I", str(ROOT / "src"),
             str(source), "-o", str(binary)],
            capture_output=True, text=True,
        )
        if compiled.returncode:
            raise AssertionError(f"{name} checks failed to compile:\n{compiled.stdout}{compiled.stderr}")
        return binary

    def check(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"{name}:\n{result.stdout}{result.stderr}")

    def test_packet_types_missing_metadata_and_legacy_fields(self):
        self.check("parser")

    def test_freshness_transport_independence_and_expiration(self):
        self.check("lifecycle")

    def test_partial_forecasts_and_gift_expiration(self):
        self.check("metadata")

    def test_log_scale_tick_heights_and_overflow_markers(self):
        self.check("geometry")

    def test_both_orientations_text_bounds_and_information_states(self):
        self.check("layout")

    def test_honest_age_and_countdown_formatting(self):
        self.check("text")

    def test_real_packet_routing_does_not_refresh_cached_quota(self):
        result = subprocess.run([str(self.integration_binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"integration:\n{result.stdout}{result.stderr}")


if __name__ == "__main__":
    unittest.main()
