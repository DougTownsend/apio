# CD74HC4094E Shift Register Test

A test project that drives an 8-bit shift register, rotating a single output bit left every second.

## Hardware Setup

Connect a CD74HC4094E shift register to the Pico:

- Pico GPIO 2 → Shift register CP (clock)
- Pico GPIO 3 → Shift register SI (serial data input)
- Pico GPIO 4 → Shift register Strobe (latch enable)

Connect the 8 output bits (Q0–Q7) to LEDs or logic analyzer to observe the pattern.

## Behavior

The firmware uses the Pico's internal 1kHz clock to time a state machine:

1. Wait 1 second (1000 cycles of 1kHz clock)
2. Shift an 8-bit pattern into the register: a single 1 bit at position `n`, surrounded by 0s
3. Strobe/latch the outputs
4. Increment `n` (0→1→2→...→7→0) and repeat

Expected output pattern (at Q0–Q7):

```
Time  Q7 Q6 Q5 Q4 Q3 Q2 Q1 Q0
0s    0  0  0  0  0  0  0  1   (bit 0 on)
1s    0  0  0  0  0  0  1  0   (bit 1 on)
2s    0  0  0  0  0  1  0  0   (bit 2 on)
...
7s    0  0  0  0  1  0  0  0   (bit 7 on)
8s    0  0  0  0  0  0  0  1   (wrap back to bit 0)
```

## Building and Flashing

```bash
cd experimental/pico-shift-register
apio build
apio upload
```

The first upload requires holding BOOTSEL while plugging in the Pico. Subsequent uploads are automatic (the firmware listens for a reboot command over USB-serial).
