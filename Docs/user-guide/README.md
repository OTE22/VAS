# VAS 5.0.0 user guide

Validated on **7 October 2026** in an isolated local instance with synthetic data.

- [Editable Word guide](VAS-User-Guide.docx)
- [Matching PDF](VAS-User-Guide.pdf)
- [Capability checklist](Capability-coverage.csv)
- [Verification report and limitations](VERIFICATION.md)

The guide contains 33 task walkthroughs, 76 annotated figures and a 71-capability register. Its 144 pages include large screenshot plates, a clickable contents page, a task finder and page numbers. Read the verification qualifier for each task: a visible control is not proof of successful completion.

## Assets

| Folder | Contents |
|---|---|
| `screenshots/` | Genuine sanitized browser captures, including discovery evidence |
| `screenshots/focus/` | Crops used in the guide |
| `annotated/` | Numbered screenshot figures as PNG |
| `annotations/` | Self-contained editable SVGs and screenshot-coordinate JSON |
| `source/` | Editable workflow/coverage JSON, figure/page indexes and build/check scripts |

Orange numbers correspond to written steps within one task. Numbers restart for the next task. Result-only screenshots have no action arrows. All portraits are synthetic silhouettes; seeded detections and similarities are demonstration records, not measured recognition results.

Passwords and the one-time sender credential were raster-redacted before assets were saved. SVGs embed only sanitized image data. Private environment configuration, browser sessions, raw logs and credentials are excluded.

## Editing and rebuilding

Edit the DOCX directly for normal editorial changes. For a reproducible rebuild, edit `source/workflows.json` and `source/coverage.json`; layout and introductory text are in `source/build_guide.py`. JSON content is authoritative. The CSV contains the same coverage entries.

Requirements: Python 3, `python-docx`, `Pillow`, `CairoSVG`, `PyMuPDF`, a Cairo runtime, and LibreOffice for PDF conversion. In an environment where these dependencies are already installed, run from this folder:

```bash
python source/annotate.py
python source/build_guide.py
libreoffice --headless --convert-to pdf --outdir . VAS-User-Guide.docx
python source/check_guide.py
```

The checker updates `source/page-map.json`. If `page_map_changed` is true, rebuild the Word/PDF files and run the checker again. It writes rendered review copies under `/tmp/vas-guide-page-review`; inspect every page before distribution. Re-export the coverage CSV after changing its JSON source.

Each SVG contains editable `step-N` groups and the sanitized screenshot background. You can edit it with an SVG editor. To regenerate figures, edit the corresponding JSON rectangles instead; `annotate.py` will overwrite generated SVG/PNG files. Coordinates refer to the original sanitized screenshot, with optional `crop_override` for context. Do not alter application controls or invent screenshots.

The private demonstration stack was stopped after capture and verification. This package does not install VAS or contain reusable credentials. The separate guided installation documentation covers deployment.
