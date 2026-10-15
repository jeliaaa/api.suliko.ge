# Import samples

Two invented `.xls` files (Excel 97-2003) for the one-file import (`tests/test_imports_combined.py`).
Every person, phone, email, ID and IBAN is made up (`example.com` emails, `GE00...` IBANs).

- `sample-1-workbook-by-sheets.xls`: one workbook, a sheet per kind (orders first on purpose), and a cover sheet.
- `sample-2-one-flat-sheet.xls`: one flat sheet, one row per order, with the client, translator and notary on every row.

The rows worth a second look are listed in the test file next to what is asserted about them.
