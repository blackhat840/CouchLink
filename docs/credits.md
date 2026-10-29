# Credits

The projects CouchLink builds on, related projects, and trademarks.

## Projects CouchLink builds on

CouchLink wouldn't work without these:

- **[libcec](https://github.com/Pulse-Eight/libcec)** by Pulse-Eight, which
  provides `cec-client`. CouchLink does all of its CEC talking through
  `cec-client`, which it runs as a separate program rather than linking to.
  libcec is licensed under the GPL, version 2 or later. On Raspberry Pi OS it
  comes from the Debian `cec-utils` package.
- **The Linux kernel's USB gadget framework** (configfs, `libcomposite` and the
  `dwc2` driver), which lets the Pi act as a USB keyboard. The keyboard's HID
  report descriptor in `install.sh` is the standard boot keyboard descriptor
  from the kernel documentation (`Documentation/usb/gadget_hid.rst`).
- **[PiKVM](https://github.com/pikvm)**, whose kernel patch adds USB remote
  wakeup to the Pi's `dwc2` driver and the `wakeup_on_write` setting that the
  keyboard script turns on when it is available. CouchLink does not include the
  patch. It only uses the setting if your kernel has it.

## Related projects

- **[steamos-cec-toolkit](https://github.com/Twsts/steamos-cec-toolkit)**
  adds console-style CEC behavior by running on the SteamOS PC itself, using a
  CEC-capable display adapter. CouchLink takes the opposite approach: nothing
  is installed on the PC and no special adapter is needed, at the cost of a
  Raspberry Pi.

## Trademarks

CouchLink is an independent project and is not affiliated with, endorsed by,
or sponsored by any of the companies below. All names are used only to
describe compatibility.

HDMI is a trademark of HDMI Licensing Administrator, Inc. Steam and SteamOS
are trademarks of Valve Corporation. Xbox is a trademark of Microsoft
Corporation. Raspberry Pi is a trademark of Raspberry Pi Ltd. Vizio, Samsung,
Anynet+, Google, Gigabyte and Pulse-Eight are trademarks of their respective
owners. Bazzite is a project of Universal Blue.

---

Back to the [Documentation](README.md) or the [README](../README.md).
