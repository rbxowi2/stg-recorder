"""Minimal driver for an SSD1306 128x64 OLED over I2C.

Uses only `smbus2` and `PIL`, both already present on the Pi. luma.oled is
deliberately not used: the only feature needed is pushing a 1-bit image, which
takes under 100 lines, and every extra dependency is another way deployment
can fail.

    from pi.oled import Ssd1306
    d = Ssd1306()
    d.show(img)          # PIL mode "1", 128x64
    d.off()

## Measured performance

    full frame update   96.7 ms (20 runs, worst 97.4)
    of which packing     1.7 ms  -> the remaining 95 ms is I2C transfer

At the default 100 kHz I2C clock, 1024 bytes plus an ack bit per byte take
about 92 ms, which matches. A video frame is 33 ms, so drawing cannot be
synchronous; the screen is updated from a background thread (see
`pi/oled_ui.py`). Adding `dtparam=i2c_arm_baudrate=400000` to `config.txt`
would bring it to about 24 ms but requires a boot configuration change.
"""
from __future__ import annotations

from PIL import Image

W, H = 128, 64
_CMD, _DATA = 0x00, 0x40


class Ssd1306:
    def __init__(self, addr=0x3C, bus_no=1):
        from smbus2 import SMBus                 # imported only when actually used
        self.addr = addr
        self.bus = SMBus(bus_no)
        self._init_panel()

    def _cmds(self, *cs):
        for c in cs:
            self.bus.write_byte_data(self.addr, _CMD, c)

    def _init_panel(self):
        self._cmds(
            0xAE,                    # display off
            0xD5, 0x80,              # clock div
            0xA8, H - 1,             # multiplex
            0xD3, 0x00,              # display offset
            0x40,                    # start line 0
            0x8D, 0x14,              # charge pump on; without it the screen stays black
            0x20, 0x00,              # horizontal addressing
            0xA1,                    # segment remap
            0xC8,                    # reversed COM scan; with 0xA1 sets the orientation
            0xDA, 0x12,              # COM pins
            0x81, 0xCF,              # contrast
            0xD9, 0xF1,              # precharge
            0xDB, 0x40,              # vcom
            0xA4,                    # show RAM contents (not the all-on test mode)
            0xA6,                    # normal display (not inverted)
            0xAF)                    # display on

    @staticmethod
    def pack(img: Image.Image) -> bytes:
        """PIL 1-bit image -> SSD1306 page format (each byte is 8 vertical pixels).

        Separate from sending so the caller can pack, compare with the previous
        frame, and skip the transfer: packing takes 1.7 ms, sending 95 ms.
        """
        px = img.load()
        buf = bytearray()
        for page in range(H // 8):
            for x in range(W):
                b = 0
                for bit in range(8):
                    if px[x, page * 8 + bit]:
                        b |= 1 << bit
                buf.append(b)
        return bytes(buf)

    def blit(self, buf: bytes) -> None:
        from smbus2 import i2c_msg
        self._cmds(0x21, 0, W - 1, 0x22, 0, H // 8 - 1)
        for i in range(0, len(buf), 128):
            # i2c_msg allows multi-byte writes; write_i2c_block_data is limited to 32 bytes
            self.bus.i2c_rdwr(i2c_msg.write(self.addr,
                                            bytes([_DATA]) + buf[i:i + 128]))

    def show(self, img: Image.Image) -> None:
        self.blit(self.pack(img))

    def contrast(self, v: int) -> None:
        """0-255. Lowered while asleep (OLED power is almost entirely lit pixels)."""
        self._cmds(0x81, max(0, min(255, int(v))))

    def off(self) -> None:
        self._cmds(0xAE)

    def close(self) -> None:
        try:
            self.off()
        finally:
            self.bus.close()
