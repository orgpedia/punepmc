# punePMC — Spec

Pune Municipal Corporation publishes a lot of useful information, but it sits in Marathi PDFs that are hard to read and parse.
The long-term aim is to download every PMC publication regularly, track changes, and then translate the documents and extract information from them.

**This spec covers only getting the PDFs in reliably**, starting with circulars from <https://www.pmc.gov.in/en/l/circular>.
Each circular is stored in `LFS/`, on Hugging Face (`Orgpedia/punePMC`) and on archive.org, and linked in the Wayback Machine.
Everything runs from one Makefile, either locally or from GitHub Actions, in three modes:

| Mode | Target | Where | What |
|---|---|---|---|
| **Setup** | `make setup` | locally, once | full crawl → move PDFs from pmccirc → download the rest → HF → archive.org. **No Wayback.** |
| **Daily** | `make daily` | GitHub Actions cron (or locally) | new and changed entries only → download → Wayback → archive.org → HF |
| **Backfill** | `make backfill_archive`, `backfill_wayback`, `backfill_crawl` | GitHub Actions (manual or cron), or locally | work through the backlog in `LIMIT`-sized chunks, or re-crawl the whole site |

Processing (OCR, translation, extraction) is a later stage and is out of scope here.

The structure follows `../doptIASCivil`, the project behind `../iasCivilList`:
- a Makefile of `uv run` targets that write logs;
- state JSON in `input/documents/`, tracked in git;
- PDFs in `LFS/` (not in git), mirrored to a Hugging Face dataset;
- relative symlinks from `input/documents/` into `LFS/`.

## 1. Source website (studied 2026-10-17)

`https://www.pmc.gov.in/en/l/circular` is a Next.js page. The list is **not** in the HTML: the browser loads it from
PMC's Drupal JSON:API backend, which we call directly.

| What | URL |
|---|---|
| Circulars (English) | `https://webadmin.pmc.gov.in/jsonapi/node/circular` |
| Circulars (Marathi) | `https://webadmin.pmc.gov.in/mr/jsonapi/node/circular`: same nodes and same PDF, with the Marathi web-page title |
| Departments | `https://webadmin.pmc.gov.in/jsonapi/taxonomy_term/department` (and `/mr/…` for Marathi names): `drupal_internal__tid`, `name`, `field_slug` |
| PDF | `https://webadmin.pmc.gov.in` + `file--file.attributes.uri.url` (`/sites/default/files/circular/<name>.pdf`) |

Query parameters (all checked to work):

- `include=field_circular_upload,field_department`: file and department records come back in `included`.
- `page[limit]=50` (the server caps it at 50) and `page[offset]=N`. Follow `links.next.href` until it is absent. **Rewrite `http://` → `https://`**: the server returns `http` links.
- `sort=changed`, `sort=-created`, `sort=drupal_internal__nid`.
- `filter[c][condition][path]=changed&filter[c][condition][operator]=>&filter[c][condition][value]=<unix seconds>`.
  The value **must be an integer epoch**: an ISO date causes an HTTP 500 SQL error.
- `filter[n][condition][path]=drupal_internal__nid&filter[n][condition][operator]=IN&filter[n][condition][value][]=…`: fetch specific nids.
- `fields[node--circular]=…`, `fields[file--file]=…`: sparse fieldsets, which make full scans lighter.

Fields used:

| JSON:API field | Meaning |
|---|---|
| `drupal_internal__nid` | PMC's circular id; **primary key** for all our state |
| `title` (en / mr endpoint) | web-page title, stored as `title_en` and `title_mr` |
| `field_circular_date` | date of the circular, `YYYY-MM-DD` |
| `created` / `changed` | node created / last edited (UTC) |
| `field_circular_upload` | **one** file (an object, not a list): `drupal_internal__fid`, `filename`, `filesize`, `uri.url` |
| `field_department` | department term → `tid` |

What the site does (full scan on 2026-10-17), and what follows for the design:

- **9,472 circulars**, 976 added since 2026-08-06 and none removed, across 69 of PMC's 92 departments. 2 have no file
  (incl. nid 23894), 6 have no department, and 2 have an invalid date.
- **Old circulars are still being added.** On 2026-10-15 nodes 42639–42645 were created with circular dates from 2015–2022.
  → "New" means a *new nid*, never "a recent circular date".
- **A circular's file changes only rarely, when PMC fixes a broken upload.** Between 2026-08-06 and 2026-10-17, 5 circulars got a new file, and all 5
  had failed to download in pmccirc (e.g. nid 10807, whose old URL is now 404). → Track the file id (`fid`) per circular and download again when it changes.
- **One PDF can be attached to several circulars.** 543 files (same `fid`) are shared by 1,213 circulars, at most 33 per file. These are mostly
  duplicate postings from bulk loads in April 2025: 499 groups have identical titles, and the rest are small variations (re-translated English,
  an added prefix, or the same PDF posted by a second department). → Still **one archive.org item per website entry**, so the totals on the site and on
  archive.org can be compared and each entry can be updated on its own. Locally the PDF is stored once (§2, "Shared PDFs").
- **Half the "English" titles are in Marathi.** 5,054 of 9,335 (Aug 2026) have a Marathi title on the English side. This is mostly 2017–2023 (75–85% of
  those years), while 2014–16 and 2024–26 are real English. → Store both titles exactly as given, and flag whether `title_en` is really English.
- Many circulars share a department and date (General Administration, 2019-05-31: 74 entries). → The per-day number in the doc_id.
- PDF responses carry `Content-Length`, `Last-Modified` and `ETag`, and support `Range`.
- There is no total count in the API. No robots.txt rules for `/jsonapi` or `/sites`. Send `User-Agent: Mozilla/5.0` and stay at ≤ 1 request/s.
- **The PDFs are scans.** In a sample of 40, none had usable Marathi text (15 had no text layer and 25 a garbled one). This matters for the later
  processing stage, not here.

## 2. Naming

Each website entry with a file gets a **doc_id** when it is first seen. The doc_id never changes after that:

```
doc_id     = <dept_code>.<YYYY-Mon-DD>.<NN>             e.g.  gad.2019-May-31.01
archive.org: in.gov.pmc.circular.<doc_id>               e.g.  in.gov.pmc.circular.gad.2019-May-31.01
HF / LFS   : publications/circulars/pdfs/<YYYY>/<doc_id>.pdf
```

- **dept_code** is the code column in the department table below (`departments.json`).
- **date** is `field_circular_date` written as `YYYY-Mon-DD`, with a three-letter English month (`Jan` … `Dec`).
- **NN** is the entry's number within its (dept_code, date) group. It is always present, zero-padded to 2 digits (`01`, `02`, …),
  and widens to 3 digits only if a group ever passes 99 (the largest is 74). Numbers are given in nid order to the entries present when the group is
  first seen. An entry added later gets the next free number, even if it was issued earlier the same day.
- **Frozen.** The doc_id is stored in `circulars.json` and never recomputed. archive.org identifiers can't be renamed, so if PMC later edits
  an entry's date or department, the doc_id stays and only the metadata changes. The longest identifier is ~55 characters (archive.org allows 100).
- **Held entries.** If the date is invalid (it won't parse, or is outside 1950…today+30 days) or the department has no code, the entry gets no doc_id
  and is **held**: nothing is downloaded or uploaded. Every run lists held entries until `overrides.json` or `departments.json` fixes them.
  This avoids freezing a wrong identifier.
- **Entries without a file** (`no_file`, 2 today) get no doc_id and no archive.org item. The site vs archive.org check (§6, `status`) counts them separately.
- **Shared PDFs.** Every entry gets its own doc_id and archive.org item, even when its PDF (same `fid`) is shared with other entries.
  The PDF is downloaded **once per fid**. Each extra doc_id path in `LFS/` is a **hardlink** to the same file, so it takes no extra disk space,
  and HF stores identical content only once. `same_file_as` in `circulars.json` and in the archive.org metadata lists the other doc_ids, so
  the later processing stage can OCR the PDF only once.

### Department codes (`departments.json`)

The codes below are a proposal, drafted from PMC's slugs (and taxonomy as of 2026-10-17). They need your review, because **a code is frozen once any doc_id
uses it.** Rules: lowercase `[a-z0-9-]`, unique, short. Ward offices use `ward-*`, and the five zones use `dmc-zone1…5`.
Codes are proposed for all 92 departments, including the 23 with no circulars yet, so a first circular from one of them isn't held.

PMC data quirks:
- tids 343 and 659 are both "Tender Cell" with the same slug `tender-cell`; their codes are `tender-cell` and `tender`.
- tids 658 and 1412 have the same Marathi name.
- Some slugs contain `(`, `)` or `.`, which archive.org identifiers do not allow; that is why codes are needed at all.

| tid | Department (en) | Department (mr) | PMC slug | **code** | circulars |
|---:|---|---|---|---|---:|
| 1 | Bhavan Rachna Department | भवन रचना विभाग | `bhavan` | **`bhavan`** | 5 |
| 2 | Building Development | बांधकाम विकास | `building-development` | **`building`** | 39 |
| 3 | Central Store Department | मध्यवर्ती भांडार विभाग | `central-store-department` | **`store`** | 71 |
| 4 | Chief Accounts And Finance Office | मुख्यलेखा व वित्त विभाग | `accounts` | **`accounts`** | 461 |
| 5 | Chief Engineer Project | मुख्य अभियंता प्रकल्प | `ce-project` | **`ce-project`** | 9 |
| 6 | City Census Department | शहर जनगणना कार्यालय | `census` | **`census`** | 21 |
| 7 | Cultural Centers | सांस्कृतिक केंद्र विभाग | `cultural` | **`cultural`** | 4 |
| 8 | Slum Eradication And Rehabilitation | झोपडपट्टी निर्मुलन व पुनर्वसन | `slum` | **`slum`** | 14 |
| 9 | Disaster Management Cell | आपत्ती व्यवस्थापन विभाग | `disaster` | **`disaster`** | 90 |
| 11 | Election Department | निवडणुक विभाग | `election` | **`election`** | 76 |
| 12 | Electrical Department | विद्युत विभाग | `electrical` | **`electrical`** | 30 |
| 13 | Encroachment Department | अतिक्रमण /अनधिकृत बांधकाम निर्मुलन विभाग | `encroachment` | **`encroachment`** | 29 |
| 14 | Environment Department | पर्यावरण विभाग | `environment` | **`environment`** | 8 |
| 15 | Estate And Management Department | मालमत्ता व व्यवस्थापन विभाग | `estate-management` | **`estate`** | 38 |
| 16 | Fire Brigade | अग्निशामक विभाग | `fire` | **`fire`** | 1 |
| 17 | Garden Department | उद्यान विभाग | `garden` | **`garden`** | 1 |
| 18 | General Administration | सामान्य प्रशासन विभाग | `general-administration` | **`gad`** | 5343 |
| 19 | Health Department | आरोग्य विभाग | `health` | **`health`** | 139 |
| 20 | Heritage Cell | वारसा व्यवस्थापन | `heritage` | **`heritage`** | 0 |
| 22 | Information Technology | माहिती व तंत्रज्ञान विभाग | `it` | **`it`** | 178 |
| 23 | Labour Welfare Department | कामगार कल्याण विभाग | `labour-welfare` | **`labour`** | 626 |
| 25 | Motor Vehicles Department | मोटार वाहन विभाग | `vehicle-depo` | **`vehicle`** | 16 |
| 26 | Municipal Secretary | नगर सचिव विभाग | `secretary` | **`secretary`** | 4 |
| 27 | Pradhan Mantri Awas Yojana | प्रधानमंत्री आवास योजना | `pmay` | **`pmay`** | 2 |
| 28 | Primary Education Department | प्राथमिक शिक्षण विभाग | `primary` | **`primary-edu`** | 128 |
| 32 | Road Department | पथ विभाग | `road` | **`road`** | 10 |
| 34 | Skysign Department | आकाशचिन्ह व परवाना विभाग | `skysign` | **`skysign`** | 30 |
| 35 | Social Development Department | समाज विकास विभाग | `social-development` | **`social-dev`** | 22 |
| 36 | Solid Waste Management | घनकचरा व्यवस्थापन विभाग | `solid-waste-management` | **`swm`** | 23 |
| 37 | Sports Department | क्रीडा विभाग | `sports` | **`sports`** | 16 |
| 38 | Tree Authority Department | वृक्ष प्राधिकरण विभाग | `tree` | **`tree`** | 3 |
| 39 | Water Supply Department | पाणीपुरवठा विभाग | `water` | **`water`** | 7 |
| 40 | Secondary and Technical Education | शिक्षणाधिकारी (माध्यमिक व तांत्रिक) विभाग | `secondary` | **`secondary-edu`** | 27 |
| 248 | Municipal Commissioner Office | महापालिका आयुक्त कार्यालय | `municipal-commissioner-office` | **`mc-office`** | 554 |
| 249 | Additional Municipal Commissioner (General) | अतिरिक्त महापालिका आयुक्त (जनरल) कार्यालय | `additional-municipal-commissioner-(general)` | **`amc-general`** | 932 |
| 250 | Additional Municipal Commissioner (Estate) | अतिरिक्त महापालिका आयुक्त (इस्टेट) कार्यालय | `additional-municipal-commissioner-(estate)` | **`amc-estate`** | 61 |
| 251 | Vigilance Department | दक्षता विभाग | `vigilance-department` | **`vigilance`** | 31 |
| 252 | City Engineer Office | शहर अभियंता कार्यालय | `city-engineer-office` | **`city-engineer`** | 16 |
| 255 | Transport Planning Department | वाहतूक नियोजन विभाग | `transport-planning-department` | **`transport-planning`** | 0 |
| 257 | Taxation and Tax Collection Department | कर आकारणी व कर संकलन विभाग | `ptax` | **`ptax`** | 29 |
| 259 | Land Acquisition | भूसंपादन व व्यवस्थापन विभाग | `land-acquisition` | **`land-acq`** | 5 |
| 260 | Social Welfare Department | समाज कल्याण विभाग | `social-welfare` | **`social-welfare`** | 1 |
| 262 | Security Department | सुरक्षा विभाग | `security-department` | **`security`** | 12 |
| 263 | Drainage Department | मलनिःसारण विभाग | `drainage` | **`drainage`** | 9 |
| 265 | Legal Adviser | विधी विभाग | `legal-adviser` | **`legal`** | 40 |
| 293 | Additional Municipal Commissioner (Special) | अतिरिक्त महापालिका आयुक्त (विशेष) कार्यालय | `additional-municipal-commissioner-(special)` | **`amc-special`** | 80 |
| 310 | Chief Audit Department | मुख्य लेखा परीक्षक विभाग | `chief-audit-department` | **`audit`** | 27 |
| 339 | Electric vehicle Department | विद्युत वाहन विभाग | `evcell` | **`ev`** | 0 |
| 341 | Telephone Department | दूरध्वनी विभाग | `telephone-department` | **`telephone`** | 1 |
| 342 | Technical Department | तांत्रिक विभाग | `technical-department` | **`technical`** | 0 |
| 343 | Tender Cell | टेंडर सेल | `tender-cell` | **`tender-cell`** | 1 |
| 344 | DMC Zone No. 5 | उप आयुक्त परिमंडळ क्र ५ | `dmc-zone-no.-5` | **`dmc-zone5`** | 2 |
| 345 | DMC Zone No. 4 | उप आयुक्त परिमंडळ क्र ४ | `dmc-zone-no.-4` | **`dmc-zone4`** | 5 |
| 346 | DMC Zone No. 3 | उप आयुक्त परिमंडळ क्र ३ | `dmc-zone-no.-3` | **`dmc-zone3`** | 1 |
| 347 | DMC Zone No. 2 | उप आयुक्त परिमंडळ क्र २ | `dmc-zone-no.-2` | **`dmc-zone2`** | 11 |
| 348 | DMC Zone No. 1 | उप आयुक्त परिमंडळ क्र १ | `dmc-zone-no.-1` | **`dmc-zone1`** | 4 |
| 349 | Cattle Shield | कोंडवाडा विभाग | `cattle-shed` | **`cattle-shed`** | 0 |
| 350 | Mandai Department | मंडई विभाग | `market-department` | **`mandai`** | 1 |
| 351 | Press Department | मुद्रणालय विभाग | `press-department` | **`press`** | 2 |
| 352 | Public Relation Officer | जनसंपर्क अधिकारी | `public-relation-officer` | **`pro`** | 1 |
| 353 | Chawl Department | चाळ विभाग | `chawl-department` | **`chawl`** | 6 |
| 354 | Local Body Tax | स्थानिक संस्था कर | `local-body-tax` | **`lbt`** | 1 |
| 640 | Deputy Commissioner (Special) | उप आयुक्त (विशेष) विभाग | `deputy-commissioner-special` | **`dc-special`** | 71 |
| 641 | Aundh Baner Ward Office | औंध बाणेर क्षेत्रिय कार्यालय | `aundh-baner-ward-office` | **`ward-aundh-baner`** | 0 |
| 642 | Bibwewadi Ward Office | बिबवेवाडी क्षेत्रिय कार्यालय | `bibwewadi-ward-office` | **`ward-bibwewadi`** | 0 |
| 643 | Dhole Patil Road Ward Office | ढोले पाटील रोड क्षेत्रिय कार्यालय | `dhole-patil-road-ward-office` | **`ward-dhole-patil`** | 11 |
| 644 | Kothrud Bavdhan Ward Office | कोथरूड बावधन क्षेत्रिय कार्यालय | `kothrud-bavdhan-ward-office` | **`ward-kothrud-bavdhan`** | 0 |
| 645 | Nagar Road - Wagholi Ward Office | नगररोड - वाघोली क्षेत्रिय कार्यालय | `nagar-road-wagholi-ward-office` | **`ward-nagar-road`** | 10 |
| 646 | Warje Karvenagar Ward Office | वारजे कर्वेनगर क्षेत्रिय कार्यालय | `warje-karvenagar-ward-office` | **`ward-warje`** | 0 |
| 647 | Yerawada - Vadgaonsheri Ward Office | येरवडा - वडगावशेरी क्षेत्रिय कार्यालय | `yerawada-vadgaonsheri-ward-office` | **`ward-yerawada`** | 0 |
| 648 | Kasaba Peth Ward Office | कसबा पेठ क्षेत्रिय कार्यालय | `kasaba-peth-ward-office` | **`ward-kasaba`** | 1 |
| 649 | Dhankawadi - Katraj - Ambegaon Ward Office | धनकवडी - कात्रज - आंबेगाव क्षेत्रिय कार्यालय | `dhankawadi-katraj-ambegaon-ward-office` | **`ward-dhankawadi`** | 7 |
| 650 | Ch. Shivajinagar – Ghole Road Ward office | छ. शिवाजीनगर - घोलेरोड क्षेत्रिय कार्यालय | `ch-shivajinagar-ghole-road-ward-office` | **`ward-shivajinagar`** | 3 |
| 651 | Hadapsar - Manjari Ward Office | हडपसर - मांजरी क्षेत्रिय कार्यालय | `hadapsar-manjari-ward-office` | **`ward-hadapsar`** | 0 |
| 652 | Vishrambaugwada Ward Office | विश्रामबागवाडा क्षेत्रिय कार्यालय | `vishrambaugwada-ward-office` | **`ward-vishrambaug`** | 1 |
| 653 | Kondhwa - Undri Ward Office | कोंढवा - उंड्री क्षेत्रिय कार्यालय | `kondhwa-undri-ward-office` | **`ward-kondhwa`** | 0 |
| 654 | Sinhgad Road Ward Office | सिंहगड रोड क्षेत्रिय कार्यालय | `sinhgad-road-ward-office` | **`ward-sinhgad`** | 0 |
| 655 | Wanawadi Ward Office | वानवडी क्षेत्रिय कार्यालय | `wanawadi-ward-office` | **`ward-wanawadi`** | 1 |
| 656 | Backward Cell | मागासवर्ग विभाग | `backward-cell` | **`backward`** | 0 |
| 657 | Training Academy | प्रशिक्षण प्रबोधिनी | `training-academy` | **`training`** | 27 |
| 658 | Town Planning and Development Department | जिल्हा नियोजन व विकास समिती विभाग | `town-planning-and-development` | **`town-planning`** | 15 |
| 659 | Tender Cell | निविदा विभाग | `tender-cell` | **`tender`** | 13 |
| 660 | Medical Education Trust | वैद्यकीय शिक्षण ट्रस्ट | `medical-education-trust` | **`med-edu-trust`** | 0 |
| 1409 | Rajiv Gandhi Zoological Park & Wildlife Research Center | राजीव गांधी प्राणीसंग्रहालय आणि वन्यजीव संशोधन केंद्र | `zoo` | **`zoo`** | 0 |
| 1412 | District Planning & Development Committee | जिल्हा नियोजन व विकास समिती विभाग | `district-planning-development-committee` | **`dpdc`** | 2 |
| 1422 | Merged 32 Villages | समाविष्ट ३२ गावे | `Merged-32-Villages` | **`merged-villages`** | 0 |
| 1489 | Swargate No. 1 | स्वारगेट क्र. ०१ | `swargate-no-1` | **`swargate-1`** | 0 |
| 1490 | Swargate No. 2 | स्वारगेट क्र. २ | `swargate-no-2` | **`swargate-2`** | 0 |
| 1491 | Lashkar | लष्कर | `lashkar` | **`lashkar`** | 0 |
| 1492 | Bund Garden | बंडगार्डन | `bund-garden` | **`bund-garden`** | 0 |
| 1493 | Chaturshrungi | चतु:शृंगी | `chaturshrungi` | **`chaturshrungi`** | 0 |
| 1494 | SNDT | एस.एन.डी.टी. | `sndt` | **`sndt`** | 0 |

Record format: `{"tid": 18, "name_en": "General Administration", "name_mr": "सामान्य प्रशासन विभाग", "slug": "general-administration", "code": "gad"}`.
`fetch_full` refreshes names and slugs from the taxonomy endpoint, but **never changes a code**. A new tid is added with `"code": null`, a warning is printed,
and its entries are held until a code is filled in.

### Overrides (`overrides.json`)

These were proposed after reading each PDF's first page. They are applied only to doc_id assignment and metadata; the site's raw values stay in `circulars.json`.

| nid | Site value | Override | Evidence on the PDF |
|---:|---|---|---|
| 36369 | date `0016-10-06` | `date: 2016-10-06` | handwritten date ०६/१०/२०१६; reference dated ०५/१०/२०१६ |
| 36823 | date empty | `date: 2016-03-10` | letter dated १०/०३/२०१६ |
| 9876 | no department | `dept_code: mc-office` | letterhead "महापालिका आयुक्त कार्यालय" |
| 9918 | no department | `dept_code: election` | Form 1 election notice, Returning Officer, 215 Kasba Peth |
| 10541 | no department | `dept_code: lbt` | letterhead "स्थानिक संस्था कर कार्यालय" |
| 10698 | no department | `dept_code: mc-office` | letterhead "महापालिका आयुक्त कार्यालय" |
| 10778 | no department | `dept_code: ward-hadapsar` | letterhead "सहायक आयुक्त कार्यालय हडपसर-मुंढवा" |

nid 23894 has no department but also no file, so it is `no_file` and needs no override.

Record format: `{"nid": 36369, "date": "2016-10-06", "note": "…"}` / `{"nid": 9876, "dept_code": "mc-office", "note": "…"}`.

## 3. Repository layout

`punepmc` becomes a git repo on GitHub. The Makefile is the only entry point, both locally and in GitHub Actions.

```
punepmc/
├── Makefile
├── SPEC.md
├── pyproject.toml            # uv: requests, python-dotenv, huggingface-hub, internetarchive
├── env.example
├── .gitignore                # .env, .venv/, LFS/, input/logs/*.log, *.part
├── .github/workflows/
│   ├── daily.yml             # cron → make daily
│   └── backfill.yml          # manual (+ cron for wayback) → make backfill_<what>
├── input/
│   ├── src/
│   │   ├── common.py         # paths, constants, http session, JSON:API paging, json/symlink helpers, doc_id rules, ensure_pdf()
│   │   ├── fetch.py          # incremental (default) | --full
│   │   ├── seed.py           # one-time: move matching PDFs from ../pmccirc
│   │   ├── download.py
│   │   ├── link_wayback.py   # --scope new|all --limit N
│   │   ├── upload_archive.py # --scope new|all --limit N | --update-metadata
│   │   ├── sync_hf.py        # pull | push | card
│   │   └── status.py
│   ├── documents/publications/circulars/      # tracked in git
│   │   ├── departments.json  # curated: tid → code (the table in §2)
│   │   ├── overrides.json    # curated: nid → date / dept_code fixes
│   │   ├── circulars.json    # one record per nid
│   │   ├── downloads.json    # one record per fid
│   │   ├── wayback.json      # one record per fid
│   │   ├── archive.json      # one record per doc_id + fid
│   │   ├── changes/<YYYY-MM-DD>.json
│   │   └── pdfs/<YYYY>/<doc_id>.pdf  ->  ../../../../../../LFS/punePMC/publications/circulars/pdfs/<YYYY>/<doc_id>.pdf
│   └── logs/
├── flow/ output/             # empty; processing comes later
└── LFS/punePMC/              # NOT in git; local mirror of HF dataset Orgpedia/punePMC
    ├── README.md             # HF dataset card
    ├── publications/circulars/pdfs/<YYYY>/<doc_id>.pdf
    └── api/circulars/<YYYY-MM-DD_HHMMSS>/{en,mr}-page-NNN.json      # raw API responses per run (not pushed)
```

- `LFS/punePMC/` mirrors the HF repo layout exactly; other publications later sit beside it as `publications/<type>/pdfs/`.
- `LFS` holds only the **current** version of each PDF. A replaced file is overwritten in place, and the old version lives on in HF's git history,
  archive.org's `history/files/` and the Wayback capture of its old URL. `downloads.json` keeps the sha1 of every version.
- The symlinks are committed, so they dangle until the PDFs are present, as in doptIASCivil.
- **`LFS/` need not be complete.** A GitHub Actions run starts with an empty `LFS/`. Whenever a step needs a PDF that isn't there,
  `ensure_pdf()` fetches it: first from HF (`hf_hub_download` of that one file), then from PMC. Daily and backfill runs therefore work without the 14 GB.

## 4. State files

All files live in `input/documents/publications/circulars/`. Each one is a JSON list sorted by its key, written with `indent=2, ensure_ascii=False` and
rewritten atomically (write `.tmp`, then rename) after every page or item, so an interrupted run can resume. Git history is the full record of edits.

**circulars.json** (key `nid`)
```json
{"nid": 42808, "doc_id": "gad.2026-Oct-16.01", "uuid": "8b26f8e4-…",
 "title_en": "Regarding the departments under the control of …", "title_mr": "अतिरिक्त महापालिका आयुक्त …",
 "title_en_is_english": true,
 "circular_date": "2026-10-16", "tid": 18, "department": "General Administration",
 "created": "2026-10-16T13:02:35+00:00", "changed": "2026-10-16T13:22:42+00:00",
 "fid": 39576, "file_url": "https://webadmin.pmc.gov.in/sites/default/files/circular/732416102026.pdf",
 "filename": "732416102026.pdf", "filesize": 243225, "same_file_as": [],
 "status": "ok", "first_seen_utc": "…", "last_seen_utc": "…", "removed_utc": null}
```
- `status` is one of `ok`, `held:bad_date`, `held:no_dept_code`, `no_file`.
- `title_en_is_english` is false when `title_en` is mostly Devanagari.
- `same_file_as` lists the doc_ids of other entries whose current fid is the same.

**downloads.json** (key `fid`)
```json
{"fid": 39576, "nids": [42808], "url": "…/732416102026.pdf",
 "paths": ["publications/circulars/pdfs/2026/gad.2026-Oct-16.01.pdf"], "size": 243225, "sha1": "…",
 "last_modified": "Fri, 16 Oct 2026 13:22:36 GMT", "etag": "\"6ad2251c-3b619\"",
 "download_time_utc": "…", "status": "downloaded", "attempts": 1, "current": true}
```
- `status` is one of `downloaded`, `seeded`, `failed:<reason>` (`404`, `size_mismatch`, `not_pdf`, …).
- `paths` has one entry per doc_id using this fid (hardlinks).
- `current` is false for versions that have been replaced.

**wayback.json** (key `fid`): `fid, url, link_success, job_id, timestamp, archive_url, content_url, message, time_utc`. The format is the same as doptIASCivil's.

**archive.json** (key `doc_id` + `fid`): `doc_id, fid, identifier, archive_url, sha1, upload_success, metadata_sha1, message, time_utc`.
`metadata_sha1` is the hash of the metadata last sent, so `update_archive_metadata` only touches items whose metadata changed.

**changes/<YYYY-MM-DD>.json**: one entry per event, appended by `fetch` and `download`:
```json
{"time_utc": "…", "nid": 10807, "doc_id": "…", "event": "file_replaced", "old": {"fid": 9999, "url": "…"}, "new": {"fid": 39420, "url": "…"}}
```
- Events: `new`, `file_replaced`, `metadata_changed` (with the list of changed fields), `removed`, `reappeared`, `held`, `released`.
- This is also the hand-off to the later processing stage.

The **watermark** for incremental fetches is derived, not stored: `max(changed)` over `circulars.json`.

## 5. Building-block targets

Each target runs `uv run python -u input/src/<script>.py …`, and the output is piped through `tee input/logs/<target>.log` (`set -o pipefail`).
All of them are idempotent and resumable: re-running one skips work already recorded in the state files.

Make variables:

| Variable | Default | Used by |
|---|---|---|
| `LIMIT` | empty (no cap) | `link_wayback`, `upload_archive`, `backfill_*`: maximum items this run |
| `SCOPE` | `all` | `link_wayback`, `upload_archive`: `new` = entries first seen, or whose fid changed, within `NEW_WINDOW` |
| `NEW_WINDOW` | `7d` | what `SCOPE=new` means; lets the daily run retry its own recent failures without touching the backlog |
| `FETCH_OVERLAP` | `2d` | `fetch` |
| `DOWNLOAD_DELAY` | `1` (s) | `download`, `fetch` |
| `RETRY_FAILED` | `0` | `download`: also retry `failed:404` |
| `WAYBACK_REUSE` | `30d` | `link_wayback` |
| `IA_COLLECTION` | `opensource` (Community Texts) | `upload_archive` |
| `SEED_DIR` | `../pmccirc/import/documents` | `seed` |

### `install`
Runs `uv venv && uv sync`.

### `fetch`: incremental
1. `since = max(changed) − FETCH_OVERLAP`, as a unix epoch. If `circulars.json` is empty, the script falls back to `--full`.
2. Pages through `en` with `filter changed > since`, `sort=changed` **ascending** (items edited mid-run land at the end instead of shifting earlier pages),
   `include=field_circular_upload,field_department`. Then runs the same query on `mr` for `title_mr`.
3. Upserts each node, assigns doc_ids, releases held entries that `overrides.json` or `departments.json` now fixes, updates `same_file_as`,
   and records events. A doc_id is never changed.
4. Saves the raw responses under `LFS/punePMC/api/circulars/<run>/`.
5. Prints a summary (new / file replaced / metadata changed / held). On a normal day this is 1–2 requests per language.

### `fetch_full`
Walks the whole list (`sort=drupal_internal__nid`, ~190 pages per language) and does the same upsert as `fetch`. It also marks nids that are no longer listed as
`removed` (their PDFs and items are kept) and refreshes `departments.json` from the taxonomy. This catches anything the `changed` filter could miss.

### `seed`: one-time, **moves** PDFs from pmccirc
For each `ok` entry without a current download, the file `$(SEED_DIR)/<year>/<nid>.pdf` is moved with a same-disk `rename` (instant, no extra space) when:
1. pmccirc's `pdfs.json` says `download_success`;
2. its `url` equals the current `file_url`; and
3. its size equals `filesize`, and the file starts with `%PDF`.

The move goes to `LFS/…/pdfs/<YYYY>/<doc_id>.pdf`, and a `seeded` record is written. For a fid shared by several entries, the first copy is moved; each
further copy is moved too, but only after its sha1 matches, and it is then replaced by a hardlink to the first. Files that don't match stay in pmccirc;
**the pmccirc repo is never deleted.**

Expected result (2026-10-17 scan): all 8,353 of pmccirc's PDFs match and are moved. That leaves ~1,100 entries (new since Aug, pmccirc's failures,
the 5 repaired files), and fewer downloads than that because some share a PDF. If `SEED_DIR` doesn't exist, the step is skipped with a note.

### `download`
For every current fid of an `ok`, non-removed entry: if the fid already has a `downloaded`/`seeded` record, the script only ensures each doc_id path
exists (hardlink, or `ensure_pdf()` on CI). Otherwise it downloads the file once:
1. Streams to `<path>.part` (retries, `Range` resume), then sleeps `DOWNLOAD_DELAY`.
2. Verifies the size against `Content-Length` and the API `filesize`, and checks that the file starts with `%PDF`. Then it computes the sha1 and renames
   the `.part` file over `<path>`. It hardlinks the other doc_id paths, sets `current` on the new fid and clears it on the old one.
3. Creates or updates the symlinks.
4. On failure, it records `failed:<reason>` and increments `attempts`. A `failed:404` is retried only when the fid changes or with `RETRY_FAILED=1`.
5. Exits with a non-zero code on failures other than 404 (404s only warn).

### `link_wayback`
For each fid without `link_success: true`, within `SCOPE`, oldest first, up to `LIMIT`. The steps follow doptIASCivil:
- SPN2 `POST https://web.archive.org/save` with `Authorization: LOW <IA_ACCESS_KEY>:<IA_SECRET_KEY>`,
  `capture_all=1`, `skip_first_archive=1`, `if_not_archived_within=$(WAYBACK_REUSE)`.
- Poll `/save/status/<job_id>` every 5 s, with a 300 s timeout.
- On HTTP 429 or 5xx, back off for 60 s, and stop after 3 consecutive failures.
- The target only needs URLs, not PDFs.

### `upload_archive`
For each `ok` doc_id whose current fid has no `upload_success`, within `SCOPE`, up to `LIMIT`: `ensure_pdf()`, then
`internetarchive.upload("in.gov.pmc.circular.<doc_id>", files={"<doc_id>.pdf": path}, metadata=…, checksum=True, retries=5, retries_sleep=30)`.
A replaced file is uploaded under the same name to the same item, and archive.org keeps the old one in `history/files/`.

Metadata (built in one function):

| Field | Value |
|---|---|
| `collection` | `$(IA_COLLECTION)`: `opensource` (Community Texts) |
| `mediatype` | `texts` |
| `title` | `title_en` if `title_en_is_english`, else `title_mr`. Replaced with the translation later, via `update_archive_metadata` |
| `title_mr` | `title_mr` |
| `title_en_site` | `title_en` as on the site, only when it is real English |
| `date` | circular date (after overrides), ISO `YYYY-MM-DD` |
| `creator` | `<department en> (<department mr>), Pune Municipal Corporation` |
| `publisher` | `Pune Municipal Corporation` |
| `language` | `mar` |
| `subject` | `Pune Municipal Corporation; PMC; Pune; circular; <department>; <year>` |
| `source` | `file_url` |
| `source_page` | `https://www.pmc.gov.in/en/l/circular` |
| `pmc_nid`, `pmc_fid`, `sha1` | ids and checksum |
| `pmc_same_file_as` | the other doc_ids sharing this PDF (if any) |
| `wayback_url` | if linked |
| `description` | HTML: both titles, department, date, then a table of source URL, Wayback URL and download date |

### `update_archive_metadata`
Rebuilds the metadata for every uploaded doc_id and calls `modify_metadata` only where `metadata_sha1` differs (after title, date or department edits,
a new Wayback link, a collection move, or later the translated title). It never uploads the PDFs again.

### `sync_hf_push`
`create_repo("Orgpedia/punePMC", repo_type="dataset", exist_ok=True)`, then `upload_folder(folder_path="LFS/punePMC",
allow_patterns=["publications/circulars/pdfs/**"], commit_message="circulars: <mode> <date>")`.
Only files present in `LFS/` are sent, unchanged ones are skipped, and nothing is ever deleted on HF. So a CI run with a partial `LFS/` is safe.

### `hf_card`
Writes and pushes `README.md` (§8) if it is missing or has changed.

### `import`
`snapshot_download(…, allow_patterns=["publications/circulars/pdfs/**"])` into `LFS/punePMC`, then repairs the symlinks. Use it locally after a fresh clone.

### `status`
Prints:
- counts: entries (ok / held / no_file / removed), downloads (current / failed), Wayback linked and pending, archive.org uploaded and pending;
- the **site vs archive.org check**: entries on the site (last fetch) − `no_file` − held = expected items, compared with the number of
  `in.gov.pmc.circular.*` items from archive.org advanced search, plus the doc_ids missing on either side.

## 6. Modes

### Setup: `make setup` (locally, once)
Runs: `install → fetch_full → seed → download → sync_hf_push → hf_card → upload_archive → status`.

- **No Wayback.** The ~8,900 captures are left to `backfill_wayback`.
- Before `seed`, it stops if any entry is held because of a missing department code, so codes get fixed before any doc_id is frozen.
- `upload_archive` covers ~9,470 items and will take many hours. It is resumable: re-run `make setup` (finished steps are no-ops), or let
  `backfill_archive` on GitHub Actions finish the rest.
- Afterwards, commit `input/documents/` and push. From then on GitHub Actions takes over.

### Daily: `make daily` (GitHub Actions cron, or locally)
Runs: `fetch → download → link_wayback SCOPE=new → upload_archive SCOPE=new → update_archive_metadata → sync_hf_push → status`.

- `SCOPE=new` limits Wayback and archive.org to entries that are new, or whose file changed, within `NEW_WINDOW` (7 days). The daily job therefore stays
  small and retries its own recent failures, and never wanders into the backlog.
- `update_archive_metadata` only touches items whose metadata changed (e.g. PMC edited a title).
- It exits non-zero if any step failed, so GitHub Actions marks the run red.

### Backfill (GitHub Actions manual or cron, or locally)

| Target | Runs | Typical use |
|---|---|---|
| `make backfill_archive LIMIT=500` | `upload_archive SCOPE=all` → `status` | finish archive.org uploads left over from setup |
| `make backfill_wayback LIMIT=200` | `link_wayback SCOPE=all` → `status` | slowly work through the ~8,900 Wayback captures; scheduled daily until done |
| `make backfill_crawl` | `fetch_full → download → upload_archive SCOPE=new → update_archive_metadata → sync_hf_push → status` | weekly safety net: re-crawl the whole site, update the JSON, fetch missing or changed PDFs, catch removals |

## 7. GitHub Actions

Both workflows run the same Makefile targets as a local run, so anything can be reproduced locally with `make <target>`.

**`.github/workflows/daily.yml`**
- `on: schedule: cron "0 16 * * *"` (21:30 IST) and `workflow_dispatch`.
- Steps: checkout → `astral-sh/setup-uv` → `make install` → `make daily` → commit `input/documents/` if it changed
  (`git pull --rebase && git push`, as `github-actions[bot]`) → upload `input/logs/*.log` as a run artifact.
- The commit step runs even when `make daily` fails (`if: always()`), so partial progress is saved.

**`.github/workflows/backfill.yml`**
- `on: workflow_dispatch` with inputs `what` (`archive` | `wayback` | `crawl`) and `limit`, plus
  `schedule` crons for `wayback` (daily, `LIMIT=200`) and `crawl` (weekly, Sunday).
- The steps are the same as daily, with `make backfill_$(what) LIMIT=$(limit)`.
- `timeout-minutes: 330`, under GitHub's 6-hour job limit. `LIMIT` keeps each run well inside it.

Shared:
- `concurrency: group: punepmc-state, cancel-in-progress: false` in **both** workflows, so no two runs ever write the state files at the same time.
  When running locally, `git pull` first and push afterwards.
- Secrets: `HF_TOKEN`, `IA_ACCESS_KEY`, `IA_SECRET_KEY`. They are passed as env vars, and `common.py` reads env first, then `.env`.
- Runs start with an empty `LFS/`, and `ensure_pdf()` fills in only what that run needs (§3). Raw API snapshots are kept only as run artifacts on CI.

## 8. Hugging Face dataset `Orgpedia/punePMC`

- Public. **Files only**: PDFs under `publications/circulars/pdfs/<YYYY>/<doc_id>.pdf`, plus `README.md`. There are no parquet, CSV or JSON metadata files.
- `README.md` front matter sets **`viewer: false`**, which turns off HF's dataset viewer and its automatic parquet conversion
  (`refs/convert/parquet`), so the repo stays plain files.
- README body: what PMC circulars are, the source URL, the naming scheme (§2), the archive.org identifier pattern, the update schedule, and a pointer to the
  GitHub repo, where the metadata lives in `input/documents/`.
- Other publication types later go under `publications/<type>/pdfs/`.

## 9. Environment

| Variable | Used by | Notes |
|---|---|---|
| `HF_TOKEN` | sync_hf_push, hf_card, import, ensure_pdf | Needs write access to `Orgpedia/punePMC`. |
| `HF_REPO_ID` | same | Optional. Default: `Orgpedia/punePMC`. |
| `IA_ACCESS_KEY` / `IA_SECRET_KEY` | link_wayback, upload_archive, update_archive_metadata, status | archive.org S3 keys. |

They come from `.env` locally (see `env.example`) and from repository secrets on GitHub Actions.

## 10. Out of scope

- OCR, translation and extraction (`flow/`, `output/`). The `changes/` files are the hand-off.
- The subject line inside the documents. Only the web-page titles (en + mr) are stored.
- Other PMC publication types. The layout leaves room for `publications/<type>/`.
