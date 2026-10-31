## Bureau rules

These are the bureau's standing rules for every translation. They come from the
bureau's own working notes. The job's language pair is given above; the order's own
instructions (below) win where they disagree with anything here.

The output is a draft that a notary lays next to the original and matches line for
line, so follow the original's shape, not your own idea of a better one.

### 1. Language and names

- The usual target is Georgian (ka). Everything below about Georgian script and
  Georgian wording applies when the target language is Georgian. The structural rules
  in section 2 apply to every target.
- Names and places are transliterated into the target script. Keep the Latin or
  Cyrillic original only where the document itself is machine-readable data: MRZ
  lines, certificate numbers, model codes, company names in a letterhead. Those are
  copied exactly, never transliterated.

Georgian personal names:

- A surname ending in a consonant takes the Georgian nominative -ი:
  ПАЛЬНИКОВ → პალნიკოვი, БОРЗЫХ → ბორზიხი, МАРЧУК → მარჩუკი. A surname ending in a
  vowel stays as it is: ГЕРМАНОВА → გერმანოვა, КЛИМЕНКО → კლიმენკო.
- Russian patronymics use the traditional Georgian form, not transliteration:
  ВЯЧЕСЛАВОВНА → ვიაჩესლავის ასული, ПАВЛОВНА → პაველის ასული, ЮРЬЕВИЧ → იურის ძე,
  ОЛЕГОВИЧ → ოლეგის ძე. The pattern is the given name in the genitive plus ასული for
  a woman, or plus ძე for a man.
- Given names take the Georgian nominative too: ИВАН → ივანი, ЕКАТЕРИНА → ეკატერინა,
  ЕВГЕНИЙ → ევგენი.

### 2. Structure

1. Mirror the original: same section order, same headings, same table shape, same
   field order. Do not reorganise, summarise, merge cells or "improve" the layout.
2. Describe every non-text element in slashes, in the target language, where it
   appears in the original. In Georgian: `/ფოტოსურათი/`, `/მფლობელის ხელმოწერა/`,
   `/ბეჭედი/`, `/QR-კოდი/`, `/ჰოლოგრაფიული დამცავი ნიშანი/`,
   `/ინდოეთის სახელმწიფო გერბი წარწერით „სატიამევა ჯაიატე“/`. Use slashes, not square
   brackets, and stay consistent within one document. This replaces the square
   brackets mentioned in the general rules above.
3. An empty field is written `[-----]`. When the emptiness means something, add a
   slashed note as well, for example `/გრაფა „მოქმედების ვადა (TR)“ შევსებული არ არის/`.
4. A document with several sides gets side headings: `წინა მხარე` and `უკანა მხარე`.
5. Copy exactly, never translate: MRZ lines, document, serial and certificate numbers,
   IBANs, standard codes (EN 1317-5:2007+A2:2012), product and model designations,
   registration plates, web addresses, licence class codes (MCWG, LMV).
6. Dates keep the original's format and separator: `14.11.1999` for a Russian passport,
   `04-10-2004` for an Indian licence. Do not normalise them.
7. Text you cannot read is marked, not guessed: `/გაურკვეველი ჩანაწერი/`.
8. Every translated document ends with a line `თარჯიმანი:` for the translator's
   signature (in another target language, the word for "Translator" followed by a colon).

### 3. Dashes and blank fields

Never use an em dash or an en dash in a translated document, and never type three
hyphens in a row. This applies to every translation.

- A blank, struck-through or not-applicable field is `[-----]`. That is the only
  marker for emptiness: no dash, no hyphen, no "N/A", no empty cell.
- Where a dash would be punctuation, rewrite the sentence with a comma, a colon or
  brackets, or split it in two. Georgian legal and civil-status wording rarely needs
  a dash.
- Hyphens inside things copied exactly stay: dates (`04-10-2004`), standard codes,
  serial and licence numbers, product designations. Those are data.

### 4. Page and type size

You write HTML; the worker builds the Word file and fits it to the original's page
count. You only say one thing about the page: if the original is landscape (its content
is wider than tall, such as a long table, even when the scan is turned sideways), make
the first line of `translation.html` the comment `<!--page: landscape-->`.

Do not try to control sizes or margins, and do not add empty paragraphs or repeated
`br` to push things around. Keep a table in one piece, with all its columns, rather
than splitting it into several. Mirror the original and add nothing to it: that is what
keeps the page count the same.

### 5. Passports

A passport is not translated freehand. Use this layout, with the values replaced:

- Centred and bold: the country name in the target language, then `პასპორტი`.
- A row with three fields: type (`ტიპი`, copied, e.g. `P`), code (`კოდი`, the ISO country
  code copied, e.g. `RUS`) and passport number (`პასპორტის N:`, as printed, spaces
  included).
- A two-column table: on the left a one-cell box holding `/ფოტოსურათი/` in italics, on
  the right the fields in this order: `გვარი`, `სახელი` (given name and patronymic in
  the traditional Georgian form), `მოქალაქეობა` (country name in Georgian),
  `დაბადების თარიღი` (DD.MM.YYYY), `სქესი` (`მდედრობითი` for Ж or F, `მამრობითი` for М or
  M), `დაბადების ადგილი` (Georgian place name only, without the half after the slash),
  `პირადი ნომერი` only where that country's passport has one (Belarus; UK and Korea use
  `პირადი N:`), `გაცემის თარიღი`, `მოქმედების ვადა`, `გამცემი ორგანო`.
- `გამცემი ორგანო`: for Russia the bare numeric code with the ministry prefix dropped
  (`76001`); for Belarus and Korea the authority written out; for the UK `HMPO`.
- The two MRZ lines at the bottom, character for character, `<` fillers included.
  Each line is exactly 44 characters. Never transliterate or re-wrap them.
- Then `თარჯიმანი:`.

### 6. Other document types

- Driving licences: no fixed template. Transcribe both sides in full under
  `წინა მხარე` and `უკანა მხარე`, reproduce the class table as a table, keep class codes
  exactly, describe emblems, holograms and QR codes in slashes.
- Certificates, declarations of performance, test reports: keep every standard
  reference, notified-body number, certificate number and product designation exactly.
  Translate the legal boilerplate fully; do not paraphrase it. Company names and
  addresses stay in the original script, with the city and country translated where
  that reads naturally.
- Commercial documents (quotations, proforma invoices): keep figures, currencies,
  Incoterms and part numbers untouched, and the table structure exactly.
- Civil-status and academic documents: birth certificates, divorce and apostille
  papers, school cards, attestats. An apostille page is part of the document and is
  translated too.
- Medical forms: keep drug names, dosages and ICD codes exactly.

### 7. Before you finish

Read `translation.html` once more and check:

- A passport's MRZ: recompute the check digits of line 2 (passport number at position
  10, date of birth at 20, expiry at 28, and the composite at 44). Weights repeat
  7, 3, 1; `<` counts as 0 and a letter as its alphabet position plus 9. If they do not
  match, read the scan again. If they still do not match, keep your best reading and add
  `/გაურკვეველი ჩანაწერი/` next to that line.
- There is no em dash, no en dash and no three hyphens anywhere. Blank fields show
  `[-----]`.
- No text from an earlier document or a sample is left over.
- The document ends with the `თარჯიმანი:` line, and nothing was added that is not in
  the original.
