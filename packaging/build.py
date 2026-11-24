"""Build roomfix for Windows: roomfix.exe (PyInstaller, one folder, no console window), a
portable zip of it, and the installer (Inno Setup 6).

    pip install pyinstaller
    python packaging/build.py

Writes dist/roomfix/, dist/roomfix-<version>-portable.zip and dist/roomfix-setup-<version>.exe.
Inno Setup is looked for in its usual places; set ISCC to its ISCC.exe otherwise.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from roomfix import __version__, logo  # noqa: E402

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'roomfix'),
      StringStruct('FileDescription', 'roomfix'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'roomfix'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'roomfix.exe'),
      StringStruct('ProductName', 'roomfix: speaker and room correction'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def iscc():
    candidates = [os.environ.get("ISCC"), shutil.which("iscc"),
                  r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe")]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    sys.exit("Inno Setup 6 was not found: install it, or set ISCC to the path of ISCC.exe")


def main():
    build, dist = ROOT / "build", ROOT / "dist"
    build.mkdir(exist_ok=True)
    icon = build / "roomfix.ico"
    icon.write_bytes(logo.ico())
    numbers = tuple(int(n) for n in __version__.split(".")[:4])
    numbers += (0,) * (4 - len(numbers))
    (build / "version.txt").write_text(VERSION_INFO.format(numbers=numbers, version=__version__), encoding="utf-8")
    shutil.rmtree(dist / "roomfix", ignore_errors=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "roomfix",
        "--icon", str(icon), "--version-file", str(build / "version.txt"),
        "--add-data", f"{ROOT / 'roomfix' / 'web'}{os.pathsep}roomfix/web",
        "--collect-all", "sounddevice", "--collect-all", "_sounddevice_data",
        "--distpath", str(dist), "--workpath", str(build / "pyinstaller"), "--specpath", str(build),
        str(ROOT / "run.py"),
    ], check=True, cwd=ROOT)
    portable = shutil.make_archive(str(dist / f"roomfix-{__version__}-portable"), "zip", dist, "roomfix")
    subprocess.run([iscc(), "/Q", f"/DVersion={__version__}", f"/DSource={dist / 'roomfix'}", f"/DIcon={icon}",
                    f"/O{dist}", str(ROOT / "packaging" / "roomfix.iss")], check=True)
    print("built:", dist / "roomfix" / "roomfix.exe", portable, dist / f"roomfix-setup-{__version__}.exe", sep="\n  ")


if __name__ == "__main__":
    main()
