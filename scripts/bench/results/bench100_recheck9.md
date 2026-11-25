# TeXres vs TeX Live: 100-document benchmark

Run 2026-11-25T08:34:18+08:00; 9 timed runs per cell (median), tools alternating; jobs=1, CPUs/job=None.

Ratio = TeXres wall time / TeX Live wall time (below 1: TeXres faster).

## Environment

| | |
| --- | --- |
| CPU | AMD Ryzen 9 9950X3D2 16-Core Processor (32 logical) |
| RAM / OS | 186.4 GiB; Arch Linux, kernel 7.3.5-arch1-1; governor `powersave` |
| TeXres | texres 0.7.11 (Rust TeX engine) (sha256 `0440f5328c3796bd…`); eb6fdce, scripts/build_pgo.sh (PGO, fat LTO, as release.yml ships); reference TeX Live aligned with locked TeX Live 2026 TEXMFLOCAL overlay |
| TeX Live | pdfTeX 3.141592653-2.6-1.40.29 (TeX Live 2026/Arch Linux); XeTeX 3.141592653-2.6-0.999998 (TeX Live 2026/Arch Linux); This is LuaHBTeX, Version 1.24.0 (TeX Live 2026/Arch Linux); Latexmk, John Collins, 15 June 2025. Version 4.87; texres 0.7.11 (Rust TeX engine) |
| Load average | start 8.8, end 1.9 |

## Corpus composition

| Group | Documents |
| --- | ---: |
| generated, lua | 1 |
| generated, pdf | 2 |
| github, pdf | 1 |
| **total** | 4 |

## Status

| Status | Documents |
| --- | ---: |
| timed | 4 |

## TeX Live Parity & Similarity

| Metric | Value |
| --- | ---: |
| Page-count parity | 4 / 4 |
| Exact text parity (`pdftotext -layout`) | 4 / 4 |
| Text similarity (min / median / mean) | 1.0000 / 1.0000 / 1.0000 |
| Composite similarity (min / median / mean) | 1.0000 / 1.0000 / 1.0000 |
| Release gate | **PASS** (min_similarity >= 0.980, min_text_similarity >= 1.000, max_median_wall_ratio <= 1.00) |

## Summary: documents where TeXres is faster / slower

| Scenario | TeXres faster | TeXres slower | Median ratio | Worst ratio |
| --- | ---: | ---: | ---: | ---: |
| (a) cold build | 4 | 0 | 0.75 | 0.89 (pdf_siunitx_tables) |
| (b) no-change rebuild | 4 | 0 | 0.08 | 0.18 (gh_lkmpg_book) |
| (c) one-line edit rebuild | 4 | 0 | 0.74 | 0.86 (pdf_siunitx_tables) |

## All timed documents, slowest first (by worst ratio over scenarios)

| Document | Engine | Pages | (a) TeXres / TL s | ratio | (b) TeXres / TL s | ratio | (c) TeXres / TL s | ratio |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| pdf_siunitx_tables | pdf | 19 | 1.88 / 2.12 | 0.89 | 0.003 / 0.042 | 0.08 | 0.632 / 0.732 | 0.86 |
| gh_lkmpg_book | pdf | 193 | 9.27 / 12.01 | 0.77 | 0.011 / 0.063 | 0.18 | 3.35 / 4.42 | 0.76 |
| lua_beamer | lua | 21 | 1.41 / 1.92 | 0.73 | 0.004 / 0.052 | 0.09 | 0.708 / 0.987 | 0.72 |
| pdf_beamer_metropolis | pdf | 73 | 1.33 / 1.85 | 0.72 | 0.004 / 0.046 | 0.08 | 0.675 / 0.945 | 0.71 |

## Documents where TeXres is slower in at least one scenario

None.

## Documents that failed, differ, or could not be timed

None.

