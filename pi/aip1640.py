"""
AIP1640 (TM1640-clone) 8x16 LED matrix driver for Raspberry Pi.

Bit-banged 2-wire serial protocol — NOT real I2C, despite the silkscreen
labelling "SCL/SDA" on most modules. No slave address, no ACK required.

Default wiring (matches the unit on the claude-monitor Pi):
    CLK (silkscreened SCL) -> GPIO 6  (header pin 31)
    DIN (silkscreened SDA) -> GPIO 5  (header pin 29)
    VCC                    -> 5V or 3.3V (check your module)
    GND                    -> GND

Frame layout: 16 column bytes, one byte per column.
  Bit 0 of a column byte = row 0 (top), bit 7 = row 7 (bottom)
  Column 0 = one physical end; column 15 = the other end
If your physical orientation comes out mirrored or flipped, reverse the
column list and/or bit-reverse each byte in the caller.
"""

import time

try:
    import RPi.GPIO as GPIO
except ImportError as e:
    raise ImportError(
        "RPi.GPIO not available — install with `sudo apt install python3-rpi.gpio` on the Pi."
    ) from e


class AIP1640:
    CMD_DATA_AUTO = 0x40        # data write, auto-increment address
    CMD_DATA_FIXED = 0x44       # data write, fixed address
    CMD_SET_ADDR = 0xC0         # OR with 0..15
    CMD_DISPLAY_ON = 0x88       # OR with brightness 0..7
    CMD_DISPLAY_OFF = 0x80

    def __init__(self, clk_pin=6, din_pin=5, brightness=4):
        self.clk = clk_pin
        self.din = din_pin
        self.brightness = brightness & 0x07
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self.clk, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(self.din, GPIO.OUT, initial=GPIO.HIGH)
        self.clear()
        self._send_cmd(self.CMD_DISPLAY_ON | self.brightness)

    def _start(self):
        GPIO.output(self.din, GPIO.HIGH)
        GPIO.output(self.clk, GPIO.HIGH)
        GPIO.output(self.din, GPIO.LOW)
        GPIO.output(self.clk, GPIO.LOW)

    def _stop(self):
        GPIO.output(self.clk, GPIO.LOW)
        GPIO.output(self.din, GPIO.LOW)
        GPIO.output(self.clk, GPIO.HIGH)
        GPIO.output(self.din, GPIO.HIGH)

    def _write_byte(self, b):
        for i in range(8):
            GPIO.output(self.clk, GPIO.LOW)
            GPIO.output(self.din, GPIO.HIGH if (b >> i) & 1 else GPIO.LOW)
            GPIO.output(self.clk, GPIO.HIGH)
        # 9th clock — chip ACKs here but we don't read it.
        GPIO.output(self.clk, GPIO.LOW)
        GPIO.output(self.din, GPIO.HIGH)
        GPIO.output(self.clk, GPIO.HIGH)

    def _send_cmd(self, b):
        self._start()
        self._write_byte(b)
        self._stop()

    def set_brightness(self, brightness):
        self.brightness = brightness & 0x07
        self._send_cmd(self.CMD_DISPLAY_ON | self.brightness)

    def display_off(self):
        self._send_cmd(self.CMD_DISPLAY_OFF)

    def clear(self):
        self.write_frame([0] * 16)

    def write_frame(self, columns):
        """Push 16 column bytes to the chip. List/tuple of ints, length 16."""
        if len(columns) != 16:
            raise ValueError(f"need 16 column bytes, got {len(columns)}")
        self._send_cmd(self.CMD_DATA_AUTO)
        self._start()
        self._write_byte(self.CMD_SET_ADDR | 0)
        for c in columns:
            self._write_byte(c & 0xFF)
        self._stop()

    def cleanup(self):
        try:
            self.clear()
            self.display_off()
        finally:
            GPIO.cleanup([self.clk, self.din])
