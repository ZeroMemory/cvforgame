# Screen OCR Typist

Capture a Windows screen region and recognize its text with Tesseract. Review and edit the result before the utility types it into the window where you pressed the hotkey.

## Install

Requires Windows 10/11, Python 3.12 with Tkinter, and [Tesseract OCR](https://tesseract-ocr.github.io/tessdoc/Installation.html). Install the language data you plan to use, such as English (`eng`) or Russian (`rus`).

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

Set these values in `config.json`:

- `tesseract_cmd`: leave empty to find Tesseract on PATH or in its standard Windows installation directory; set it to use a specific executable.
- `left`, `top`, `width`, `height`: the capture region in screen pixels. Coordinates can be negative on monitors left of the primary display.
- `language`: installed language data, such as `eng`, `rus` or `rus+eng`.
- `cpm`: typing speed in characters per minute.
- `focus_delay`: time for the target to regain focus.

```powershell
.venv\Scripts\python main.py --config config.json --check
.venv\Scripts\python main.py --config config.json
```

## Read text from an image

Set the language in `config.json` and supply the path to your image. The command prints the recognized text to the terminal.

```powershell
$imagePath = Read-Host 'Path to the image'
.venv\Scripts\python main.py --config config.json --image "$imagePath"
```

## Capture and type

1. Put the source text in the configured region and focus the target input field.
2. Press **Ctrl+Backspace** to preview the recognized text.
3. Edit the text and choose **Confirm**, or choose **Skip** to discard it.

**Confirm** refocuses the target and starts typing. Typing stops when focus moves. **Escape** cancels the preview or typing. **Ctrl+C** in the terminal exits.

Use a capture region within the connected displays and readable, high-contrast text. The hotkey suppresses Backspace in the target.

Run the utility and target at compatible Windows privilege levels. The target application must accept Windows Unicode keyboard input.
