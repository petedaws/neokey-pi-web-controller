"""USB-controlled NeoPixel firmware for the Adafruit NeoKey Trinkey M0."""

import time

import board
import digitalio
import neopixel_write
import usb_cdc


pixel = digitalio.DigitalInOut(board.NEOPIXEL)
pixel.direction = digitalio.Direction.OUTPUT
pixel_buffer = bytearray(3)

# Safe, useful defaults while the Pi controller is starting.
red = 255
green = 255
blue = 255
brightness = 25
flashing = False
flash_rate = 2.0
light_on = True
next_change = 0.0

data = usb_cdc.data
data.timeout = 0
receive_buffer = bytearray()


def show_pixel(enabled):
    """Write one GRB pixel, applying the current brightness percentage."""
    if enabled:
        pixel_buffer[0] = (green * brightness) // 100
        pixel_buffer[1] = (red * brightness) // 100
        pixel_buffer[2] = (blue * brightness) // 100
    else:
        pixel_buffer[0] = 0
        pixel_buffer[1] = 0
        pixel_buffer[2] = 0
    neopixel_write.neopixel_write(pixel, pixel_buffer)


def reply(message):
    try:
        data.write(bytes(message + "\n", "ascii"))
    except OSError:
        pass


def apply_command(line):
    """Accept: SET <r> <g> <b> <brightness%> <flash 0|1> <rate Hz>."""
    global red, green, blue, brightness, flashing, flash_rate
    global light_on, next_change

    try:
        parts = bytes(line).strip().split()
        if len(parts) != 7 or parts[0] != b"SET":
            raise ValueError("syntax")

        new_red = int(parts[1])
        new_green = int(parts[2])
        new_blue = int(parts[3])
        new_brightness = int(parts[4])
        new_flashing = int(parts[5])
        new_rate = float(parts[6])

        if not (0 <= new_red <= 255 and 0 <= new_green <= 255):
            raise ValueError("rgb")
        if not (0 <= new_blue <= 255 and 0 <= new_brightness <= 100):
            raise ValueError("rgb/brightness")
        if new_flashing not in (0, 1) or not (0.1 <= new_rate <= 20.0):
            raise ValueError("flash")

        red = new_red
        green = new_green
        blue = new_blue
        brightness = new_brightness
        flashing = bool(new_flashing)
        flash_rate = new_rate
        light_on = True
        next_change = time.monotonic() + (0.5 / flash_rate)
        show_pixel(True)
        reply("OK {} {} {} {} {} {:.2f}".format(
            red, green, blue, brightness, int(flashing), flash_rate
        ))
    except (ValueError, UnicodeError):
        reply("ERR expected: SET r g b brightness flash rate")


show_pixel(True)

while True:
    waiting = data.in_waiting
    if waiting:
        chunk = data.read(min(waiting, 64))
        if chunk:
            receive_buffer.extend(chunk)
            while b"\n" in receive_buffer:
                newline = receive_buffer.index(b"\n")
                command = receive_buffer[:newline]
                receive_buffer = receive_buffer[newline + 1 :]
                apply_command(command)
            if len(receive_buffer) > 160:
                receive_buffer = bytearray()
                reply("ERR command too long")

    now = time.monotonic()
    if flashing:
        if now >= next_change:
            light_on = not light_on
            show_pixel(light_on)
            next_change = now + (0.5 / flash_rate)
    elif not light_on:
        light_on = True
        show_pixel(True)

    time.sleep(0.005)
