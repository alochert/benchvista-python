"""SH1106 OLED, 128 x 64, on I2C: a Spark driver for BenchVista. CC0-1.0.

The controller holds 132 columns and its panel shows the middle 128, so every page is sent from
column 2. A one-bit frame in MONO_VLSB order, which is the controller's own page layout, sent
page by page by show(). The setup values are the datasheet's own after power-on.
"""
import framebuf

COMMANDS = 0x00
DATA = 0x40


class Driver:
    def __init__(self, bus, settings):
        self.bus = bus
        self.width = settings['width']
        self.height = settings['height']
        self.pages = self.height//8
        self.buffer = bytearray(self.pages*self.width)
        self.frame = framebuf.FrameBuffer(self.buffer, self.width, self.height, framebuf.MONO_VLSB)
        self.bus.writeto_mem(COMMANDS, bytes((
            0xae,           # display off
            0xd5, 0x80,     # clock
            0xa8, 0x3f,     # 64 lines
            0xd3, 0x00,     # no display offset
            0x40,           # start line 0
            0xad, 0x8b,     # internal DC-DC converter on
            0xa1,           # column 131 is segment 0
            0xc8,           # scan from COM[N-1]
            0xda, 0x12,     # COM pin layout
            0x81, 0x80,     # contrast
            0xd9, 0x22,     # pre-charge
            0xdb, 0x35,     # VCOM deselect level
            0xa4,           # show the frame
            0xa6,           # not inverted
        )))
        self.frame.fill(0)
        self.show()
        self.bus.writeto_mem(COMMANDS, bytes((0xaf,)))  # display on

    def show(self):
        view = memoryview(self.buffer)
        for page in range(self.pages):
            # The page, then column 2 as its low and high halves.
            self.bus.writeto_mem(COMMANDS, bytes((0xb0 | page, 0x02, 0x10)))
            self.bus.writeto_mem(DATA, view[page*self.width:(page+1)*self.width])
