# Installing Coppernicus

There are two ways to run Coppernicus:

- **The packaged app.** A macOS disk image (`.dmg`, Apple silicon) or a Windows `.zip`. It
  needs no Python and no uv. It contains everything except the Triangle mesher, which it
  downloads once on its first start (see [Triangle](#the-triangle-mesher) below).
- **From source, with uv.** See [Install](../README.md#install) in the README. Nothing below
  applies to that: uv installs Triangle with everything else.

The builds are not signed with an Apple or Microsoft certificate yet, so the first start asks
you to confirm, as described below.

## macOS

Requirements: a Mac with Apple silicon (M1 or later) and macOS 14 or later. Intel Macs are not
supported: Triangle publishes no Intel macOS wheel for the version Coppernicus pins.

1. Download `Coppernicus-<version>-macos-arm64.dmg` and open it.
2. Drag **Coppernicus** onto **Applications**.
3. Copy the **Examples** folder somewhere you can write, for example your Documents folder.
   Coppernicus writes its results into a `results` folder next to a project file, and it cannot
   write inside the disk image or inside the app.
4. Start Coppernicus from Applications. The app is not notarized by Apple, so macOS stops it
   the first time:
   - **macOS 15 (Sequoia) and later:** macOS says that Coppernicus was not opened. Click
     **Done**, open **System Settings → Privacy & Security**, scroll down to the message about
     Coppernicus and click **Open Anyway**. Confirm with your password, then click **Open**.
   - **macOS 14:** right-click (or Control-click) Coppernicus in Applications, choose
     **Open**, then click **Open** in the dialog.

   macOS remembers the choice: later starts open directly.

The command line and the MCP server are inside the app:

```sh
/Applications/Coppernicus.app/Contents/MacOS/Coppernicus --version
/Applications/Coppernicus.app/Contents/MacOS/Coppernicus solve ~/Documents/Examples/power_chain/power_chain.coppernicus.yml --html
/Applications/Coppernicus.app/Contents/MacOS/Coppernicus mcp
```

## Windows

Requirements: 64-bit Windows 10 or 11 on an x64 processor.

> The Windows build is made by the release workflow but has not been tried on a Windows
> computer yet.

1. Download `Coppernicus-<version>-windows-x64.zip` and extract it into a folder of your own,
   for example `C:\Users\<you>\Coppernicus`. Do not run it from inside the zip.
2. Copy the `examples` folder somewhere you can write if you extracted into a protected
   folder: results are written next to a project file.
3. Start **Coppernicus.exe**. The program is not signed, so Microsoft Defender SmartScreen may
   show "Windows protected your PC": click **More info**, then **Run anyway**.

For the command line and the MCP server use **coppernicus-cli.exe** in the same folder (the
app itself has no console window):

```bat
coppernicus-cli.exe --version
coppernicus-cli.exe solve examples\power_chain\power_chain.coppernicus.yml --html
coppernicus-cli.exe mcp
```

## The Triangle mesher

Coppernicus meshes copper with [Triangle](https://www.cs.cmu.edu/~quake/triangle.html), by
Jonathan Richard Shewchuk, through the [`triangle`](https://pypi.org/project/triangle/)
Python package. Triangle's licence does not allow it to be distributed as part of Coppernicus,
but it allows telling you how to get it free of charge. So the packaged app does not contain
it, and on its first start:

1. it shows "Downloading the Triangle mesher from PyPI (once)…" with a short licence note
   (the **Triangle's licence…** button shows the full terms);
2. it downloads the `triangle` wheel for its Python and your system from PyPI, the exact
   version and file that Coppernicus's `uv.lock` pins (about 1.4 MB);
3. it checks the file's SHA-256 against the hash the build embedded, and refuses any other
   file;
4. it unpacks it into a folder of your own, in one step, so that a failed or interrupted
   download leaves nothing behind:
   - macOS: `~/Library/Application Support/Coppernicus/site-packages`
   - Windows: `%LOCALAPPDATA%\Coppernicus\site-packages`

After that Coppernicus works offline. The command line does the same on its first use, with
progress lines on the standard error stream.

**One copy, checked at every start.** Each start reads the marker file
`.coppernicus-triangle.json` in that folder (the version installed and the SHA-256 of the wheel
it came from) and checks the SHA-256 of every installed file against the list the wheel
carries (its `RECORD`). This takes milliseconds and needs no network. The dialog and the
download come back only when the pinned version is missing or fails this check. When an update
of Coppernicus pins another version of `triangle`, the new one is installed in one step and the
old version's files are deleted: versions never pile up. Anything else found in or beside the
folder (a damaged or partial copy, a download that was interrupted) is deleted at start.

**The folder and its size.** About 4.5 MB on disk (81 files: the `triangle` package, with
its example meshes, and its `triangle-<version>.dist-info`), from a 1.4 MB download. Beside it,
in the same `Coppernicus` folder, the app keeps matplotlib's font cache (`matplotlib`, under
1 MB) and a zero-byte lock file used while installing.

**If the download fails** (no network, a proxy, PyPI unreachable), Coppernicus says why and
installs nothing. Check the connection and click **Retry**, or start Coppernicus again later.
The download uses the system's proxy settings and honours the `HTTPS_PROXY` variable.

**A computer without internet access.** Download the wheel named in the error message from
[PyPI](https://pypi.org/project/triangle/#files) on another computer, copy it over, and start
Coppernicus once with the variable `COPPERNICUS_TRIANGLE_URL` set to that file:

```sh
COPPERNICUS_TRIANGLE_URL=file:///Users/you/Downloads/triangle-20250106-cp312-cp312-macosx_11_0_arm64.whl \
  /Applications/Coppernicus.app/Contents/MacOS/Coppernicus
```

```bat
set COPPERNICUS_TRIANGLE_URL=file:///C:/Users/you/Downloads/triangle-20250106-cp312-cp312-win_amd64.whl
Coppernicus.exe
```

The SHA-256 is checked whatever the source. `COPPERNICUS_SITE_PACKAGES` moves the folder
Triangle goes into (it is used by the tests).

**Removing Triangle.** Run the app with `--remove-triangle`:

```sh
/Applications/Coppernicus.app/Contents/MacOS/Coppernicus --remove-triangle
```

```bat
coppernicus-cli.exe --remove-triangle
```

It deletes the folder (and anything an interrupted download left) and says how much it freed.
Deleting the `site-packages` folder by hand does the same. Coppernicus downloads Triangle again
on its next start. To remove Coppernicus completely, delete the app (or the extracted folder),
that `Coppernicus` folder (it also holds matplotlib's font cache), and the app's settings (macOS:
`~/Library/Preferences/com.coppernicus.Coppernicus.plist`; Windows: the registry key
`HKEY_CURRENT_USER\Software\Coppernicus`).

## Licences

The disk image and the zip hold `LICENSE` (Coppernicus, GPL-3.0-or-later) and
`THIRD_PARTY_NOTICES.txt`: every library in the bundle with its version and licence, and their
licence texts (Qt and PySide6 under LGPL-3.0, VTK, NumPy, SciPy, shapely, KiPI...). The same
files are inside the app, with `licenses/TRIANGLE_NOTICE.txt`, Triangle's terms verbatim.

## Building the packaged app

From a clone of the repository, with [uv](https://docs.astral.sh/uv/):

- **macOS (Apple silicon):** `packaging/build_macos.sh`. It writes `dist/Coppernicus.app` and
  `dist/Coppernicus-<version>-macos-arm64.dmg`.
- **Windows (x64, PowerShell):** `packaging\build_windows.ps1`. It writes `dist\Coppernicus\`
  and `dist\Coppernicus-<version>-windows-x64.zip`.

Each script makes a build environment of its own under `build/` (the packages of `uv.lock`,
without the dev tools and without `triangle`, on a uv-managed Python), installs PyInstaller
(`packaging/requirements-build.txt`), runs `packaging/coppernicus.spec`, and then
`packaging/check_bundle.py`, which fails the build if any part of `triangle` is in the bundle
and writes the bundle's full listing to `dist/*-contents.txt`. The workflow
`.github/workflows/release.yml` runs both scripts on a `v*` tag or by hand and keeps the
results as workflow artifacts; it does not publish a release.

What is in `packaging/`:

| File | What it does |
|---|---|
| `coppernicus.spec` | PyInstaller: what goes into the bundle, the Triangle pin, the notices |
| `frozen_main.py` | The bundle's entry point: fetches Triangle if needed, then runs `coppernicus` |
| `notices.py` | Writes `THIRD_PARTY_NOTICES.txt` from what the bundle contains |
| `check_bundle.py` | Proves `triangle` is not in the bundle (files and PYZ modules) |
| `build_macos.sh`, `build_windows.ps1` | The builds |
| `licenses/` | Triangle's terms (verbatim) and the LGPL-3.0 text |

The fetch, the check and the install are in `src/coppernicus/bootstrap_triangle.py`, tested by
`tests/test_bootstrap_triangle.py` without network.
