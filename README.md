# roomfix

Fully automatic speaker and room correction for Windows. One button measures
your speakers, computes the correction and applies it to everything you play.

- **Room correction** to a target curve you choose (not just flat), with the
  measured, target and corrected curves on screen.
- **Time alignment** of speakers, and of the drivers inside each speaker.
- **Active 2-way and 3-way crossovers** on a multichannel DAC (e.g. Topping
  DM7), with driver protection built in.
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
| EQ | Up to 20 peaking filters per speaker. Peaks are cut precisely; only broad dips are filled; boost is limited; nothing is corrected outside the speaker's range. |
| Apply | Written as a CamillaDSP or Equalizer APO configuration. Exported filters are tested against the built-in simulation to be identical. |

Settings and measurements are stored in `%APPDATA%\roomfix`.

## Limits

- Stereo or multi-speaker setups with 1 to 3 ways each; no subwoofer bass management yet.
- 48 kHz processing, minimum-phase (IIR) correction.

## Development

```
pip install -e .[dev]
pytest
```

The tests run the whole pipeline against a simulated room (`roomfix/sim.py`).
MIT license. CamillaDSP and Equalizer APO are separate projects under their own licenses.
