-- One-off repair for data imported before the loader lower-cased language codes.
-- Documents of imported orders kept "Uz" while the language row is "uz".
-- Touches only order documents of orders whose source starts with 'tarjimnebi:'.
-- Run in psql on the server; replace the tenant slug. Safe to run twice.

BEGIN;

-- What would change (read this first).
SELECT d.source_language, d.target_language, count(*) AS documents
FROM order_documents d
JOIN orders o ON o.id = d.order_id
JOIN tenants t ON t.id = o.tenant_id
WHERE t.slug = 'translation-house-llc-2'
  AND o.source LIKE 'tarjimnebi:%'
  AND (d.source_language <> lower(d.source_language)
       OR d.target_language <> lower(d.target_language))
GROUP BY 1, 2;

UPDATE order_documents d
SET source_language = lower(d.source_language),
    target_language = lower(d.target_language)
FROM orders o
JOIN tenants t ON t.id = o.tenant_id
WHERE o.id = d.order_id
  AND t.slug = 'translation-house-llc-2'
  AND o.source LIKE 'tarjimnebi:%'
  AND (d.source_language <> lower(d.source_language)
       OR d.target_language <> lower(d.target_language));

-- Any code still without a language row (should return no rows).
SELECT DISTINCT d.source_language AS code
FROM order_documents d
JOIN orders o ON o.id = d.order_id
JOIN tenants t ON t.id = o.tenant_id
WHERE t.slug = 'translation-house-llc-2' AND o.source LIKE 'tarjimnebi:%'
  AND d.source_language NOT IN (SELECT code FROM languages WHERE tenant_id = t.id)
UNION
SELECT DISTINCT d.target_language
FROM order_documents d
JOIN orders o ON o.id = d.order_id
JOIN tenants t ON t.id = o.tenant_id
WHERE t.slug = 'translation-house-llc-2' AND o.source LIKE 'tarjimnebi:%'
  AND d.target_language NOT IN (SELECT code FROM languages WHERE tenant_id = t.id);

-- Check the output above, then change ROLLBACK to COMMIT.
ROLLBACK;
