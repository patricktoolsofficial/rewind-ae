# Rewind AE

Rewind AE is a Windows tool for creating copies of After Effects projects, templates, and presets for older releases. It supports AEP, AET, AEPX, and FFX files, with conversion targets from AE 2020 to AE 2026.

## How to use Rewind AE

### 1. Download and open the tool

1. Download the project ZIP and extract it to a folder on your PC.
2. Open the `RewindAE` folder, then open `dist`.
3. Double-click `RewindAE.exe`.
4. You do not need to install Python or pip to use this executable.
5. If Microsoft WebView2 is missing, the app attempts to download and install it. An internet connection is needed for that first-time setup. If setup fails, follow the error message and reopen the tool after installing WebView2.

The included executable targets Windows 10 and 11 on 64-bit PCs. Source-code build instructions are below.

### 2. Add your After Effects files

1. Open the **Rewind** tab.
2. Drag files from Windows File Explorer onto **DROP HERE**. You can also drop an entire project folder.
3. Alternatively, click **DROP HERE** to select files, or press **Ctrl+O**.
4. Wait for the tool to finish reading the selection. Large folders can take longer; the main screen stays visible while it reads them.

Supported file types:

| Extension | File type |
| --- | --- |
| `.aep` | After Effects project |
| `.aet` | After Effects project template |
| `.aepx` | XML project |
| `.ffx` | Animation preset |

### 3. Choose the After Effects version

1. On the **Rewind** tab, use the version slider or click a year beneath it.
2. Select the version you want to open the files in. Available targets are **AE 2020 through AE 2026**.
3. Check the large version label to confirm your selection.

For example, to open an AE 2026 project in AE 2024, choose **AE 2024**. Files already at or below the selected target do not need downgrading. If no files need conversion, the tool shows **Nothing to rewind**.

### 4. Review the file list

1. Open the **Files** tab.
2. Expand a folder using its arrow to see its contents.
3. Review the detected file types and After Effects versions.
4. Use the filters to inspect **All**, **Rewind**, **Compatible**, or **Copied** items. An issues filter appears when relevant.
5. Remove a source with its **×** button if you added the wrong item.

Before conversion, **Copied** refers to assets planned for copying. A compatible project can be copied unchanged as part of a folder conversion.

### 5. Choose where to save the results

1. Open **Settings**.
2. Leave **Custom folder** off to save results beside the source files or folders.
3. Turn **Custom folder** on to choose a different output location.
4. To change that location later, click the small **arrow button** beside the folder path.
5. Check the displayed path before starting conversion.

Optional settings:

- **Copy assets:** When processing a folder, include non-AE files such as footage, images, and fonts. This copies files; it does not install fonts or plugins.
- **Show when done:** Open the output location automatically when conversion finishes.

Copying a folder does not collect media stored elsewhere on your PC. Keep any externally linked footage and required plugins available separately.

### 6. Start the conversion

1. Return to **Rewind**.
2. Confirm the target version and output settings.
3. Click **Rewind … files**.
4. Wait for processing to finish. Large projects and folders may take longer. The app keeps the main view visible instead of displaying a separate loading screen.
5. To stop an active conversion, click **Cancel** and wait for cancellation to finish.

The tool creates new output copies and keeps your originals untouched. Output names include the selected AE year. If an output name already exists, the tool chooses another name rather than replacing it.

### 7. Find and check your converted files

1. When the results appear, review the converted, copied, and failed counts.
2. Click **Show in folder** to locate the output, or use the custom output folder you selected.
3. Open the converted project, template, or preset in the target version of After Effects.
4. Check compositions, footage links, effects, expressions, and fonts before relying on the result.
5. Click **Start over** when you are ready to process another selection.

### 8. Change the accent color

1. Click the **rainbow button** at the top-right of the app.
2. Choose a color swatch or move the hue slider.
3. Close the panel to return to your files.

The app starts with green on a fresh installation. Your selected accent is saved, and the app logo and file icons follow it.

### 9. Move or close the window

- Drag the **Rewind AE logo/name area** to move the window.
- Press **Alt+F4** while the app is focused to close it.

### Important compatibility limits

- Downgrading a version stamp cannot add newer After Effects features to an older release. Some effects, expressions, or project features may be unavailable or behave differently.
- AE 2023-and-newer `.aepx` projects cannot be downgraded to AE 2022 or older by this tool. Use an `.aep` project instead.
- A converted file still requires the appropriate version of After Effects to open or use it.
- Keep the original files until you have checked the converted results.
- Conversion happens locally. Internet access is used for missing-runtime setup and for external links you choose to open.

### If something does not work

1. **No files appear:** Check that the selection contains supported files. Try one file first rather than a large folder.
2. **Nothing to rewind:** Choose an older target version or add a project created in a newer version.
3. **Cannot save output:** Choose a writable folder and check available disk space.
4. **After Effects reports missing footage:** Locate the original media and relink it in After Effects. Files outside a dropped folder are not automatically collected.
5. **After Effects still cannot open a result:** Keep the original, check the compatibility limits above, and try a more recent target version.
6. **The app cannot start:** Check the WebView2 setup message. Managed-PC restrictions or an unavailable internet connection may prevent installation.


## Run or build from source
For development, install Python 3.10+ (the included executable was built with Python 3.12 x64).
- Run: `run.bat`
- Build: `build.bat`, which writes `dist/RewindAE.exe`
- Alternatively: `python -m pip install -r requirements.txt`, then `python app.py` or `python -m PyInstaller --clean --noconfirm RewindAE.spec`.

## Files
- `app.py`: desktop window, settings, file dialogs and UI bridge.
- `ae_downgrader.py`: scanning and conversion engine.
- `windows_runtime.py`: missing WebView2 detection and installation.
- `ui.html`: current UI, including embedded font, file icons and background assets. Edit this file directly.
- `DotsAllForNow.ttf`: current font source; also embedded in ui.html.
- `app_icon.ico`: Windows app icon.
- `RewindAE.spec`, `requirements.txt`, `build.bat`, `run.bat`: build and source-run configuration.
- `dist/RewindAE.exe`: current ready-to-run release with Patrick Tools links.

Conversion targets: AE 2020–2026. Outputs are new copies; newer After Effects features may not work in older versions. Check output projects in the target After Effects version. The executable is unsigned.
