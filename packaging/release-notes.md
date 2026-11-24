The first release of roomfix as a Windows program: install it, start it from the Start menu.

**Download `roomfix-setup-0.2.1.exe`** and run it. It installs roomfix for your Windows user
(no administrator rights needed) with a Start menu entry. roomfix opens its page in your
browser. Windows may warn that the installer is from an unknown publisher (it is not code
signed): choose "More info", then "Run anyway". Without installing:
`roomfix-0.2.1-portable.zip`, unpack it and run `roomfix.exe`.

You also need [VB-CABLE](https://vb-audio.com/Cable/) (for the CamillaDSP engine) and a
measurement microphone. See the README for the setup.

## What it does

- Measures your speakers (and subwoofer) with one button, computes the room correction,
  time alignment, crossovers and subwoofer integration, and applies it to everything
  Windows plays.
- A background process keeps the correction running, also after a device was switched off
  and on, with its own volume on the keyboard's volume keys and an icon in the notification
  area.
- **Saved corrections**: every applied correction is kept; name the ones you like and switch
  between them without a pause, from the page, the tray icon or the phone.
- **Changes by hand**: level, delay and polarity of each speaker, driver and the subwoofer,
  on top of what was measured.
- **Phone page** (over Tailscale): volume, mute, on/off, source, saved corrections.
- **TV App source**: what the TV's own apps play, from its optical output, through the same
  speakers and correction; or, when the speakers are the TV's own, roomfix adds the
  subwoofer.

## New in this version

- roomfix.exe and an installer, with the roomfix logo; no console window.
- A sharp notification-area icon on scaled screens (150 %, 250 %).
- The TV App source plays the same speakers as the PC source, with the whole correction.
- A new measurement starts near the test levels of the one before: no long quiet start.
- Speaker polarity is only flipped when the measurement is clear about it.
