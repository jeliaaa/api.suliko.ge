/*
 * Read-only extractor for app.tarjimnebi.ge.
 *
 * Run it INSIDE the signed-in tarjimnebi tab (browser console, or the Browser
 * pane's javascript tool). It issues GET requests only and never calls a page
 * function, so nothing in tarjimnebi is changed. Your session cookie never
 * leaves the browser: results are POSTed to scripts/tarjimnebi/receive.py on
 * 127.0.0.1:8765.
 *
 *   TJ.run()                    everything
 *   TJ.run({ only: ['notaries'] })
 *   TJ.run({ limit: 20 })       first 20 orders only (a trial)
 *
 * One thing to know: fetching an order's comments marks them read for the
 * signed-in user in tarjimnebi. Orders that still have unread comments are
 * therefore skipped (listed in orders.skipped_comments) rather than
 * silently read.
 */
(() => {
  const RECEIVER = 'http://127.0.0.1:8765/save?name=';
  const PAUSE_MS = 120;
  const PARALLEL = 3;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const money = (s) => {
    const m = String(s || '').replace(/,/g, '').match(/-?\d+(?:\.\d+)?/);
    return m ? Math.round(parseFloat(m[0]) * 100) / 100 : null;
  };
  const pad = (n) => String(n).padStart(2, '0');
  const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parseDate = (s) => {
    const d = new Date(s);
    return isNaN(d) ? null : isoDate(d);
  };
  const parseDateTime = (s) => {
    const d = new Date(norm(s).replace(/\b(am|pm)\b/i, (x) => x.toUpperCase()));
    return isNaN(d) ? null : `${isoDate(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };

  async function get(url) {
    for (let attempt = 1; attempt <= 3; attempt++) {
      const res = await fetch(url, { credentials: 'include', method: 'GET' });
      if (res.ok) {
        if (res.redirected && /login/i.test(res.url)) throw new Error('Signed out: ' + res.url);
        return res.text();
      }
      if (attempt === 3) throw new Error(`${res.status} ${url}`);
      await sleep(1000 * attempt);
    }
  }
  const doc = async (url) => new DOMParser().parseFromString(await get(url), 'text/html');

  // Results always stay in TJ.data. They are also sent to the local receiver
  // when the page is allowed to reach it (a normal Chrome tab is; the Claude
  // Browser pane blocks loopback, so there TJ.download() is used instead).
  let receiverUp = true;
  async function post(name, data) {
    TJ.data[name] = data;
    if (!receiverUp) return;
    try {
      const res = await fetch(RECEIVER + name, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(data),
      });
      if (!res.ok) throw new Error('receiver said ' + res.status);
    } catch (_) {
      receiverUp = false;
      console.warn('[TJ] receiver not reachable; keeping results in TJ.data (use TJ.download())');
    }
  }

  async function pool(items, worker, onProgress) {
    const out = new Array(items.length);
    let next = 0;
    let done = 0;
    await Promise.all(
      Array.from({ length: PARALLEL }, async () => {
        while (next < items.length) {
          const i = next++;
          out[i] = await worker(items[i], i);
          done++;
          if (onProgress && done % 25 === 0) onProgress(done, items.length);
          await sleep(PAUSE_MS);
        }
      }),
    );
    return out;
  }

  // ── Listing ids ──────────────────────────────────────────────────────────
  async function idsFromList(page, pattern, paged) {
    const ids = new Set();
    for (let p = 1; p < 400; p++) {
      const d = await doc(paged ? `${page}?page=${p}` : page);
      const before = ids.size;
      d.querySelectorAll('a[href]').forEach((a) => {
        const m = a.getAttribute('href').match(pattern);
        if (m) ids.add(Number(m[1]));
      });
      if (!paged || ids.size === before) break;
      await sleep(PAUSE_MS);
    }
    return [...ids].sort((a, b) => a - b);
  }

  // ── Form helpers (edit pages hold the cleanest values) ──────────────────
  const val = (d, name) => {
    const e = d.querySelector(`[name="${name}"]`);
    return e ? (e.tagName === 'SELECT' ? e.value : e.value) : null;
  };
  const text = (d, name) => norm(val(d, name)) || null;
  const selectMap = (d, name) =>
    Object.fromEntries(
      [...d.querySelectorAll(`select[name="${name}"] option`)]
        .filter((o) => o.value)
        .map((o) => [o.value, norm(o.textContent)]),
    );

  async function clientRecord(id) {
    const d = await doc(`edit_client.php?id=${id}`);
    const group = val(d, 'acquisition_source_group');
    return {
      id,
      name: text(d, 'client_name'),
      client_type: text(d, 'client_type'),
      personal_number: text(d, 'personalnum'),
      email: text(d, 'email'),
      phone: text(d, 'phone'),
      address: text(d, 'address'),
      acquisition_source: text(d, 'acquisition_source') || text(d, 'acquisition_source_new'),
      acquisition_group: group || null,
    };
  }

  async function translatorRecord(id) {
    const d = await doc(`edit_translator.php?id=${id}`);
    // The edit page fills languages_json with JavaScript, so read the pairs
    // from the view page's "Language Pairs & Prices" table instead.
    const view = await doc(`view_translator.php?id=${id}`);
    const pairs = [...view.querySelectorAll('table')]
      .filter((t) => /From/.test(t.textContent) && /Price \/ Page/.test(t.textContent))
      .flatMap((t) => [...t.querySelectorAll('tbody tr')])
      .map((tr) => [...tr.children].map((td) => norm(td.textContent)))
      .filter((c) => c.length >= 4 && /^[A-Za-z]{2,3}$/.test(c[0]))
      .map((c) => ({ from: c[0].toLowerCase(), to: c[2].toLowerCase(), price: money(c[3]) }));
    return {
      id,
      name: text(d, 'translator_name'),
      experience_from: text(d, 'experience_from'),
      email: text(d, 'email'),
      phone: text(d, 'phone_number'),
      office_address: text(d, 'office_address'),
      comment: text(d, 'comment'),
      default_rate: money(val(d, 'price')),
      language_pairs: pairs,
      bank_iban: text(d, 'bank_iban'),
      bank_inn: text(d, 'bank_inn'),
      bank_code: text(d, 'bank_code'),
    };
  }

  async function notaryRecord(id) {
    const d = await doc(`edit_notary.php?id=${id}`);
    return {
      id,
      name: text(d, 'notary_name'),
      phone: text(d, 'phone_number'),
      email: text(d, 'email'),
      registration_number: text(d, 'registration_number'),
      office_address: text(d, 'office_address'),
      comment: text(d, 'comment'),
      bank_iban: text(d, 'bank_iban'),
      bank_inn: text(d, 'bank_inn'),
      bank_code: text(d, 'bank_code'),
    };
  }

  // ── Orders ───────────────────────────────────────────────────────────────
  const idIn = (el, re) => {
    const m = (el.getAttribute('onclick') || '').match(re);
    return m ? Number(m[1]) : null;
  };

  function paymentRow(row) {
    const parts = [...(row.children[0] ? row.children[0].children : [])].map((c) => norm(c.textContent));
    const dateLine = parts.find((p) => /[A-Z][a-z]{2} \d{1,2}, \d{4}/.test(p)) || '';
    const [datePart, ...methodParts] = dateLine.split('—');
    const dateMatch = (datePart || dateLine).match(/[A-Z][a-z]{2} \d{1,2}, \d{4}/);
    const amount = row.querySelector('.fw-semibold');
    return {
      date: dateMatch ? parseDate(dateMatch[0]) : null,
      method: norm(methodParts.join('—')) || null,
      amount: amount ? money(amount.textContent) : null,
      batch: parts.some((p) => /batch/i.test(p)),
      note: parts.filter((p) => p && p !== dateLine && !/batch/i.test(p)).join(' | ') || null,
    };
  }

  /** Walk a card in document order, tracking which person's block each payment row sits under. */
  function payoutRows(card, personRe, deleteRe) {
    if (!card) return [];
    const out = [];
    let person = null;
    const walk = (el) => {
      if (el.tagName === 'A') {
        const m = (el.getAttribute('href') || '').match(personRe);
        if (m) person = Number(m[1]);
      }
      const modal = el.getAttribute && el.getAttribute('onclick');
      if (modal) {
        const pm = modal.match(/openPay\w*\((\d+)/);
        if (pm) person = Number(pm[1]);
      }
      if (el.classList && el.classList.contains('align-items-start') && el.querySelector('.fw-semibold')) {
        const del = [...el.querySelectorAll('[onclick]')].map((b) => idIn(b, deleteRe)).find(Boolean);
        out.push({ payment_id: del || null, person_id: person, ...paymentRow(el) });
      }
      [...el.children].forEach(walk);
    };
    walk(card);
    return out;
  }

  function parseView(d, statusByLabel) {
    const card = (id) => d.getElementById(id);
    const payments = [...(card('payments-history-list') ? card('payments-history-list').children : [])]
      .filter((r) => r.querySelector('.fw-semibold'))
      .map((r) => ({
        payment_id: [...r.querySelectorAll('[onclick]')].map((b) => idIn(b, /deleteClientPayment\((\d+)/)).find(Boolean) || null,
        ...paymentRow(r),
      }));

    const expenses = [...d.querySelectorAll('#expenses-card [onclick*="deleteExpense"]')].map((b) => {
      const row = b.closest('div.d-flex') || b.parentElement;
      return { expense_id: idIn(b, /deleteExpense\w*\((\d+)/), text: norm((row.parentElement || row).textContent) };
    });

    const history = [...d.querySelectorAll('.timeline-item')].map((it) => {
      const badge = norm((it.querySelector('.badge') || {}).textContent);
      const small = it.querySelector('small');
      const lines = small ? [...small.childNodes].map((n) => norm(n.textContent)).filter(Boolean) : [];
      return {
        status: statusByLabel[badge] || badge,
        status_label: badge,
        user: lines[0] || null,
        at: lines[1] ? parseDateTime(lines[1]) : null,
      };
    });

    return {
      payments,
      expenses,
      notary_payments: payoutRows(card('notary-payouts-card'), /view_notary\.php\?id=(\d+)/, /deleteNotaryPaymentOnOrder\((\d+)/),
      translator_payments: payoutRows(card('translator-payouts-card'), /view_translator\.php\?id=(\d+)/, /deleteTranslatorPayoutOnOrder\((\d+)/),
      history,
    };
  }

  async function orderRecord(id, ctx) {
    const e = await doc(`edit_translation.php?id=${id}`);
    if (!val(e, 'client_id')) return { id, missing: true };
    if (!ctx.statusByLabel) {
      const st = selectMap(e, 'status');
      ctx.statusByLabel = Object.fromEntries(Object.entries(st).map(([k, v]) => [v, k]));
      ctx.statuses = st;
      ctx.documentTypes = selectMap(e, 'documents[0][document_type_id]');
      ctx.languages = selectMap(e, 'documents[0][source_language]');
    }
    const documents = [];
    for (let i = 0; val(e, `documents[${i}][document_id]`) !== null; i++) {
      const k = (f) => `documents[${i}][${f}]`;
      const nz = (f) => val(e, k(f)) || null;
      const notary = e.querySelector(`[name="${k('notarized')}"]`);
      documents.push({
        document_id: Number(val(e, k('document_id'))),
        document_type_id: nz('document_type_id') && Number(nz('document_type_id')),
        source_language: nz('source_language'),
        target_language: nz('target_language'),
        translator_id: nz('translator_id') && Number(nz('translator_id')),
        notarized: !!(notary && notary.checked),
        notary_id: nz('notary_id') && Number(nz('notary_id')),
        copy_type: nz('copy_type'),
        page_count: Number(nz('page_count') || 1),
        translator_cost: money(val(e, k('translator_cost'))) || 0,
        notary_cost: money(val(e, k('notary_cost'))) || 0,
        price: money(val(e, k('price'))) || 0,
      });
    }
    const v = parseView(await doc(`view_translation.php?id=${id}`), ctx.statusByLabel);
    let comments = null;
    if (!ctx.unread.has(id)) {
      const raw = JSON.parse(await get(`ajax/order_comments.php?action=get&translation_id=${id}&since=0`));
      comments = (raw.comments || []).map((c) => ({
        comment_id: c.comment_id,
        user_id: c.user_id,
        author_name: c.author_name,
        message: c.message,
        pinned: !!Number(c.pinned),
        created_at: c.created_at,
      }));
    }
    return {
      id,
      date: val(e, 'date'),
      due_date: val(e, 'duedate') || null,
      client_id: Number(val(e, 'client_id')),
      handover_methods: [...e.querySelectorAll('input[name="handover_method[]"]')].filter((c) => c.checked).map((c) => c.value),
      delivery_address: text(e, 'delivery_address'),
      status: val(e, 'status'),
      delivery_cost: money(val(e, 'delivery_cost')) || 0,
      contact_info: text(e, 'contact_info'),
      google_drive_folder: text(e, 'google_drive_folder'),
      documents,
      comments,
      ...v,
    };
  }

  /** Every expense, from the Finances page over an all-time date range. */
  async function expenseRows() {
    const d = await doc('finances.php?start_date=2000-01-01&end_date=2100-12-31');
    const table = [...d.querySelectorAll('table')].find(
      (t) => /Category/.test(t.textContent) && /Description/.test(t.textContent),
    );
    if (!table) return [];
    return [...table.querySelectorAll('tbody tr')]
      .map((tr) => [...tr.children].map((td) => norm(td.textContent)))
      .filter((c) => /^\d{4}-\d{2}-\d{2}$/.test(c[0]))
      .map((c) => ({
        date: c[0],
        category: c[1],
        description: c[2],
        order_id: (c[3].match(/#(\d+)/) || [])[1] ? Number(c[3].match(/#(\d+)/)[1]) : null,
        amount: money(c[4]),
      }));
  }

  // ── Run ──────────────────────────────────────────────────────────────────
  const TJ = (window.TJ = { log: [], progress: '', data: {} });
  TJ.download = (name = 'tarjimnebi-dump.json') => {
    const blob = new Blob([JSON.stringify(TJ.data)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  const say = (m) => {
    TJ.progress = m;
    TJ.log.push(m);
    console.log('[TJ]', m);
  };

  TJ.run = async (opts = {}) => {
    const only = opts.only || ['notaries', 'translators', 'clients', 'expenses', 'orders'];
    const t0 = Date.now();

    if (only.includes('notaries')) {
      const ids = await idsFromList('notaries.php', /view_notary\.php\?id=(\d+)/, false);
      await post('notaries', await pool(ids, notaryRecord));
      say(`notaries: ${ids.length}`);
    }
    if (only.includes('translators')) {
      const ids = await idsFromList('translators.php', /view_translator\.php\?id=(\d+)/, true);
      await post('translators', await pool(ids, translatorRecord));
      say(`translators: ${ids.length}`);
    }
    if (only.includes('clients')) {
      const ids = await idsFromList('clients.php', /view_client\.php\?id=(\d+)/, true);
      say(`clients: listing found ${ids.length}`);
      await post('clients', await pool(ids, clientRecord, (d, n) => say(`clients ${d}/${n}`)));
    }
    if (only.includes('expenses')) {
      const rows = await expenseRows();
      await post('expenses', rows);
      say(`expenses: ${rows.length}`);
    }
    if (only.includes('orders')) {
      let ids = await idsFromList('translations.php', /view_translation\.php\?id=(\d+)/, true);
      if (opts.limit) ids = ids.slice(-opts.limit);
      say(`orders: listing found ${ids.length}`);
      const summary = JSON.parse(await get('ajax/order_comments.php?action=summary'));
      const ctx = { unread: new Set((summary.items || []).map((i) => Number(i.translation_id))) };
      const results = await pool(ids, (id) => orderRecord(id, ctx), (d, n) => say(`orders ${d}/${n}`));
      const orders = results.filter((o) => !o.missing);
      const part = 250;
      for (let i = 0; i * part < orders.length; i++) {
        await post(`orders_${String(i).padStart(3, '0')}`, orders.slice(i * part, (i + 1) * part));
      }
      await post('reference', {
        statuses: ctx.statuses,
        document_types: ctx.documentTypes,
        languages: ctx.languages,
        skipped_comments: [...ctx.unread].filter((id) => ids.includes(id)),
        extracted_at: new Date().toISOString(),
      });
      say(`orders: ${orders.length} saved, ${results.length - orders.length} missing, ${ctx.unread.size} had unread comments (comments skipped)`);
    }
    say(`done in ${Math.round((Date.now() - t0) / 1000)}s`);
    return TJ.progress;
  };
  console.log('TJ loaded. Start the receiver, then run TJ.run({limit: 20}) as a trial.');
})();
