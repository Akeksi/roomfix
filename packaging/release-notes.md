roomfix as a Windows program: install it, start it from the Start menu.

**Download `roomfix-setup-0.2.2.exe`** and run it. It installs roomfix for your Windows user
(no administrator rights needed) with a Start menu entry. roomfix opens its page in your
browser. Windows may warn that the installer is from an unknown publisher (it is not code
signed): choose "More info", then "Run anyway". Without installing:
`roomfix-0.2.2-portable.zip`, unpack it and run `roomfix.exe`.

**Updating from 0.2.1:** run the new installer. It stops roomfix, replaces it and starts the
correction again; your measurements and saved corrections stay as they are.

You also need [VB-CABLE](https://vb-audio.com/Cable/) (for the CamillaDSP engine) and a
measurement microphone. See the README for the setup.

## New in this version

- **No more short dropouts.** The background process restarted an engine as soon as it missed
  a single answer from it, which can happen on a busy computer or when a security program
  resets a local connection. Each restart cut the sound for a moment, every half hour to
  three hours. Now an engine is restarted only when it has not answered for 10 seconds and
  three questions in a row.
- An engine whose control port is taken by another program gets a new port, instead of
  being started again on the same port.

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
