"""Configure the NeoKey Trinkey's USB interfaces.

The first CDC interface remains the CircuitPython console.  The second is a
dedicated data channel used by the Raspberry Pi web controller.
"""

import usb_cdc
import usb_hid
import usb_midi


# NeoKey's SAMD21 has a small USB endpoint budget.  HID and MIDI are not needed
# for this light controller, so make room for the dedicated CDC data channel.
usb_hid.disable()
usb_midi.disable()
usb_cdc.enable(console=True, data=True)
