# ArXiv LaTeX Translator repository guidance

## Scope

This file contains maintenance and implementation guidance for agents working in this repository. User-facing startup and configuration instructions belong in `README.md`.

## Runtime boundaries

- Keep paper archives, extracted sources, translated sources, PDFs, logs, job metadata, and Codex credentials outside the Git repository.
- Do not commit `auth.json`, `.env` files, task data, generated PDFs, compiler logs, or backend URLs containing private network information.
- GitHub Pages contains only the static frontend. The backend and paper data stay on the Mac mini.
- Keep the public repository's `CODEX_MODEL`, `CODEX_FALLBACK_MODEL`, and backend URL configuration empty. Set deployment values through the Mac mini service environment or the Pages deployment secret.

## Translation invariants

- Preserve formulas, LaTeX commands, labels, references, citation commands, image commands, table environments, table cells, bibliography entries, custom macros, and source assets byte-for-byte where the pipeline promises protection.
- Translate prose, section headings, and figure/table captions. Keep title, author, date, thanks, and institute metadata unchanged.
- Remove source comments before translation. Copy `preamble.tex` unchanged. Skip Appendix files and content after `\\appendix` by default, then process them only through the explicit Appendix action.
- Resume from the per-file and per-chunk cache. Never redownload a resumable job or retranslate a chunk whose source hash and protected structure still validate.
- If Codex returns invalid protected structure, send the concrete validation error back in the next prompt. Preserve the failed chunk as pending and never cache malformed output as a successful translation.
- Use three parallel Codex calls per TeX file by default. Aggregate token usage in the main thread and persist it after every completed chunk.
- The service deployment currently uses `gpt-6.2-sol` as the primary model, `gpt-6-sol` as the configured fallback, and `medium` reasoning effort. Do not replace these with an unrequested model.

## Compiler repair

- A compile failure automatically invokes Codex repair inside the translated work tree and recompiles the document. The default limit is three repair attempts and is controlled by `ARXIV_MAX_COMPILE_REPAIRS`.
- Each repair receives the latest compiler log and must preserve formulas, citations, labels, tables, figures, and paper content.
- A failed compile remains visible with its compiler log and translated work tree after the automatic repair limit is exhausted.
- Preserve the original source archive and extracted source during repair and resume.

## Verification

Run the focused test suite before committing:

```bash
PYTHONPATH=. python3 -m pytest -q
```

For pipeline changes, also verify a real completed job through the Mac mini HTTPS endpoint. Check history search, token accounting, translated PDF text, comparison reading, protected structures, source assets, and compiler return code.

After persistent changes pass validation, create a local milestone commit and push only when the user explicitly requests a remote update. Do not create a PR or deploy unrelated changes.
