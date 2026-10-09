# Third-party notices

This repository is primarily original project code and documentation. It relies on several **public third-party packages and tools** that are installed from their official package managers instead of being vendored into this repository.

## Public package dependencies

| Component | Used for | How it is consumed |
|---|---|---|
| `pptxgenjs` | Generate `.pptx` files from PageIR | Installed from npm |
| `numpy` | Numeric/image array operations | Installed from pip |
| `pillow` | Image reading and writing | Installed from pip |
| `python-pptx` | PPTX inspection and structural checks | Installed from pip |
| `pytesseract` | OCR bridge from Python to Tesseract | Installed from pip |
| `PyYAML` | YAML parsing and validation | Installed from pip |

## Public runtime tools

These tools may be required or recommended in the runtime environment, but they are **not** redistributed inside this repository:

- Tesseract OCR
- LibreOffice
- Poppler (`pdftoppm`)
- Node.js
- Python

## Important note

- The repository does **not** vendor or republish those upstream projects' source trees.
- Users should review each dependency's upstream license and distribution terms in their own environment.
- This file is a release transparency aid, not a substitute for upstream license texts.
