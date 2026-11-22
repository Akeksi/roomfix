# roomfix

Fully automatic speaker and room correction for Windows. One button measures
your speakers, computes the correction and applies it to everything you play.

- **Room correction** to a target curve you choose (not just flat), with the
  measured, target and corrected curves on screen.
- **Time alignment** of speakers, and of the drivers inside each speaker.
- **Active 2-way and 3-way crossovers** on a multichannel DAC (e.g. Topping
  DM7), with driver protection built in.
- **Subwoofer** with bass management, level, delay and polarity set
  automatically, even when it hangs on a **different output device** than the
  speakers (say, speakers on the TV over HDMI and the subwoofer on a USB DAC).
  The delay between the two devices is measured and compensated.
- Needs only a DAC and a measurement microphone (calibration file supported).

![roomfix result page: before, after and target curves](docs/screenshot.png)

## Install

1. Download `roomfix.exe` from [Releases](../../releases) and run it. A page
   opens in your browser.
   (Or from source: `pip install .` then `roomfix`.)
2. Pick a playback engine:
   - **CamillaDSP** (default, required for active crossovers). roomfix
     downloads and runs it for you. Install [VB-CABLE](https://vb-audio.com/Cable/)
     and make "CABLE Input" the default Windows output; roomfix sends that
     audio, corrected, to the DAC. Set the cable and the DAC to 48 kHz in
     Windows sound settings.
   - **Equalizer APO** (passive speakers only). Install it and enable it for
     the DAC. No virtual cable needed.

No hardware at hand? `roomfix --demo` runs everything against simulated speakers.

## Use

1. **Setup**: choose DAC, microphone and calibration file, and say which DAC
   channel drives which speaker (or driver).
2. **Measure and correct**: place the microphone at the listening position
   and press the button. roomfix sets the levels, measures every driver,
   designs the correction, measures the result and switches it on.
3. **Result**: adjust the target curve (tilt, bass boost, limits, or your own
   curve) or the crossover; the correction updates instantly, without
   measuring again. Press **Apply correction** to use it.

Using several microphone positions (Setup) makes the correction valid for a
wider listening area.

## Subwoofer and a second output device

Tick **Use a subwoofer** in Setup and say where it is connected: a free channel
of the DAC, or another output device. Every speaker is then high-passed at the
subwoofer crossover and the subwoofer plays the bass of all of them.

Two output devices never have the same latency (an HDMI TV is easily 70 ms
slower than a USB DAC), so music played to both arrives out of step. roomfix
runs one playback engine per device, plays a timing chirp to a speaker and a
sweep to the subwoofer through the system audio path, exactly as music will
travel, and delays the faster device by the difference. Afterwards it measures
each speaker together with the subwoofer through the running engines.

- The subwoofer has its own "loudest allowed test signal", because its
  amplifier is separate from the speakers'.
- After measuring, leave the Windows volume of both output devices and the
  volume controls of TV, amplifier and subwoofer where they are: they set the
  balance between speakers and subwoofer. Listen louder or quieter with the
  volume keys of the keyboard, which roomfix takes over (see below).
- After the engines start (at login, when you press Apply, or when a device
  returns), the devices need up to a minute to settle into exact alignment.
- Known limit: the latency of an output device can differ by a few
  milliseconds from one engine start to the next. roomfix measures the result
  through the running engines and warns if subwoofer and speakers do not add
  up as predicted at the crossover.

### TV App source: the TV's own apps, with the subwoofer

When the TV plays its own apps (streaming, antenna) on its own speakers, the PC
never sees that sound. If the TV's optical output is connected to the S/PDIF
input of a sound card on the PC (for example a Sound BlasterX G5), roomfix can
still add the subwoofer:

1. In Setup, under the subwoofer, choose the **TV's S/PDIF input** and press
   **Apply correction**. The source switch appears at the top of the page, in
   the tray icon's menu and on the phone page.
2. On the TV, set the digital (optical) audio output to **PCM**, not Dolby,
   DTS or "auto"/bitstream, and keep the TV's speakers on.
3. Choose the source **TV App**. roomfix stops the engines that play the PC's
   sound (so the TV is no longer fed by the PC) and runs one engine that plays
   only the subwoofer, from left and right of the S/PDIF input. Choose **PC**
   to go back to the usual chain.

In the TV App source the subwoofer gets the measured correction: the speaker
EQ in the bass, the level and crossover filter, and the polarity. It does not
get the measured delay, which lines the subwoofer up with the PC's path to the
TV. The TV's speakers play without waiting for roomfix, so the subwoofer can
only be later than them: its engine runs with small buffers (256-sample chunks)
to keep that small, and a rate adjuster follows the TV's clock. **The timing
between the TV's speakers and the subwoofer is not measured in this source.**
Set it by ear in Setup: an extra subwoofer delay (0 to 50 ms), the polarity
(try both with a steady bass line; keep the fuller one) and a level for this
source only. If the bass lags behind the TV's speakers, the TV's own AV sync
setting may be able to delay its speakers. Changes apply with **Apply
correction**.

Volume, mute and on/off are the same for both sources, and the chosen source is
remembered across restarts. When nothing has arrived at the S/PDIF input for
10 seconds, the tray icon turns red and the pages say "no sound from the TV":
the TV is off or paused, or it sends Dolby instead of PCM. The S/PDIF input is
followed when Windows renames it ("SPDIF In (2- G5)" to "SPDIF In (3- G5)"),
like the output devices.

Test signals are played in Windows shared mode, so the bit depth and sample
rate a device is set to do not matter for measuring. In a noisy room, the
subwoofer is only corrected down to the frequency where it is louder than the
room's own rumble.

## Always on, and volume

With CamillaDSP, a small background process (`roomfix --engine`) looks after the
correction. roomfix starts it when you press Apply and, with "Start correction
with Windows", at login. The roomfix page does not need to be open.

- If an output device disappears (a TV that is switched off, a DAC that is
  unplugged), its engine stops. The background process waits for the device and
  starts the engine again within a few seconds of its return, also when Windows
  has renamed the device meanwhile ("1 - LG TV" to "2 - LG TV"). An engine that
  fails for another reason is restarted too, with growing pauses.
- **Volume keys.** The Windows volume of a virtual cable does nothing useful, so
  while the correction is on, the volume and mute keys of the keyboard set
  roomfix's own volume (2 dB a press, 1 dB with Shift) and show it on screen.
  The volume is the same on all output devices, is remembered, and never goes
  above 0 dB. The page has a slider and a mute button for the same volume.
- **Icon in the notification area** (bottom right). Its colour shows the state:
  on, muted, waiting for a device, or off. Click it for the menu: mute, volume,
  open roomfix, restart the engines, switch the correction off and on, give the
  volume keys back to Windows, open the log folder, or close it all. With the
  TV App source set up, the menu also switches between PC and TV App.
- **Phone.** The background process also serves a page for the phone at
  `http://127.0.0.1:8136/`: volume slider, ± buttons, mute, on/off, the source (if
  TV App is set up) and the state of each output device. To reach it from your phone, put the PC and the phone in the
  same [Tailscale](https://tailscale.com) network and run, once, on the PC:
  `tailscale serve --bg 8136`. Then open `https://<pc>.<tailnet>.ts.net` on the
  phone and add it to the home screen. Changes made there show on the PC's screen
  like a volume key. Only this computer's own names and Tailscale names (`*.ts.net`)
  are answered, so other websites cannot reach it; "close it all" stays on the PC.
- Hotkey scripts can use the same API (the port is in
  `%APPDATA%\roomfix\engine.json`, normally 8136), with `Content-Type: application/json`:
  `POST /api/volume {"value": -20}` (add `"unmute": true` to unmute too) or
  `{"delta": -2}`, `POST /api/mute {"mute": true}` (or `{}` to toggle),
  `POST /api/restart {}`, `POST /api/power {"on": false}`,
  `POST /api/source {"name": "tv_app"}` (or `"pc"`), `GET /api/status`.
  Absolute values are safe to send again when a request got no answer.

## Active crossovers and driver safety

For each driver above the woofer you enter its **lowest safe crossover
frequency** (from the data sheet). Then:

- Every test signal for that driver passes a 24 dB/oct high-pass at that
  frequency. A tweeter never receives a full-range sweep.
- A crossover below that frequency is refused.
- Before the first measurement, a wiring check plays a quiet hiss that is
  harmless for any driver, one channel at a time, and asks you to confirm
  that each came from the right driver.
- Levels start very low and rise only until the microphone hears enough,
  never above the limit set in Setup.
- With CamillaDSP, audio reaches the DAC only through the crossover: if the
  engine is not running, the speakers are silent.

Still: keep the amplifier volume low for the first run, and remember that no
software can protect against a miswired amplifier. A capacitor in series with
each tweeter is cheap insurance.

## How it works

| Step | Method |
|---|---|
| Measure | Log sine sweep per driver. A short chirp from one reference driver precedes each sweep, giving exact relative timing without a loopback cable. |
| Crossover | Linkwitz-Riley 12/24/48 dB/oct. Driver levels are matched; delay and polarity are chosen so the drivers sum in phase at the crossover. |
| Speakers | Delay and level are set so all speakers arrive together and equally loud. A speaker wired in reverse is detected and fixed. |
| Subwoofer | Linkwitz-Riley 24 dB/oct against all speakers. Level matched to the speakers; delay and polarity chosen so that it adds up with them at the crossover. |
| Two devices | The latency of a CamillaDSP engine is its chunk plus its buffer level; all engines are held at the same level. The remaining difference between the devices is measured through the engines and removed with a delay. |
| EQ | Up to 20 peaking filters per speaker. Peaks are cut precisely; only broad dips are filled; boost is limited; nothing is corrected outside the speaker's range. |
| Apply | Written as a CamillaDSP or Equalizer APO configuration. Exported filters are tested against the built-in simulation to be identical. |

Settings and measurements are stored in `%APPDATA%\roomfix`.

## Limits

- Stereo or multi-speaker setups with 1 to 3 ways each, plus one subwoofer.
- Only the subwoofer can be on a second output device, and it needs the CamillaDSP engine.
- 48 kHz processing, minimum-phase (IIR) correction.

## Development

```
pip install -e .[dev]
pytest
```

The tests run the whole pipeline against a simulated room (`roomfix/sim.py`).
MIT license. CamillaDSP and Equalizer APO are separate projects under their own licenses.
