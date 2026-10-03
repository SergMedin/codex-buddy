#pragma once
#include "usage_dashboard.h"

namespace usage {

constexpr uint16_t rgb(unsigned value) {
  return ((value >> 8) & 0xF800) | ((value >> 5) & 0x07E0) | ((value >> 3) & 0x001F);
}
constexpr uint16_t WHITE = rgb(0xFFFFFF), MUTED = rgb(0xAAAAAA), DIM = rgb(0x848284);
constexpr uint16_t RED = rgb(0xC2676B), GREEN = rgb(0x74AF88), AMBER = rgb(0xC79A57);
constexpr uint16_t RECENT = rgb(0x55BDE0), HISTORY = rgb(0xB58BE3);
constexpr int LANDSCAPE_PET_WIDTH = 104, LANDSCAPE_PET_HEIGHT = 92;
enum class Align { Left, Center, Right };

template<class Canvas>
class Display {
 public:
  Display(Canvas& canvas, uint16_t background) : c(canvas), bg(background) {}

  void landscape(const View& v) {
    c.fillRect(LANDSCAPE_PET_WIDTH, 0, 240 - LANDSCAPE_PET_WIDTH, 92, bg);
    c.fillRect(0, 92, 240, 43, bg);
    if (!v.usable) { empty(v, true); return; }
    const bool cached = v.warning != Warning::None;
    text("WEEK LEFT", 108, 8, MUTED);
    char number[4]; snprintf(number, sizeof(number), "%u", v.left);
    text(number, 224, 6, cached ? MUTED : WHITE, 3, Align::Right);
    text("%", 226, 20, MUTED);
    if (cached) {
      text(warning(v), 108, 23, RED);
      char age[20]; ageText(v, age, sizeof(age));
      text(age, 232, 30, DIM, 1, Align::Right);
    }
    bar(v, 108, 39, 124, 5);
    timer("RESET IN", v.resetSeconds, true, 108, 232, 56);
    timer("GIFT EXP", v.giftSeconds, v.giftAvailable, 108, 232, 72);
    forecast(v, 8, 224, 112, true);
  }

  void portrait(const View& v) {
    c.fillRect(0, 120, 135, 120, bg);
    if (!v.usable) { empty(v, false); return; }
    text("WEEK LEFT", 8, 124, MUTED);
    char number[4]; snprintf(number, sizeof(number), "%u", v.left);
    text(number, 118, 122, v.warning == Warning::None ? WHITE : MUTED, 2, Align::Right);
    text("%", 121, 130, MUTED);
    if (v.warning != Warning::None) {
      text(warning(v), 8, 138, RED);
      char age[20]; ageText(v, age, sizeof(age));
      text(age, 127, 138, DIM, 1, Align::Right);
    }
    bar(v, 8, 148, 119, 4);
    timer("RESET IN", v.resetSeconds, true, 8, 127, 158);
    timer("GIFT EXP", v.giftSeconds, v.giftAvailable, 8, 127, 170);
    forecast(v, 8, 119, 215, false);
  }

 private:
  Canvas& c;
  uint16_t bg;

  void text(const char* value, int x, int y, uint16_t color, int size = 1,
            Align align = Align::Left) {
    c.setTextSize(size);
    c.setTextColor(color, bg);
    c.setTextDatum(TL_DATUM);
    const int width = c.textWidth(value);
    if (align == Align::Right) x -= width;
    else if (align == Align::Center) x -= width / 2;
    c.drawString(value, x, y);
  }

  static const char* warning(const View& v) {
    return v.warning == Warning::NoLink ? "NO LINK" : "NO UPDATE";
  }

  void bar(const View& v, int x, int y, int width, int height) {
    c.fillRect(x, y, width, height, rgb(0x303030));
    const int fill = width * v.left / 100;
    if (fill) c.fillRect(x, y, fill, height, v.warning == Warning::None ? rgb(0xC5C5C5) : DIM);
  }

  void timer(const char* label, uint32_t seconds, bool available, int x, int right, int y) {
    text(label, x, y, MUTED);
    char value[24];
    if (available) durationText(seconds, value, sizeof(value));
    else snprintf(value, sizeof(value), "--");
    const uint16_t color = !available ? DIM : seconds > 4 * 86400 ? GREEN
        : seconds > 2 * 86400 ? AMBER : RED;
    text(value, right, y, color, 1, Align::Right);
  }

  void marker(int x, int width, int axis, int value, bool upper, bool cached) {
    if (!markerValid(value)) return;
    const int px = forecastPosition(x, width, value);
    const uint16_t color = upper ? (cached ? rgb(0x618B9B) : RECENT)
                                : (cached ? rgb(0x89739F) : HISTORY);
    if (value < -5000 || value > 5000) {
      const int inward = value < 0 ? 1 : -1;
      const int cy = axis + (upper ? -7 : 7);
      for (int column = 0; column < 10; ++column) {
        const int radius = (2 * column + 4) / 9;
        c.fillRect(px + inward * (column - 5), cy - radius, 1, 2 * radius + 1, color);
      }
    } else {
      for (int row = 0; row < 10; ++row) {
        const int radius = (2 * row + 4) / 9;
        c.fillRect(px - radius, axis + (upper ? -2 - row : 2 + row), 2 * radius + 1, 1, color);
      }
    }
  }

  void forecast(const View& v, int x, int width, int axis, bool wide) {
    if (!markerValid(v.remaining48h) && !markerValid(v.remaining14d)) {
      text("NO FORECAST", x + width / 2, axis - (wide ? 14 : 18), WHITE, wide ? 2 : 1, Align::Center);
      text(v.learning ? "COLLECTING HISTORY" : "FORECAST UNAVAILABLE", x + width / 2,
           axis + (wide ? 9 : 5), DIM, 1, Align::Center);
      return;
    }
    const int top = axis - 18, bottom = axis + 13;
    c.drawLine(x + 3, axis, x + width - 4, axis, DIM);
    const int ticks[] = {-50,-40,-30,-20,-10,-5,-4,-3,-2,-1,0,1,2,3,4,5,10,20,30,40,50};
    for (int value : ticks) {
      const int height = value == 0 || value == -10 || value == 10 || value == -50 || value == 50 ? 13 : 7;
      const uint16_t color = value == 0 ? WHITE : value == -50 ? RED : value == 50 ? GREEN : DIM;
      c.fillRect(forecastPosition(x, width, value * 100), axis - height / 2, 1, height, color);
    }
    text("-50", x, top, RED);
    text("+50", x + width, top, GREEN, 1, Align::Right);
    text("0", forecastPosition(x, width, 0), top, WHITE, 1, Align::Center);
    const int tensY = wide ? top : bottom;
    text("-10", forecastPosition(x, width, -1000), tensY, DIM, 1, Align::Center);
    text("+10", forecastPosition(x, width, 1000), tensY, DIM, 1, Align::Center);
    text("LEFT AT RESET (pp)", x + width / 2, wide ? bottom : axis - 31, WHITE, 1, Align::Center);
    marker(x, width, axis, v.remaining48h, true, v.warning != Warning::None);
    marker(x, width, axis, v.remaining14d, false, v.warning != Warning::None);
  }

  void empty(const View& v, bool wide) {
    const int x = wide ? 108 : 67;
    const Align align = wide ? Align::Left : Align::Center;
    text(v.linked ? "NO DATA" : "NO LINK", x, wide ? 20 : 128, v.linked ? AMBER : RED, 2, align);
    text(v.linked ? "MAC CONNECTED" : "MAC DISCONNECTED", x, wide ? 44 : 153, MUTED, 1, align);
    text("LAST DATA", x, wide ? 62 : 171, DIM, 1, align);
    char age[20]; ageText(v, age, sizeof(age));
    text(age, x, wide ? 75 : 185, WHITE, 2, align);
    if (wide) {
      if (v.linked) {
        text("NO FRESH QUOTA DATA", 120, 106, WHITE, 1, Align::Center);
        text("CHECK CODEX ON YOUR MAC", 120, 122, MUTED, 1, Align::Center);
      } else text("CHECK BLUETOOTH ON YOUR MAC", 120, 113, MUTED, 1, Align::Center);
    } else {
      text(v.linked ? "CHECK CODEX" : "CHECK BLUETOOTH", 67, 216, WHITE, 1, Align::Center);
      text("ON YOUR MAC", 67, 230, MUTED, 1, Align::Center);
    }
  }
};

} // namespace usage
