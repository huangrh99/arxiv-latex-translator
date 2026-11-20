---
name: arxiv-latex-repair
description: Diagnose and repair build problems in Chinese translations of arXiv LaTeX papers compiled with XeTeX (Tectonic or XeLaTeX with xeCJK). Use when a translated paper fails to compile, or compiles but has undefined citations or references, empty bibliographies, or missing glyphs.
---

# Repairing translated arXiv LaTeX builds

The work tree is a Chinese translation of an English arXiv paper. `../source` holds the original,
untranslated source with the same layout. Translation replaced prose only, so the original is the
reference for every piece of LaTeX syntax. The pipeline adds xeCJK and a Chinese font itself.

## Workflow

1. Read the build check findings and the first fatal error in the compiler log.
2. Open the file the log points to and compare it with the same file in `../source`.
3. Make the smallest edit that fixes the cause, then look for other errors with the same cause.
4. Never translate, rewrite, or delete prose, formulas, citations, labels, tables, figures, or
   sections, and never modify `../source`.
5. You run offline. Do not download packages or fonts, and do not treat a local compile that fails
   for lack of network as a new error. Diagnose from the log.
6. Finish with a summary listing every file you changed and why.

## Known problems and fixes

### LaTeX syntax broken by translation
- Symptom: `Undefined control sequence`, `Missing \endcsname`, runaway argument, or a `\ref`,
  `\Cref`, or `\cite` key that does not exist, in a file that compiles in `../source`.
- Fix: restore the LaTeX syntax exactly from `../source` (key names, braces, command names, `%`,
  `$`) and keep the Chinese prose around it.

### pdfTeX-only features under XeTeX
- Symptom: microtype errors such as `Disabling ligatures of a font is only possible with pdfTeX`
  (from `\DisableLigatures`), or errors from `\pdfglyphtounicode`, `\input{glyphtounicode}`, microtype `tracking=` or
  `expansion`, `\usepackage[T1]{fontenc}`, or `inputenc`.
- Fix: load `iftex` and wrap the feature in `\ifPDFTeX ... \fi`, also inside `.cls` and `.sty`
  files. Do not delete it.

### Package missing from the TeX distribution
- Symptom: `File 'pkg.sty' not found` for a package the paper really uses.
- Fix: never create an empty stub `.sty`; that silently drops content. For decorative macros
  (icons, logos), use `\IfFileExists{pkg.sty}{\usepackage{pkg}}{...}` with minimal fallback
  definitions of the macros the paper uses, and mention it in the summary.

### Bibliography database missing, prebuilt .bbl present
- Symptom: BibTeX reports `I couldn't open database file X.bib`; every citation is undefined and
  prints as `??`, although the build exits with code 0. A `<main>.bbl` file exists in the tree.
- Cause: arXiv authors often upload the generated `.bbl` without the `.bib`. Tectonic reruns
  BibTeX, which finds no database and replaces the bibliography with an empty one.
- Fix: in the main file, replace `\bibliography{X}` with `\input{<main>.bbl}` and keep
  `\bibliographystyle` as it is. Do not edit the `.bbl` content.

## Learned cases

Entries below were added automatically after successful repairs. Prefer a matching case here.
