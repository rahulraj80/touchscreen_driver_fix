# Hardware profile — FTSC1000 touchscreen (verified on rajtab, 2026-10-29)

Everything here was read off the running machine. Re-verify with the commands
at the bottom after a kernel update.

## Identity

| Item | Value |
|---|---|
| Panel | ASUS UX360CA 13.3" convertible, active area **290 x 163 mm** |
| ACPI device | `FTSC1000:00` (PNP0C50), i2c bus 0, `i2c_hid_acpi` |
| HID | `0018:2808:5112` (bus I2C, vendor 0x2808 FocalTech, product 0x5112) |
| Driver stack | `i2c_hid` -> `hid-multitouch` -> `/dev/input/event4` (`mouse0`), hidraw **`/dev/hidraw0`** |
| input node | `/sys/class/input/input6`, name `FTSC1000:00 2808:5112` |
| properties | `INPUT_PROP_DIRECT` (real touchscreen, not a pointer) |
| quirks | `334864` (hid-multitouch quirk bitfield, kernel-default) |
| **Touchpad (do not touch)** | `ELAN1200:00 04F3:301A`, hidraw1, `/dev/input/event6` — separate device, same i2c bus |

Report descriptor: 454 bytes, dumped to `/tmp/ftsc-rd.bin` from
`/sys/bus/hid/devices/0018:2808:5112.0002/report_descriptor`.

## Report format (report ID 1, per frame)

4 finger slots, then per-frame trailer:

| Field | Bits | Range | Notes |
|---|---|---|---|
| TipSwitch | 1 | 0/1 | contact active |
| (padding) | 7 | — | constant |
| ContactID | 8 | 0..255 | tracking id |
| X | 16 | logical 0..**3200** | physical max 2952 |
| Y | 16 | logical 0..**1800** | physical max 1667 |
| Width | 8 | 0..255 | contact area |
| Height | 8 | 0..255 | contact area |
| ScanTime | 16 | 0..65535 | per-frame scan stamp |
| ContactCount | 8 | 0..16 | contacts in this frame |

Report ID 2 = `ContactCountMaximum` feature (max 10).
Report ID 5 = vendor feature page `0xFF00`/`0xC5`, **256 bytes** (FocalTech
debug / mass-production interface — untouched, see plan P4b).
Report ID 6 = vendor input/output, 63 bytes (unexplored).

## Consequences that shape the design

1. **There is no pressure axis and no confidence bit.** Filtering must come
   from geometry (Width/Height), position, timing, and contact lifecycle.
   This is the single most important constraint.
2. Coordinates are ~11 units/mm (3200/290 = 11.03), so a 2 mm ghost has a
   ~22-unit footprint — plenty of resolution for a spatial mask.
3. `ContactCount` per frame gives a cheap consistency check against the number
   of slots with TipSwitch set.
4. `ScanTime` lets us measure the real report interval instead of trusting
   wall-clock deltas.
5. The device is `hid-multitouch` + `i2c-hid`: **both are upstream generic
   drivers**. Anything we add there is a fork we must carry forever — which is
   why HID-BPF is the recommended carrier (see plan).

## Platform facts

- Kernel **7.0.12-201.fc44** running; 7.2.13-200 also installed (next boot may
  pick it up). `kernel-devel` for the running kernel is installed; `dkms` and
  `akmods` are not.
- **Secure Boot disabled, platform in Setup Mode** — unsigned modules load.
- `CONFIG_HID_BPF=y`, `CONFIG_DEBUG_INFO_BTF=y`, `CONFIG_UHID=m`,
  `CONFIG_HID_MULTITOUCH=m`, `CONFIG_I2C_HID=y`.
  `/sys/kernel/btf/hid_multitouch` exists; `hid_bpf_ops` struct_ops present.
- **`udev-hid-bpf` 2.2.0 is installed** with
  `/usr/lib/udev/rules.d/81-hid-bpf.rules` and the `hid-bpf:` hwdb namespace —
  i.e. a BPF fix can auto-attach at device bind with no custom service.
- Fedora 44, KDE Plasma 6.7.5 on **Wayland** (`libinput`, no XInput, no
  xorg.conf). logind does **not** ACL the touchscreen node.
- `/dev/input/event4` is `root:input 0660`; `rahul` is not in `input`;
  `/dev/hidraw0` is `0600 root`. `kernel.dmesg_restrict=1`.
- No `i2c-hid` errors, no ACPI errors, and **zero suspend/resume cycles in the
  sampled boot** — the phantoms are not (currently) a resume-path bug.

## Re-verify

```sh
cat /proc/bus/input/devices | sed -n '/FTSC1000/,/^$/p'
udevadm info /dev/input/event4 | grep -E 'ID_INPUT_(TOUCHSCREEN|WIDTH|HEIGHT)'
cat /sys/bus/hid/devices/0018:2808:5112.0002/report_descriptor | od -An -tx1
ls /sys/kernel/btf/ | tr ' ' '\n' | grep -E 'hid_multitouch|i2c_hid'
grep -E 'CONFIG_HID_BPF|CONFIG_UHID|CONFIG_HID_MULTITOUCH' /boot/config-$(uname -r)
udev-hid-bpf list-devices
```

## Report descriptor facts (verified 2026-10-30)

`/sys/bus/hid/devices/0018:2808:5112.0002/report_descriptor` is 454 bytes and is
readable without root. Report ID 1 carries the touch frame (60 bytes on the
wire). The digitizer declares **Logical Maximum 3200 for X and 1800 for Y**
(16:9); `0x0fff` (4095) is out of range for both axes and is used as the
"no contact" marker in inactive slots. Per-slot fields are TipSwitch,
ContactID, X, Y, Width, Height; there is **no pressure and no confidence**
field. For a 1920x1080 panel the raw-to-pixel scale is therefore a uniform
0.6 on both axes — do **not** scale against 4095.

`tools/analyze.py` defaults to 3200x1800 for exactly this reason; override with
`--raw-x-max/--raw-y-max` only if a different panel is ever fitted.
