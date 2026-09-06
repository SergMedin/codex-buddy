"""Host checks of the actual firmware parser and drawing functions.

Requires a C++ compiler and ArduinoJson downloaded by `pio run`.
Only hardware/graphics primitives are stubbed; production functions are extracted
verbatim so the checks exercise their real implementation.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
ARDUINO_JSON = ROOT / ".pio/libdeps/m5stack-sticks3/ArduinoJson/src"

PRELUDE = r'''
#include <ArduinoJson.h>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>
#include <algorithm>
#include <cstdlib>
static uint32_t fakeMs = 1000;
uint32_t millis() { return fakeMs; }
bool xferCommand(JsonDocument&) { return false; }
void appRtcSynced(time_t) {}
void statsOnBridgeTokens(uint32_t) {}
'''

GRAPHICS = r'''
namespace lgfx { namespace v1 {
struct LGFXBase {
  std::vector<int> pixels = std::vector<int>(240*240, -1);
  std::vector<std::string> text;
  void pixel(int x, int y, int c) {
    assert(x>=0 && x<240 && y>=0 && y<240);
    pixels[y*240+x] = c;
  }
  void fillRect(int x,int y,int w,int h,int c) {
    for(int j=y;j<y+h;++j) for(int i=x;i<x+w;++i) pixel(i,j,c);
  }
  void drawRect(int x,int y,int w,int h,int c) {
    fillRect(x,y,w,1,c); fillRect(x,y+h-1,w,1,c);
    fillRect(x,y,1,h,c); fillRect(x+w-1,y,1,h,c);
  }
  void drawLine(int x,int y,int xx,int yy,int c) {
    int dx=abs(xx-x), sx=x<xx?1:-1, dy=-abs(yy-y), sy=y<yy?1:-1, e=dx+dy;
    while(true) {
      pixel(x,y,c); if(x==xx && y==yy) break;
      int e2=2*e; if(e2>=dy) {e+=dy;x+=sx;} if(e2<=dx) {e+=dx;y+=sy;}
    }
  }
  void setTextSize(int s) {text.push_back("size:"+std::to_string(s));}
  void setTextDatum(int s) {text.push_back("datum:"+std::to_string(s));}
  void setTextColor(int c,int bg) {text.push_back("color:"+std::to_string(c)+":"+std::to_string(bg));}
  void drawString(const char* s,int x,int y) {text.push_back(std::string(s)+":"+std::to_string(x)+":"+std::to_string(y));}
  int textWidth(const char* s) {return strlen(s)*6;}
};
}}
struct Palette { uint16_t text=0xFFFF, textDim=0x7BEF, bg=0x0000; };
constexpr int TL_DATUM=0, TR_DATUM=1;
uint16_t usageColor(uint8_t pct,const Palette&) {return pct<35?0x001F:pct<70?0x07E0:0xFD20;}
uint16_t resetColor(uint32_t,const char*,bool,const Palette&) {return 0x07E0;}
void resetTimeText(uint32_t,char* out,size_t n) {snprintf(out,n,"3d 00h");}
'''

CHECKS = r'''
int main(int argc, char** argv) {
  assert(argc==2);
  if(std::string(argv[1])=="parser") {
    TamaState s{};
    assert(s.codexForecast48h==-1 && s.codexForecast14d==-1);
    const char* packet=R"({"state":"idle","now":1800000000,"secondary":40,"secondary_resets_at":1800003600,"secondary_forecast_48h":85,"secondary_forecast_14d":64,"secondary_forecast_valid_until":1800000900})";
    _applyJson(packet,&s); s.connected=true;
    assert(s.codexSecondary==40 && s.codexForecast48h==85 && s.codexForecast14d==64);
    assert(dataForecastActive(s));
    fakeMs += 900000;
    assert(!dataForecastActive(s)); // Even if other packets keep BLE alive.
    fakeMs=1000;
    _applyJson(packet,&s);
    dataSetDemo(true); assert(!dataForecastActive(s)); dataSetDemo(false);
    s.connected=false; assert(!dataForecastActive(s)); s.connected=true;
    _applyJson(R"({"state":"idle","secondary":40,"secondary_resets_at":1800003600})",&s);
    assert(s.codexForecast48h==-1 && s.codexForecast14d==-1 && !dataForecastActive(s));
    _applyJson(packet,&s);
    _applyJson(R"({"state":"idle"})",&s);
    assert(!s.codexSecondaryAvailable && s.codexForecast48h==-1 && s.codexForecast14d==-1);
    for(const char* value: {"null","true","\"85\"","-1","102","85.5","9999999999999999"}) {
      std::string bad=std::string("{\"v\":")+value+"}";
      JsonDocument doc; deserializeJson(doc,bad);
      assert(_jsonForecast(doc["v"])==-1);
    }
    for(int n: {0,64,100,101}) {
      JsonDocument doc; doc["v"]=n; assert(_jsonForecast(doc["v"])==n);
    }
    _applyJson(packet,&s); s.codexSecondaryResetsAt=1800000000;
    assert(!dataForecastActive(s));
  } else if(std::string(argv[1])=="pixels") {
    for(int w: {8,119,120}) for(int forecast=-1;forecast<=102;++forecast) {
      lgfx::v1::LGFXBase dst;
      drawForecastTick(&dst,8,100,w,forecast,true);
      drawForecastTick(&dst,8,100,w,forecast,false);
      bool cyan=false, magenta=false;
      for(int y=0;y<240;++y) for(int x=0;x<240;++x) {
        int c=dst.pixels[y*240+x]; if(c==-1) continue;
        assert(forecast>=0 && forecast<=101);
        assert(x>8 && x<8+w-1 && y>100 && y<112);
        if(c==0x07FF) {cyan=true; assert(y<=105);}
        if(c==0xF81F) {magenta=true; assert(y>=107);}
      }
      assert(cyan==(forecast>=0 && forecast<=101));
      assert(magenta==(forecast>=0 && forecast<=101));
    }
    lgfx::v1::LGFXBase ceiling, overflow;
    drawForecastTick(&ceiling,8,100,119,100,true);
    drawForecastTick(&overflow,8,100,119,101,true);
    assert(ceiling.pixels!=overflow.pixels);
  } else if(std::string(argv[1])=="layout") {
    Palette p; dataSyncUtc(1800000000);
    for(bool landscape: {false,true}) for(int pct: {0,34,35,69,70,100}) {
      int x=landscape?112:8, y=landscape?81:184, w=landscape?120:119;
      lgfx::v1::LGFXBase original, marked;
      drawUsageMeterOn(&original,x,y,w,pct,"7d",1800003600,true,true,p);
      drawUsageMeterOn(&marked,x,y,w,pct,"7d",1800003600,true,true,p,85,64);
      assert(original.text==marked.text);
      for(int yy=0;yy<240;++yy) for(int xx=0;xx<240;++xx) {
        if(original.pixels[yy*240+xx]==marked.pixels[yy*240+xx]) continue;
        assert(xx>x && xx<x+w-1 && yy>y+24 && yy<y+36);
      }
      lgfx::v1::LGFXBase offline, offlineMarked;
      drawUsageMeterOn(&offline,x,y,w,pct,"7d",1800003600,false,true,p);
      drawUsageMeterOn(&offlineMarked,x,y,w,pct,"7d",1800003600,false,true,p,85,64);
      assert(offline.pixels==offlineMarked.pixels && offline.text==offlineMarked.text);
    }
  } else { assert(false); }
}
'''


class ForecastFirmwareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("c++") or not ARDUINO_JSON.exists():
            raise unittest.SkipTest("Install a C++ compiler and run pio run -e m5stack-sticks3 first")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        data = (ROOT / "src/data.h").read_text()
        data = data[data.index("struct TamaState"):data.index("template<size_t N>")]
        main = (ROOT / "src/main.cpp").read_text()
        drawing = main[main.index("static void drawForecastTick"):main.index("static void drawUsageMeter(int")]
        source = Path(cls.tmp.name) / "check.cpp"
        source.write_text(PRELUDE + data + GRAPHICS + drawing + CHECKS)
        cls.binary = Path(cls.tmp.name) / "check"
        subprocess.run(["c++", "-std=c++17", "-I", str(ARDUINO_JSON), str(source), "-o", str(cls.binary)],
                       check=True, capture_output=True, text=True)

    def test_firmware_packet_validation_expiry_and_legacy_compatibility(self):
        subprocess.run([str(self.binary), "parser"], check=True, capture_output=True)

    def test_markers_stay_inside_bar_and_remain_distinct_when_overlapping(self):
        subprocess.run([str(self.binary), "pixels"], check=True, capture_output=True)

    def test_both_layouts_keep_all_text_and_pixels_outside_bar_unchanged(self):
        subprocess.run([str(self.binary), "layout"], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
