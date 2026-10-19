---
pretty_name: Pune Municipal Corporation (PMC) publications
language:
- mr
- en
tags:
- government
- india
- pune
- municipal-corporation
- marathi
- pdf
viewer: false
---

# punePMC: Pune Municipal Corporation publications

PDFs published by the Pune Municipal Corporation (पुणे महानगरपालिका), mirrored as plain files.
Most documents are scanned Marathi PDFs. This dataset holds the files only; there are no parquet or
metadata files here.

## Contents

| Path | Source |
|---|---|
| `publications/circulars/pdfs/<YYYY>/<doc_id>.pdf` | circulars from https://www.pmc.gov.in/en/l/circular |

A few circulars were posted by PMC as JPEG images or Word files; they are kept as they are, under the same
folder with their own extension (`.jpg`, `.png`, `.doc`, `.docx`).

## Naming

`doc_id = <dept_code>.<YYYY-Mon-DD>.<NN>`, e.g. `gad.2019-May-31.01`:

- `dept_code`: short code of the issuing PMC department (e.g. `gad` = General Administration)
- `YYYY-Mon-DD`: date of the circular
- `NN`: number of the circular among those of that department on that day (`01`, `02`, ...)

The same document is on archive.org as `in.gov.pmc.circular.<doc_id>`, e.g.
https://archive.org/details/in.gov.pmc.circular.gad.2019-May-31.01, and its PMC URL is captured in the Wayback Machine.

## Metadata and updates

The metadata (titles in English and Marathi, department, date, PMC node id, source URL, checksums,
Wayback and archive.org links) and the code that builds this dataset are at https://github.com/orgpedia/punepmc
(`input/documents/publications/circulars/`). The dataset is updated daily. When PMC replaces the PDF of a
circular, the file is overwritten here and the previous version stays in the repository history.
