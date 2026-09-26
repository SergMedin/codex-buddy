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
#include <cmath>
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
constexpr int TL_DATUM=0, TR_DATUM=1, TC_DATUM=2;
uint16_t usageColor(uint8_t pct,const Palette&) {return pct<35?0x001F:pct<70?0x07E0:0xFD20;}
uint16_t resetColor(uint32_t,const char*,bool,const Palette&) {return 0x07E0;}
void resetTimeText(uint32_t,char* out,size_t n) {snprintf(out,n,"3d 00h");}
'''

CHECKS = r'''
int main(int argc, char** argv) {
  assert(argc==2);
  if(std::string(argv[1])=="parser") {
    TamaState s{};
    assert(s.codexRemaining48h==FORECAST_UNAVAILABLE && s.codexRemaining14d==FORECAST_UNAVAILABLE);
    const char* packet=R"({"state":"idle","now":1800000000,"secondary":40,"secondary_resets_at":1800003600,"secondary_remaining_48h_bp":-125,"secondary_remaining_14d_bp":3600,"secondary_forecast_valid_until":1800000900})";
    _applyJson(packet,&s); s.connected=true;
    assert(s.codexSecondary==40 && s.codexRemaining48h==-125 && s.codexRemaining14d==3600);
    assert(dataForecastActive(s));
    fakeMs += 900000;
    assert(!dataForecastActive(s));
    fakeMs=1000;
    _applyJson(packet,&s);
    dataSetDemo(true); assert(!dataForecastActive(s)); dataSetDemo(false);
    s.connected=false; assert(!dataForecastActive(s)); s.connected=true;
    _applyJson(R"({"state":"idle","secondary":40,"secondary_resets_at":1800003600})",&s);
    assert(s.codexRemaining48h==FORECAST_UNAVAILABLE && s.codexRemaining14d==FORECAST_UNAVAILABLE && !dataForecastActive(s));
    _applyJson(packet,&s);
    _applyJson(R"({"state":"idle"})",&s);
    assert(!s.codexSecondaryAvailable && s.codexRemaining48h==FORECAST_UNAVAILABLE && s.codexRemaining14d==FORECAST_UNAVAILABLE);
    for(const char* value: {"null","true", "false", "\"85\"","-5101","5101","85.5","9999999999999999","1e50"}) {
      std::string bad=std::string("{\"v\":")+value+"}";
      JsonDocument doc; deserializeJson(doc,bad);
      assert(_jsonRemaining(doc["v"])==FORECAST_UNAVAILABLE);
    }
    for(int n: {-5100,-5000,-100,-1,0,1,100,5000,5100}) {
      JsonDocument doc; doc["v"]=n; assert(_jsonRemaining(doc["v"])==n);
    }
    // Old overflow value cannot be converted into a meaningful position.
    _applyJson(R"({"state":"idle","secondary":40,"secondary_resets_at":1800003600,"secondary_forecast_48h":101,"secondary_forecast_14d":80,"secondary_forecast_valid_until":1800000900})",&s);
    assert(s.codexRemaining48h==FORECAST_UNAVAILABLE && s.codexRemaining14d==FORECAST_UNAVAILABLE);
    _applyJson(R"({"state":"idle","secondary":40,"secondary_resets_at":1800003600,"secondary_remaining_48h_bp":0,"secondary_forecast_valid_until":1800000900})",&s);
    assert(s.codexRemaining48h==0 && s.codexRemaining14d==FORECAST_UNAVAILABLE);
    _applyJson(packet,&s); s.codexSecondaryResetsAt=1800000000;
    assert(!dataForecastActive(s));
  } else if(std::string(argv[1])=="pixels") {
    for(bool landscape: {false,true}) {
      int x=landscape?112:8, w=landscape?120:119, axis=landscape?59:155;
      int screenW=landscape?240:135;
      assert(forecastPosition(x,w,5000)==x+3);
      assert(forecastPosition(x,w,-5000)==x+w-4);
      for(int bp=-5000;bp<5000;++bp)
        assert(forecastPosition(x,w,bp)>=forecastPosition(x,w,bp+1));
      assert(forecastPosition(x,w,0)-forecastPosition(x,w,100) >
             forecastPosition(x,w,4900)-forecastPosition(x,w,5000));
      for(int bp: {-5101,-5100,-5001,-5000,-1000,-500,-100,-1,0,1,100,500,1000,5001,5000,5100,5101,int(FORECAST_UNAVAILABLE)}) {
        lgfx::v1::LGFXBase dst;
        drawForecastMarker(&dst,x,axis,w,bp,true);
        drawForecastMarker(&dst,x,axis,w,bp,false);
        for(int color: {0x07FF,0xF81F}) {
          int minX=240,maxX=-1,minY=240,maxY=-1;
          for(int yy=0;yy<240;++yy) for(int xx=0;xx<240;++xx) {
            if(dst.pixels[yy*240+xx]!=color) continue;
            assert(xx>=0 && xx<screenW);
            assert(xx>=x-2 && xx<=x+w+1);
            assert(color==0x07FF ? yy<axis : yy>axis);
            minX=std::min(minX,xx);maxX=std::max(maxX,xx);
            minY=std::min(minY,yy);maxY=std::max(maxY,yy);
          }
          if(abs(bp)>5100) { assert(maxX==-1); continue; }
          bool overflow=abs(bp)>5000;
          assert(maxX-minX+1==(overflow?10:5));
          assert(maxY-minY+1==(overflow?5:10));
          if(overflow) {
            int cy=axis+(color==0x07FF?-7:7);
            int tip=bp>0?x-2:x+w+1;
            assert(dst.pixels[cy*240+tip]==color);
            assert(dst.pixels[(cy-1)*240+tip]!=color);
            assert(dst.pixels[(cy+1)*240+tip]!=color);
          }
        }
      }
    }
  } else if(std::string(argv[1])=="layout") {
    Palette p; dataSyncUtc(1800000000);
    for(bool landscape: {false,true}) {
      int x=landscape?112:8, y=landscape?27:123, w=landscape?120:119;
      int weeklyY=landscape?81:184;
      lgfx::v1::LGFXBase dst, missing;
      drawForecastScaleOn(&dst,x,y,w,5100,-5100,p);
      drawForecastScaleOn(&missing,x,y,w,FORECAST_UNAVAILABLE,FORECAST_UNAVAILABLE,p);
      bool unavailable=false;
      for(auto& text: missing.text) if(text.find("NO FORECAST:")==0) unavailable=true;
      assert(unavailable);
      for(int yy=0;yy<240;++yy) for(int xx=0;xx<240;++xx) {
        assert(missing.pixels[yy*240+xx]==-1);
        if(dst.pixels[yy*240+xx]!=-1) assert(yy>=y && yy<weeklyY);
      }
      // Inspect ticks away from the two forecast markers.
      lgfx::v1::LGFXBase ticks;
      drawForecastScaleOn(&ticks,x,y,w,250,-250,p);
      // All labelled major ticks are thirteen pixels, with their original gray.
      for(int bp: {-5000,-1000,1000,5000}) {
        int px=forecastPosition(x,w,bp), axis=y+32;
        for(int dy=-6;dy<=6;++dy) assert(ticks.pixels[(axis+dy)*240+px]==p.textDim);
        assert(ticks.pixels[(axis-7)*240+px]==-1);
        assert(ticks.pixels[(axis+7)*240+px]==-1);
      }
      for(int bp: {-4000,-3000,-2000,-100,100,2000,3000,4000}) {
        int px=forecastPosition(x,w,bp), axis=y+32;
        for(int dy=-3;dy<=3;++dy) assert(ticks.pixels[(axis+dy)*240+px]==p.textDim);
        assert(ticks.pixels[(axis-4)*240+px]==-1);
        assert(ticks.pixels[(axis+4)*240+px]==-1);
      }
      int center=forecastPosition(x,w,0);
      for(int dy=-6;dy<=6;++dy) assert(dst.pixels[(y+32+dy)*240+center]==0xFFFF);
      for(int pct: {0,34,35,69,70,100}) {
        lgfx::v1::LGFXBase weekly;
        drawUsageMeterOn(&weekly,x,weeklyY,w,pct,"7d",1800003600,true,true,p);
        for(int pixel: weekly.pixels) assert(pixel!=0x07FF && pixel!=0xF81F);
      }
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
        data = data[data.index("// Signed hundredths"):data.index("template<size_t N>")]
        main = (ROOT / "src/main.cpp").read_text()
        drawing = main[main.index("static int forecastPosition"):main.index("static void drawUsageMeter(int")]
        source = Path(cls.tmp.name) / "check.cpp"
        source.write_text(PRELUDE + data + GRAPHICS + drawing + CHECKS)
        cls.binary = Path(cls.tmp.name) / "check"
        subprocess.run(["c++", "-std=c++17", "-I", str(ARDUINO_JSON), str(source), "-o", str(cls.binary)],
                       check=True, capture_output=True, text=True)

    def test_firmware_packet_validation_expiry_and_legacy_compatibility(self):
        subprocess.run([str(self.binary), "parser"], check=True, capture_output=True)

    def test_log_scale_and_marker_bounds_in_both_orientations(self):
        subprocess.run([str(self.binary), "pixels"], check=True, capture_output=True)

    def test_scale_ticks_missing_data_and_unmarked_weekly_bar(self):
        subprocess.run([str(self.binary), "layout"], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
