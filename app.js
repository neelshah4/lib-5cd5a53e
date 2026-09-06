/* Reading Library — static catalogue browser.
 *
 * Vendored search dependency (no CDN at runtime):
 *   MiniSearch 7.2.0 (UMD build, cdnjs: /ajax/libs/minisearch/7.2.0/umd/index.min.js)
 *   vendor/minisearch.min.js
 *   sha256 9e6fdf53fac7045fc44ab468592e723b38c836c7f105e40c994cc7ac7952829a
 */
(function () {
  "use strict";

  var SECTIONS = ["ECMO", "Respiratory/ARDS", "Shock & Sepsis", "Neurocritical Care",
                  "Cardiac CC", "Renal", "Misc"];
  var SECTION_CLASS = {
    "ECMO": "sec-ecmo", "Respiratory/ARDS": "sec-resp", "Shock & Sepsis": "sec-shock",
    "Neurocritical Care": "sec-neuro", "Cardiac CC": "sec-cardiac", "Renal": "sec-renal",
    "Misc": "sec-misc"
  };
  var CHUNK = 200;
  var MULTI = ["section", "year", "journal", "wissue", "missue", "subtopic", "source", "impact"];
  var ISSUE_DIM = { weekly: "wissue", monthly: "missue" };
  var KINDS = ["monthly", "weekly", "all"];

  var el = function (id) { return document.getElementById(id); };

  /* ── data ────────────────────────────────────────────────── */
  var catalog = [];
  var digests = [];
  var digestById = new Map();   // id -> digest
  var digestOrder = [];         // digests, newest first
  var doiIssues = new Map();    // doi -> [issue ids, newest first]
  var mini = null;              // MiniSearch index
  var miniHasAbstracts = false;
  var abstracts = {};           // doi -> text
  var abstractsState = "off";   // off | loading | none | ready
  var searchHits = null;        // Set of ids, or null when no query

  /* ── state ───────────────────────────────────────────────── */
  var S = {
    view: "library", digest: null, akind: "monthly", iq: "",
    q: "", oa: false, sort: "default", dir: "desc",
    section: [], year: [], journal: [], wissue: [], missue: [], subtopic: [], source: [], impact: []
  };
  var lastFilterLabel = null;
  var journalFilterText = "";
  var facetExpanded = {};
  var expanded = {};            // row id -> true
  var issueOpen = {};           // digest id -> true
  var risState = {};            // digest id -> true (exists) | false (404)
  var archiveHits = null;       // { issues: Set, dois: Set, papers: n } or null
  var archiveRenderMs = null;
  var rendered = 0;
  var filtered = [];

  /* ── small helpers ───────────────────────────────────────── */
  var decoder = document.createElement("textarea");
  function decode(s) {
    if (!s) return "";
    if (s.indexOf("&") === -1) return s;
    decoder.innerHTML = s;
    return decoder.value;
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function nfmt(n) { return n.toLocaleString("en-US"); }
  function latestDigest(r) {
    return (r._issues && r._issues[0]) || null;
  }
  function idKind(id) { return /-W\d{1,2}$/i.test(String(id || "")) ? "weekly" : "monthly"; }
  function kindOf(d) {
    if (d.kind === "weekly" || d.kind === "monthly") return d.kind;
    return idKind(d.id);
  }
  function kindOfId(id) {
    var d = digestById.get(id);
    return d ? d._kind : idKind(id);
  }
  function kindLabel(k) { return k === "weekly" ? "Weekly" : "Monthly"; }
  /* The id carries the issue period (YYYY-MM or YYYY-Www) and is the only field
     guaranteed to be right; `date` is the source-file date and for some archived
     monthly issues points at the day they were exported, not the month covered.
     So order and group on the id, and show `date` only when it agrees with it. */
  function issueYear(d) { return String(d.id || d.date || "").slice(0, 4) || "—"; }
  function periodKey(d) {
    var id = String(d.id || "");
    if (idKind(id) === "weekly") return (d.date && d.date.slice(0, 4) === id.slice(0, 4)) ? d.date : id;
    return id + "-99";                       // a month sorts after its own weeks
  }
  function issueWhen(d) {
    var id = String(d.id || "");
    if (d.date && d.date.slice(0, 7) === id.slice(0, 7)) return d.date;
    if (d.date && d._kind === "weekly" && d.date.slice(0, 4) === id.slice(0, 4)) return d.date;
    return id;
  }

  /* Build the digest indexes and stamp every catalogue record with the issue ids
     that contain it. The digest file is authoritative for membership; a record's
     own `digests` array is folded in so nothing is lost if the two drift. */
  function indexDigests() {
    digestById = new Map();
    doiIssues = new Map();
    digests.forEach(function (d) { d._kind = kindOf(d); digestById.set(d.id, d); });
    digestOrder = digests.slice().sort(function (a, b) {
      return cmpStr(periodKey(b), periodKey(a)) || cmpStr(b.id, a.id);
    });
    function add(doi, id) {
      var arr = doiIssues.get(doi);
      if (!arr) { arr = []; doiIssues.set(doi, arr); }
      if (arr.indexOf(id) === -1) arr.push(id);
    }
    digestOrder.forEach(function (d) { (d.dois || []).forEach(function (doi) { add(doi, d.id); }); });
    catalog.forEach(function (r) {
      (r.digests || []).forEach(function (id) { add(r.doi, id); });
      var ids = doiIssues.get(r.doi) || [];
      // newest first: digestOrder already is, catalog-only ids sort by id
      r._issues = ids.slice().sort(function (a, b) { return cmpStr(b, a); });
    });
  }

  /* ── hash routing ────────────────────────────────────────── */
  function readHash() {
    var h = location.hash.replace(/^#/, "");
    if (!h) h = "/library";
    var qi = h.indexOf("?");
    var path = qi === -1 ? h : h.slice(0, qi);
    var qs = new URLSearchParams(qi === -1 ? "" : h.slice(qi + 1));
    var parts = path.split("/").filter(Boolean);

    S.view = parts[0] === "archive" ? "archive" : "library";
    S.digest = S.view === "archive" && parts[1] ? decodeURIComponent(parts[1]) : null;

    S.akind = qs.get("kind");
    if (KINDS.indexOf(S.akind) === -1) S.akind = "monthly";
    S.iq = qs.get("iq") || "";

    S.q = qs.get("q") || "";
    S.oa = qs.get("oa") === "1";
    S.sort = qs.get("sort") || "default";
    S.dir = qs.get("dir") === "asc" ? "asc" : "desc";
    MULTI.forEach(function (k) {
      var v = qs.get(k);
      S[k] = v ? v.split(",").filter(Boolean) : [];
    });
  }

  function buildHash() {
    if (S.view === "archive") {
      if (S.digest) return "#/archive/" + encodeURIComponent(S.digest);
      var aq = new URLSearchParams();
      aq.set("kind", S.akind);
      if (S.iq) aq.set("iq", S.iq);
      return "#/archive?" + aq.toString();
    }
    var qs = new URLSearchParams();
    if (S.q) qs.set("q", S.q);
    MULTI.forEach(function (k) { if (S[k].length) qs.set(k, S[k].join(",")); });
    if (S.oa) qs.set("oa", "1");
    if (S.sort !== "default") { qs.set("sort", S.sort); qs.set("dir", S.dir); }
    var s = qs.toString();
    return "#/library" + (s ? "?" + s : "");
  }

  var writing = false;
  function pushState() {
    writing = true;
    var next = buildHash();
    if (next !== location.hash) location.hash = next;
    writing = false;
    render();
  }

  /* ── filtering ───────────────────────────────────────────── */
  function passes(r, skip) {
    if (searchHits && !searchHits.has(r._i)) return false;
    if (S.oa && skip !== "oa" && !r.oa) return false;
    for (var i = 0; i < MULTI.length; i++) {
      var k = MULTI[i];
      if (k === skip || !S[k].length) continue;
      if (k === "wissue" || k === "missue") {
        var ids = r._issues || [], hit = false;
        for (var j = 0; j < ids.length; j++) {
          if (S[k].indexOf(ids[j]) !== -1) { hit = true; break; }
        }
        if (!hit) return false;
        continue;
      }
      var v;
      if (k === "year") v = r.year == null ? "" : String(r.year);
      else v = r[k] == null ? "" : String(r[k]);
      if (S[k].indexOf(v) === -1) return false;
    }
    return true;
  }

  function applyFilters() {
    filtered = catalog.filter(function (r) { return passes(r, null); });
    sortRows(filtered);
  }

  function cmpStr(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

  function sortRows(arr) {
    var dir = S.dir === "asc" ? 1 : -1;
    var key = S.sort;
    arr.sort(function (a, b) {
      var x, y, c;
      if (key === "title")   { c = cmpStr(decode(a.title).toLowerCase(), decode(b.title).toLowerCase()); }
      else if (key === "journal") { c = cmpStr(decode(a.journal || "").toLowerCase(), decode(b.journal || "").toLowerCase()); }
      else if (key === "year") { x = a.year == null ? -Infinity : a.year; y = b.year == null ? -Infinity : b.year; c = x < y ? -1 : x > y ? 1 : 0; }
      else if (key === "posted") { c = cmpStr(latestDigest(a) || "", latestDigest(b) || ""); }
      else { /* default: first_seen desc, then year desc */
        c = cmpStr(a.first_seen || "", b.first_seen || "");
        if (c === 0) { x = a.year || 0; y = b.year || 0; c = x < y ? -1 : x > y ? 1 : 0; }
        return -c;
      }
      if (c === 0) c = cmpStr(a.doi, b.doi);
      return c * dir;
    });
  }

  /* ── facets ──────────────────────────────────────────────── */
  function countFor(dim) {
    var map = new Map();
    for (var i = 0; i < catalog.length; i++) {
      var r = catalog[i];
      if (!passes(r, dim)) continue;
      if (dim === "wissue" || dim === "missue") {
        var want = dim === "wissue" ? "weekly" : "monthly";
        var ids = r._issues || [];
        for (var j = 0; j < ids.length; j++) {
          if (kindOfId(ids[j]) !== want) continue;
          map.set(ids[j], (map.get(ids[j]) || 0) + 1);
        }
        continue;
      }
      var v;
      if (dim === "year") v = r.year == null ? null : String(r.year);
      else v = r[dim] == null || r[dim] === "" ? null : String(r[dim]);
      if (v == null) continue;
      map.set(v, (map.get(v) || 0) + 1);
    }
    return map;
  }

  function toggleFacet(dim, value, label) {
    var arr = S[dim];
    var i = arr.indexOf(value);
    if (i === -1) { arr.push(value); lastFilterLabel = label || value; }
    else { arr.splice(i, 1); if (lastFilterLabel === (label || value)) lastFilterLabel = null; }
    resetScroll();
    pushState();
  }

  function activeCount() {
    var n = MULTI.reduce(function (s, k) { return s + S[k].length; }, 0);
    return n + (S.oa ? 1 : 0);
  }

  function facetBlock(title, dim, entries, opts) {
    opts = opts || {};
    var wrap = document.createElement("section");
    wrap.className = "facet";
    var h = document.createElement("h3");
    h.textContent = title;
    wrap.appendChild(h);

    if (opts.search) {
      var inp = document.createElement("input");
      inp.type = "search";
      inp.className = "facet-filter";
      inp.placeholder = "Filter journals";
      inp.value = journalFilterText;
      inp.setAttribute("aria-label", "Type to filter journals");
      inp.addEventListener("input", function () {
        journalFilterText = inp.value;
        var pos = inp.selectionStart;
        renderFacets();
        var again = document.querySelector(".facet-filter");
        if (again) { again.focus(); again.setSelectionRange(pos, pos); }
      });
      wrap.appendChild(inp);
    }

    if (!entries.length) {
      var p = document.createElement("p");
      p.className = "facet-empty";
      p.textContent = "No matches";
      wrap.appendChild(p);
      return wrap;
    }

    var limit = opts.limit || entries.length;
    var open = facetExpanded[dim];
    var shown = open ? entries : entries.slice(0, limit);

    shown.forEach(function (e) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "facet-opt";
      b.setAttribute("aria-pressed", S[dim].indexOf(e.value) !== -1 ? "true" : "false");
      var name = document.createElement("span");
      name.className = "facet-name";
      name.textContent = e.label;
      var n = document.createElement("span");
      n.className = "facet-n";
      n.textContent = nfmt(e.count);
      b.appendChild(name); b.appendChild(n);
      b.addEventListener("click", function () { toggleFacet(dim, e.value, e.label); });
      wrap.appendChild(b);
    });

    if (entries.length > limit) {
      var more = document.createElement("button");
      more.type = "button";
      more.className = "facet-more";
      more.textContent = open ? "Show fewer" : "Show all " + nfmt(entries.length);
      more.addEventListener("click", function () { facetExpanded[dim] = !open; renderFacets(); });
      wrap.appendChild(more);
    }
    return wrap;
  }

  function sortedEntries(map, order) {
    var arr = [];
    map.forEach(function (count, value) { arr.push({ value: value, label: value, count: count }); });
    if (order === "count") arr.sort(function (a, b) { return b.count - a.count || cmpStr(a.label, b.label); });
    else if (order === "desc") arr.sort(function (a, b) { return cmpStr(b.value, a.value); });
    else arr.sort(function (a, b) { return cmpStr(a.label, b.label); });
    return arr;
  }

  function renderFacets() {
    var box = el("facets");
    box.textContent = "";

    // Section — fixed order
    var secMap = countFor("section");
    var secEntries = SECTIONS.filter(function (s) { return secMap.has(s) || S.section.indexOf(s) !== -1; })
      .map(function (s) { return { value: s, label: s, count: secMap.get(s) || 0 }; });
    box.appendChild(facetBlock("Section", "section", secEntries));

    // Year
    box.appendChild(facetBlock("Article year", "year", sortedEntries(countFor("year"), "desc"), { limit: 12 }));

    // Journal
    var jEntries = sortedEntries(countFor("journal"), "count").map(function (e) {
      return { value: e.value, label: decode(e.value), count: e.count };
    });
    if (journalFilterText.trim()) {
      var needle = journalFilterText.trim().toLowerCase();
      jEntries = jEntries.filter(function (e) { return e.label.toLowerCase().indexOf(needle) !== -1; });
    }
    box.appendChild(facetBlock("Journal", "journal", jEntries, { limit: 15, search: true }));

    // Posted splits into the two issue streams; each lists ids newest first.
    var wMap = countFor("wissue");
    if (wMap.size) box.appendChild(facetBlock("Weekly issue", "wissue", sortedEntries(wMap, "desc"), { limit: 12 }));
    var mMap = countFor("missue");
    if (mMap.size) box.appendChild(facetBlock("Monthly issue", "missue", sortedEntries(mMap, "desc"), { limit: 12 }));

    // Subtopic — scoped to selected section(s)
    var subMap = countFor("subtopic");
    if (subMap.size > 1) {
      box.appendChild(facetBlock(S.section.length ? "Subtopic in " + S.section.join(", ") : "Subtopic",
        "subtopic", sortedEntries(subMap, "count"), { limit: 12 }));
    }

    // Source / Impact — shown only when they discriminate
    var srcMap = countFor("source");
    if (srcMap.size > 1) box.appendChild(facetBlock("Source", "source", sortedEntries(srcMap, "count")));
    var impMap = countFor("impact");
    if (impMap.size > 1) box.appendChild(facetBlock("Impact", "impact", sortedEntries(impMap, "count")));

    // OA
    var oaWrap = document.createElement("section");
    oaWrap.className = "facet";
    var oaH = document.createElement("h3");
    oaH.textContent = "Access";
    oaWrap.appendChild(oaH);
    var oaBtn = document.createElement("button");
    oaBtn.type = "button";
    oaBtn.className = "facet-opt";
    oaBtn.setAttribute("aria-pressed", S.oa ? "true" : "false");
    var oaN = catalog.reduce(function (s, r) { return s + (r.oa && passes(r, "oa") ? 1 : 0); }, 0);
    var oaName = document.createElement("span");
    oaName.className = "facet-name";
    oaName.textContent = "Open access only";
    var oaCount = document.createElement("span");
    oaCount.className = "facet-n";
    oaCount.textContent = nfmt(oaN);
    oaBtn.appendChild(oaName); oaBtn.appendChild(oaCount);
    oaBtn.addEventListener("click", function () {
      S.oa = !S.oa;
      if (S.oa) lastFilterLabel = "Open access";
      resetScroll(); pushState();
    });
    oaWrap.appendChild(oaBtn);
    box.appendChild(oaWrap);

    var badge = el("filters-badge");
    var n = activeCount();
    badge.hidden = n === 0;
    badge.textContent = n;
  }

  /* ── chips ───────────────────────────────────────────────── */
  function renderChips() {
    var box = el("chips");
    box.textContent = "";
    var any = false;

    function chip(label, onClear) {
      any = true;
      var b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.setAttribute("aria-label", "Remove filter " + label);
      var t = document.createElement("span");
      t.textContent = label;
      var x = document.createElement("span");
      x.className = "x"; x.setAttribute("aria-hidden", "true"); x.textContent = "×";
      b.appendChild(t); b.appendChild(x);
      b.addEventListener("click", onClear);
      box.appendChild(b);
    }

    if (S.q) chip("Search: " + S.q, function () { S.q = ""; el("q").value = ""; searchHits = null; resetScroll(); pushState(); });
    MULTI.forEach(function (k) {
      S[k].slice().forEach(function (v) {
        var pre = k === "wissue" ? "Weekly " : k === "missue" ? "Monthly " : "";
        chip(pre + decode(v), function () { toggleFacet(k, v); });
      });
    });
    if (S.oa) chip("Open access", function () { S.oa = false; resetScroll(); pushState(); });

    if (any) {
      var c = document.createElement("button");
      c.type = "button";
      c.className = "chip chip-clear";
      c.textContent = "Clear all";
      c.addEventListener("click", function () {
        MULTI.forEach(function (k) { S[k] = []; });
        S.oa = false; S.q = ""; el("q").value = ""; searchHits = null;
        lastFilterLabel = null; resetScroll(); pushState();
      });
      box.appendChild(c);
    }
  }

  /* ── rows ────────────────────────────────────────────────── */
  function issueBadges(r) {
    var ids = r._issues || [];
    if (!ids.length) return "";
    return ids.map(function (id) {
      var k = kindOfId(id);
      return '<a class="ibadge ibadge-' + k + '" href="#/archive/' + esc(encodeURIComponent(id)) +
        '" aria-label="' + kindLabel(k) + ' issue ' + esc(id) + '">' +
        '<span class="ibadge-k" aria-hidden="true">' + (k === "weekly" ? "W" : "M") + '</span>' +
        '<span class="ibadge-id">' + esc(id) + '</span></a>';
    }).join("");
  }

  function rowHTML(r) {
    var badges = issueBadges(r);
    var title = decode(r.title) || "(untitled)";
    var href = "https://doi.org/" + encodeURI(r.doi);
    var cls = SECTION_CLASS[r.section] || "sec-misc";
    var links = "";
    if (r.pmid) {
      links += '<a class="linkicon" target="_blank" rel="noopener" href="https://pubmed.ncbi.nlm.nih.gov/' +
        esc(r.pmid) + '/" aria-label="PubMed record for ' + esc(title) + '">PubMed</a>';
    }
    if (r.oa) links += '<span class="oa-dot" title="Open access">OA</span>';
    links += '<button type="button" class="row-toggle" aria-expanded="' +
      (expanded[r._i] ? "true" : "false") + '" aria-label="Details for ' + esc(title) + '">Details</button>';

    var meta = '<span class="pill ' + cls + '">' + esc(r.section) + "</span>" +
      '<span class="row-meta-text">' + esc(decode(r.journal) || "—") +
      (r.year == null ? "" : " · " + r.year) + "</span>" +
      (badges ? '<span class="row-meta-badges">' + badges + "</span>" : "");
    return '<td class="col-title"><a href="' + esc(href) + '" target="_blank" rel="noopener">' + esc(title) + "</a>" +
      '<span class="row-meta">' + meta + links + "</span></td>" +
      '<td class="col-journal">' + esc(decode(r.journal) || "—") + "</td>" +
      '<td class="col-year">' + (r.year == null ? "—" : r.year) + "</td>" +
      '<td class="col-section"><span class="pill ' + cls + '">' + esc(r.section) + "</span></td>" +
      '<td class="col-posted">' + (badges || "—") + "</td>" +
      '<td class="col-links">' + links + "</td>";
  }

  function ris(r) {
    var L = ["TY  - JOUR"];
    (r.authors || []).forEach(function (a) { L.push("AU  - " + decode(a)); });
    L.push("TI  - " + decode(r.title));
    if (r.journal) L.push("JO  - " + decode(r.journal));
    if (r.year) L.push("PY  - " + r.year);
    if (r.doi) L.push("DO  - " + r.doi);
    if (r.pmid) L.push("AN  - " + r.pmid);
    if (r.url) L.push("UR  - " + r.url);
    if (r.section) L.push("KW  - " + r.section);
    if (r.subtopic) L.push("KW  - " + r.subtopic);
    L.push("ER  - ");
    return L.join("\n");
  }

  function bibtex(r) {
    var first = (r.authors && r.authors[0] || "anon").split(",")[0].replace(/[^A-Za-z]/g, "").toLowerCase();
    var key = first + (r.year || "") + (r.doi.split("/").pop() || "").replace(/[^A-Za-z0-9]/g, "").slice(0, 6);
    var out = ["@article{" + key + ","];
    out.push("  title = {" + decode(r.title) + "},");
    if (r.authors && r.authors.length) out.push("  author = {" + r.authors.map(decode).join(" and ") + "},");
    if (r.journal) out.push("  journal = {" + decode(r.journal) + "},");
    if (r.year) out.push("  year = {" + r.year + "},");
    if (r.doi) out.push("  doi = {" + r.doi + "},");
    if (r.url) out.push("  url = {" + r.url + "}");
    out.push("}");
    return out.join("\n");
  }

  function copyBtn(label, text) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn-secondary";
    b.textContent = label;
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var done = function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = label; }, 1500); };
      var fail = function () { b.textContent = "Copy failed"; setTimeout(function () { b.textContent = label; }, 1500); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fail);
      } else {
        var ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); done(); } catch (e) { fail(); }
        document.body.removeChild(ta);
      }
    });
    return b;
  }

  function detailRow(r) {
    var tr = document.createElement("tr");
    tr.className = "detail";
    tr.dataset.detailFor = String(r._i);
    var td = document.createElement("td");
    td.colSpan = 6;
    var grid = document.createElement("div");
    grid.className = "detail-grid";

    var left = document.createElement("div");
    if (r.authors && r.authors.length) {
      var au = document.createElement("p");
      au.className = "authors";
      au.textContent = r.authors.map(decode).join("; ");
      left.appendChild(au);
    }
    if ((r.take || "").trim()) {
      var tk = document.createElement("p");
      tk.className = "take";
      tk.textContent = decode(r.take);
      left.appendChild(tk);
    }
    var abs = abstracts[r.doi];
    if (abs) {
      var ap = document.createElement("p");
      ap.className = "abstract";
      ap.textContent = abs.slice(0, 300) + (abs.length > 300 ? "…" : "");
      if (r.pmid) {
        ap.appendChild(document.createTextNode(" "));
        var rl = document.createElement("a");
        rl.href = "https://pubmed.ncbi.nlm.nih.gov/" + r.pmid + "/";
        rl.target = "_blank"; rl.rel = "noopener";
        rl.textContent = "Read on PubMed";
        ap.appendChild(rl);
      }
      left.appendChild(ap);
    }
    var actions = document.createElement("div");
    actions.className = "detail-actions";
    actions.appendChild(copyBtn("Copy RIS", ris(r)));
    actions.appendChild(copyBtn("Copy BibTeX", bibtex(r)));
    left.appendChild(actions);

    var right = document.createElement("dl");
    function pair(k, v) {
      if (!v) return;
      var dt = document.createElement("dt"); dt.textContent = k;
      var dd = document.createElement("dd"); dd.textContent = v;
      right.appendChild(dt); right.appendChild(dd);
    }
    pair("DOI", r.doi);
    pair("Subtopic", r.subtopic);
    pair("Tags", (r.tags || []).join(", "));
    pair("Digests", (r._issues || []).join(", "));
    pair("First seen", r.first_seen);
    pair("Source", r.source);

    grid.appendChild(left); grid.appendChild(right);
    td.appendChild(grid); tr.appendChild(td);
    return tr;
  }

  function renderChunk() {
    var body = el("rows");
    var frag = document.createDocumentFragment();
    var end = Math.min(rendered + CHUNK, filtered.length);
    for (var i = rendered; i < end; i++) {
      var r = filtered[i];
      var tr = document.createElement("tr");
      tr.className = "row";
      tr.dataset.i = String(r._i);
      tr.setAttribute("aria-expanded", expanded[r._i] ? "true" : "false");
      tr.innerHTML = rowHTML(r);
      frag.appendChild(tr);
      if (expanded[r._i]) frag.appendChild(detailRow(r));
    }
    rendered = end;
    body.appendChild(frag);
  }

  function resetScroll() { window.scrollTo({ top: 0 }); }

  function renderTable() {
    var body = el("rows");
    body.textContent = "";
    rendered = 0;
    renderChunk();

    var emptyEl = el("empty");
    if (!filtered.length) {
      emptyEl.hidden = false;
      emptyEl.textContent = lastFilterLabel
        ? "No papers match — try removing “" + lastFilterLabel + "”."
        : "No papers match the current filters.";
    } else {
      emptyEl.hidden = true;
    }

    document.querySelectorAll("thead th[data-sort]").forEach(function (th) {
      if (th.dataset.sort === S.sort) th.setAttribute("aria-sort", S.dir === "asc" ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });
  }

  /* ── search ──────────────────────────────────────────────── */
  function buildIndex(withAbstracts) {
    var fields = ["title", "authors", "journal", "doi", "tags", "subtopic", "take"];
    if (withAbstracts) fields.push("abstract");
    var boosts = { title: 3, take: 2 };
    var m = new MiniSearch({
      idField: "_i",
      fields: fields,
      storeFields: [],
      searchOptions: { prefix: true, fuzzy: 0.2, boost: boosts, combineWith: "AND" }
    });
    m.addAll(catalog.map(function (r) {
      return {
        _i: r._i,
        title: decode(r.title),
        authors: (r.authors || []).map(decode).join(" "),
        journal: decode(r.journal || ""),
        doi: r.doi || "",
        tags: (r.tags || []).join(" "),
        subtopic: r.subtopic || "",
        take: decode(r.take || ""),
        abstract: withAbstracts ? (abstracts[r.doi] || "") : ""
      };
    }));
    mini = m;
    miniHasAbstracts = !!withAbstracts;
  }

  function runSearch() {
    if (!S.q) { searchHits = null; return; }
    if (!mini) { buildIndex(abstractsState === "ready"); }
    var res = mini.search(S.q);
    searchHits = new Set(res.map(function (h) { return h.id; }));
  }

  var debounceTimer = null;
  function onSearchInput(value) {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      S.q = value.trim();
      if (S.q) lastFilterLabel = "Search: " + S.q;
      resetScroll();
      pushState();
    }, 120);
  }

  /* ── abstracts ───────────────────────────────────────────── */
  function note(msg) {
    var n = el("abstract-note");
    n.hidden = !msg;
    n.textContent = msg || "";
  }

  function loadAbstracts() {
    abstractsState = "loading";
    note("Loading abstracts…");
    // Only ask for year files the catalogue says exist (has_abstract). When the catalogue
    // carries no flag at all, probe the newest year once rather than one request per year.
    var flagged = Array.from(new Set(catalog.filter(function (r) { return r.has_abstract && r.year; })
      .map(function (r) { return r.year; }))).sort().reverse();
    var years = flagged.length
      ? flagged
      : Array.from(new Set(catalog.map(function (r) { return r.year; }).filter(Boolean))).sort().reverse().slice(0, 1);
    if (!years.length) { abstractsState = "none"; note("No abstracts available yet."); return; }

    fetch("data/abstracts/" + years[0] + ".json")
      .then(function (res) { if (!res.ok) throw new Error("absent"); return res.json(); })
      .then(function (first) {
        Object.assign(abstracts, first);
        return Promise.all(years.slice(1).map(function (y) {
          return fetch("data/abstracts/" + y + ".json")
            .then(function (res) { return res.ok ? res.json() : null; })
            .then(function (j) { if (j) Object.assign(abstracts, j); })
            .catch(function () { });
        }));
      })
      .then(function () {
        abstractsState = "ready";
        note(nfmt(Object.keys(abstracts).length) + " abstracts loaded.");
        buildIndex(true);
        render();
      })
      .catch(function () {
        abstractsState = "none";
        note("No abstracts available yet.");
      });
  }

  /* ── archive ─────────────────────────────────────────────── */
  /* Cards render collapsed: no paper-level DOM is built until a card is
     expanded, so the view stays cheap with ~80 issues and ~4k papers. */

  function byDoi() {
    if (!byDoi._m) {
      var m = new Map();
      catalog.forEach(function (r) { m.set(r.doi, r); });
      byDoi._m = m;
    }
    return byDoi._m;
  }

  function issuesForKind(kind) {
    return digestOrder.filter(function (d) { return kind === "all" || d._kind === kind; });
  }

  function issueTotal(d) {
    return (d.dois || []).length ||
      Object.keys(d.counts || {}).reduce(function (n, k) { return n + d.counts[k]; }, 0);
  }

  function runArchiveSearch() {
    if (!S.iq) { archiveHits = null; return; }
    if (!mini) buildIndex(abstractsState === "ready");
    var res = mini.search(S.iq);
    var issues = new Set(), dois = new Set(), papers = 0;
    for (var i = 0; i < res.length; i++) {
      var r = catalog[res[i].id];
      if (!r) continue;
      var ids = r._issues || [], counted = false;
      for (var j = 0; j < ids.length; j++) {
        if (S.akind !== "all" && kindOfId(ids[j]) !== S.akind) continue;
        if (!digestById.has(ids[j])) continue;
        issues.add(ids[j]); counted = true;
      }
      if (counted) { dois.add(r.doi); papers++; }
    }
    archiveHits = { issues: issues, dois: dois, papers: papers };
  }

  /* Lazy HEAD on expand: an export that was never built is hidden rather than
     offered as a link that 404s. */
  function attachRis(slot, id) {
    function show() {
      var dl = document.createElement("a");
      dl.className = "ris-link";
      dl.href = "exports/digests/" + encodeURIComponent(id) + ".ris";
      dl.textContent = "Download RIS";
      dl.setAttribute("aria-label", "Download RIS for issue " + id);
      slot.textContent = "";
      slot.appendChild(dl);
    }
    if (risState[id] === true) { show(); return; }
    if (risState[id] === false) return;
    fetch("exports/digests/" + encodeURIComponent(id) + ".ris", { method: "HEAD" })
      .then(function (res) {
        risState[id] = !!res.ok;
        if (res.ok) show();
      })
      .catch(function () { risState[id] = false; });
  }

  function paperLine(r, hit) {
    var li = document.createElement("li");
    if (hit) li.className = "hit";
    var link = document.createElement("a");
    link.href = "https://doi.org/" + encodeURI(r.doi);
    link.target = "_blank"; link.rel = "noopener";
    link.textContent = decode(r.title) || r.doi;
    li.appendChild(link);
    var meta = decode(r.journal || "");
    if (r.year) meta += (meta ? " " : "") + r.year;
    if (meta) li.appendChild(document.createTextNode(" — " + meta));
    var take = (r.take || "").trim();
    if (take) {
      var tk = document.createElement("span");
      tk.className = "take";
      tk.textContent = decode(take);
      li.appendChild(tk);
    }
    return li;
  }

  function fillIssueBody(box, d) {
    box.textContent = "";
    var map = byDoi();
    var papers = (d.dois || []).map(function (doi) { return map.get(doi); }).filter(Boolean);
    var missing = (d.dois || []).length - papers.length;

    SECTIONS.forEach(function (sname) {
      var inSec = papers.filter(function (r) { return r.section === sname; });
      if (!inSec.length) return;
      var h = document.createElement("h4");
      h.className = "issue-sec";
      h.textContent = sname + " · " + inSec.length;
      box.appendChild(h);
      var ol = document.createElement("ol");
      inSec.forEach(function (r) {
        ol.appendChild(paperLine(r, !!(archiveHits && archiveHits.dois.has(r.doi))));
      });
      box.appendChild(ol);
    });

    if (!papers.length) {
      var p = document.createElement("p");
      p.className = "issue-note";
      p.textContent = "None of this issue's papers are in the catalogue yet.";
      box.appendChild(p);
    } else if (missing > 0) {
      var m = document.createElement("p");
      m.className = "issue-note";
      m.textContent = missing + (missing === 1 ? " paper is" : " papers are") + " not in the catalogue yet.";
      box.appendChild(m);
    }
  }

  function openIssue(art, on) {
    var id = art.dataset.id;
    var btn = art.querySelector(".issue-toggle");
    var box = art.querySelector(".issue-body");
    var slot = art.querySelector(".ris-slot");
    btn.setAttribute("aria-expanded", on ? "true" : "false");
    box.hidden = !on;
    if (on) {
      issueOpen[id] = true;
      if (!box.dataset.filled) { fillIssueBody(box, digestById.get(id)); box.dataset.filled = "1"; }
      if (slot) attachRis(slot, id);
    } else {
      delete issueOpen[id];
    }
  }

  function issueCard(d) {
    var art = document.createElement("article");
    art.className = "issue";
    art.id = "digest-" + d.id;
    art.dataset.id = d.id;

    var h3 = document.createElement("h3");
    h3.className = "issue-h";
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "issue-toggle";
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-controls", "issue-body-" + d.id);

    var chev = document.createElement("span");
    chev.className = "chev"; chev.setAttribute("aria-hidden", "true");
    var name = document.createElement("span");
    name.className = "issue-name";
    name.textContent = d.title || d.id;
    var badge = document.createElement("span");
    badge.className = "badge badge-" + d._kind;
    badge.textContent = kindLabel(d._kind);
    var date = document.createElement("span");
    date.className = "issue-date";
    date.textContent = issueWhen(d);
    var total = document.createElement("span");
    total.className = "issue-total";
    var n = issueTotal(d);
    total.textContent = nfmt(n) + (n === 1 ? " paper" : " papers");

    btn.appendChild(chev); btn.appendChild(name);
    btn.appendChild(badge); btn.appendChild(date); btn.appendChild(total);
    btn.addEventListener("click", function () {
      openIssue(art, btn.getAttribute("aria-expanded") !== "true");
    });
    h3.appendChild(btn);
    art.appendChild(h3);

    var sub = document.createElement("p");
    sub.className = "counts";
    var parts = SECTIONS.filter(function (x) { return (d.counts || {})[x]; })
      .map(function (x) { return x + " " + d.counts[x]; });
    sub.textContent = parts.join("  ·  ") || "Sections not recorded";
    art.appendChild(sub);

    var tools = document.createElement("p");
    tools.className = "issue-tools";
    var perma = document.createElement("a");
    perma.className = "issue-perma";
    perma.href = "#/archive/" + encodeURIComponent(d.id);
    perma.textContent = "Permalink";
    perma.setAttribute("aria-label", "Permalink to " + (d.title || d.id));
    tools.appendChild(perma);
    var slot = document.createElement("span");
    slot.className = "ris-slot";
    tools.appendChild(slot);
    art.appendChild(tools);

    var box = document.createElement("div");
    box.className = "issue-body";
    box.id = "issue-body-" + d.id;
    box.hidden = true;
    art.appendChild(box);

    return art;
  }

  function scrollToEl(node) {
    if (!node) return;
    var head = parseInt(getComputedStyle(document.documentElement)
      .getPropertyValue("--topbar-h"), 10) || 96;
    var y = node.getBoundingClientRect().top + window.scrollY - head - 16;
    var soft = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: Math.max(0, y), behavior: soft ? "smooth" : "auto" });
  }

  function renderTabs() {
    var wCount = issuesForKind("weekly").length;
    var mCount = issuesForKind("monthly").length;
    var counts = { monthly: mCount, weekly: wCount, all: mCount + wCount };
    document.querySelectorAll("#archive-tabs .tab").forEach(function (t) {
      var k = t.dataset.kind;
      var on = k === S.akind;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      t.textContent = (k === "all" ? "All" : kindLabel(k)) + " · " + nfmt(counts[k]);
    });
    var sel = document.querySelector('#archive-tabs .tab[aria-selected="true"]');
    if (sel) el("archive-body").setAttribute("aria-labelledby", sel.id);
  }

  function renderArchive() {
    var t0 = (window.performance && performance.now) ? performance.now() : 0;
    var body = el("archive-body");
    var nav = el("archive-nav");
    body.textContent = "";
    nav.textContent = "";

    // A permalink pins the tab to the issue's own stream.
    if (S.digest && digestById.has(S.digest)) S.akind = kindOfId(S.digest);
    renderTabs();

    var iq = el("iq");
    if (iq.value !== S.iq) iq.value = S.iq;
    el("iq-clear").hidden = !S.iq;

    if (!digests.length) {
      var box = document.createElement("div");
      box.className = "archive-empty";
      var h = document.createElement("h2");
      h.textContent = "No issues yet";
      var p = document.createElement("p");
      p.textContent = "The first issue will appear here after the next run; until then the whole catalogue is browsable in the Library.";
      box.appendChild(h); box.appendChild(p);
      body.appendChild(box);
      el("archive-summary").textContent = "";
      archiveRenderMs = ((window.performance && performance.now) ? performance.now() : 0) - t0;
      return;
    }

    runArchiveSearch();

    var list = issuesForKind(S.akind);
    var kindTotal = list.length;
    if (archiveHits) list = list.filter(function (d) { return archiveHits.issues.has(d.id); });

    // Summary line
    var sum = el("archive-summary");
    if (archiveHits) {
      sum.textContent = nfmt(list.length) + (list.length === 1 ? " issue · " : " issues · ") +
        nfmt(archiveHits.papers) + (archiveHits.papers === 1 ? " paper matches" : " papers match");
    } else {
      var papersHere = list.reduce(function (n, d) { return n + issueTotal(d); }, 0);
      sum.textContent = nfmt(kindTotal) + (kindTotal === 1 ? " issue · " : " issues · ") +
        nfmt(papersHere) + " papers";
    }

    if (S.digest && !digestById.has(S.digest)) {
      var miss = document.createElement("p");
      miss.className = "empty";
      miss.textContent = "No issue with id “" + S.digest + "”.";
      body.appendChild(miss);
      archiveRenderMs = ((window.performance && performance.now) ? performance.now() : 0) - t0;
      return;
    }

    if (!list.length) {
      var e = document.createElement("p");
      e.className = "empty";
      e.textContent = archiveHits
        ? "No issues match “" + S.iq + "”."
        : (S.akind === "weekly"
          ? "No weekly issues are archived yet."
          : S.akind === "monthly"
            ? "No monthly issues are archived yet."
            : "No issues are archived yet.");
      body.appendChild(e);
      archiveRenderMs = ((window.performance && performance.now) ? performance.now() : 0) - t0;
      return;
    }

    var frag = document.createDocumentFragment();
    var years = [];
    var seen = null;
    list.forEach(function (d) {
      var y = issueYear(d);
      if (y !== seen) {
        seen = y;
        years.push({ year: y, n: 0 });
        var yh = document.createElement("h2");
        yh.className = "yr-head";
        yh.id = "yr-" + y;
        yh.textContent = y;
        frag.appendChild(yh);
      }
      years[years.length - 1].n++;
      frag.appendChild(issueCard(d));
    });
    body.appendChild(frag);

    // Year timeline
    var navH = document.createElement("h3");
    navH.textContent = "Years";
    nav.appendChild(navH);
    var strip = document.createElement("div");
    strip.className = "yr-strip";
    years.forEach(function (y) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "yr-chip";
      b.setAttribute("aria-label", "Jump to " + y.year + ", " + y.n + (y.n === 1 ? " issue" : " issues"));
      var lab = document.createElement("span"); lab.textContent = y.year;
      var n = document.createElement("span"); n.className = "yr-n"; n.textContent = y.n;
      b.appendChild(lab); b.appendChild(n);
      b.addEventListener("click", function () { scrollToEl(document.getElementById("yr-" + y.year)); });
      strip.appendChild(b);
    });
    nav.appendChild(strip);

    // Re-open whatever was open, plus the permalinked issue.
    if (S.digest) issueOpen[S.digest] = true;
    Object.keys(issueOpen).forEach(function (id) {
      var art = body.querySelector('.issue[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
      if (art) openIssue(art, true);
    });
    if (S.digest) {
      var target = document.getElementById("digest-" + S.digest);
      if (target) { target.classList.add("is-target"); setTimeout(function () { scrollToEl(target); }, 0); }
    }

    archiveRenderMs = ((window.performance && performance.now) ? performance.now() : 0) - t0;
    window.__archiveRenderMs = archiveRenderMs;
  }

  /* ── render ──────────────────────────────────────────────── */
  function render() {
    var navKind = S.view === "archive"
      ? (S.digest && digestById.has(S.digest) ? kindOfId(S.digest) : S.akind)
      : null;
    document.querySelectorAll(".seg").forEach(function (seg) {
      var on = seg.dataset.nav === S.view &&
        (S.view !== "archive" || navKind === "all" || seg.dataset.kind === navKind);
      if (on) seg.setAttribute("aria-current", "page");
      else seg.removeAttribute("aria-current");
    });

    el("view-library").hidden = S.view !== "library";
    el("view-archive").hidden = S.view !== "archive";

    if (S.view === "archive") {
      renderArchive();
      window.__archiveRenderMs = archiveRenderMs;
      el("count").textContent = digests.length
        ? nfmt(digests.length) + (digests.length === 1 ? " issue" : " issues") + " archived"
        : "No issues yet";
      return;
    }

    runSearch();
    applyFilters();
    renderFacets();
    renderChips();
    renderTable();

    var n = filtered.length;
    el("count").textContent = nfmt(n) + (n === 1 ? " paper" : " papers") +
      (n !== catalog.length ? " of " + nfmt(catalog.length) : "");
  }

  /* ── events ──────────────────────────────────────────────── */
  function wire() {
    window.addEventListener("hashchange", function () {
      if (writing) return;
      readHash();
      var q = el("q");
      if (q.value !== S.q) q.value = S.q;
      render();
    });

    var tabs = Array.prototype.slice.call(document.querySelectorAll("#archive-tabs .tab"));
    tabs.forEach(function (t, i) {
      t.addEventListener("click", function () {
        S.akind = t.dataset.kind;
        S.digest = null;
        resetScroll();
        pushState();
        var again = document.getElementById(t.id);
        if (again) again.focus();
      });
      t.addEventListener("keydown", function (e) {
        var d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1
          : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1
            : e.key === "Home" ? -999 : e.key === "End" ? 999 : 0;
        if (!d) return;
        e.preventDefault();
        var next = d === -999 ? 0 : d === 999 ? tabs.length - 1
          : (i + d + tabs.length) % tabs.length;
        tabs[next].focus();
        tabs[next].click();
      });
    });

    var iqTimer = null;
    el("iq").addEventListener("input", function (e) {
      var v = e.target.value;
      clearTimeout(iqTimer);
      iqTimer = setTimeout(function () {
        S.iq = v.trim();
        S.digest = null;
        pushState();
        var again = el("iq");
        if (again && document.activeElement !== again) again.focus();
      }, 140);
    });
    el("iq").addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.target.value = ""; S.iq = ""; pushState(); }
    });
    el("iq-clear").addEventListener("click", function () {
      el("iq").value = ""; S.iq = ""; S.digest = null; pushState(); el("iq").focus();
    });

    el("q").addEventListener("input", function (e) { onSearchInput(e.target.value); });
    el("q").addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.target.value = ""; onSearchInput(""); }
    });

    document.addEventListener("keydown", function (e) {
      var t = e.target;
      var typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
      if (e.key === "/" && !typing) { e.preventDefault(); el("q").focus(); }
      if (e.key === "Escape" && railOpen()) closeRail();
    });

    document.querySelectorAll("thead th[data-sort] button").forEach(function (b) {
      b.addEventListener("click", function () {
        var key = b.parentElement.dataset.sort;
        if (S.sort === key) S.dir = S.dir === "asc" ? "desc" : "asc";
        else { S.sort = key; S.dir = key === "year" || key === "posted" ? "desc" : "asc"; }
        resetScroll();
        pushState();
      });
    });

    el("rows").addEventListener("click", function (e) {
      var toggle = e.target.closest(".row-toggle");
      if (!toggle && e.target.closest("a, button")) return;
      var tr = e.target.closest("tr.row");
      if (!tr) return;
      var i = Number(tr.dataset.i);
      var rec = catalog[i];
      var toggles = tr.querySelectorAll(".row-toggle");
      if (expanded[i]) {
        delete expanded[i];
        tr.setAttribute("aria-expanded", "false");
        toggles.forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
        var d = el("rows").querySelector('tr.detail[data-detail-for="' + i + '"]');
        if (d) d.remove();
      } else {
        expanded[i] = true;
        tr.setAttribute("aria-expanded", "true");
        toggles.forEach(function (b) { b.setAttribute("aria-expanded", "true"); });
        tr.parentNode.insertBefore(detailRow(rec), tr.nextSibling);
      }
    });

    el("abstracts").addEventListener("change", function (e) {
      if (e.target.checked) {
        if (abstractsState === "ready") { note(nfmt(Object.keys(abstracts).length) + " abstracts loaded."); }
        else if (abstractsState === "none") { note("No abstracts available yet."); }
        else loadAbstracts();
      } else {
        note("");
        if (miniHasAbstracts) buildIndex(false);
      }
      render();
    });

    el("filters-open").addEventListener("click", openRail);
    el("rail-close").addEventListener("click", closeRail);
    el("rail-scrim").addEventListener("click", closeRail);

    var io = new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting && rendered < filtered.length) renderChunk();
    }, { rootMargin: "600px" });
    io.observe(el("sentinel"));

    el("theme-toggle").addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      var next = cur === "dark" ? "light" : cur === "light" ? "dark"
        : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark");
      document.documentElement.setAttribute("data-theme", next);
      el("theme-glyph").textContent = next === "dark" ? "Light" : "Dark";
      try { localStorage.setItem("rl-theme", next); } catch (err) { /* storage blocked */ }
    });
  }

  function railOpen() { return el("rail").dataset.open === "true"; }
  function openRail() {
    el("rail").dataset.open = "true";
    el("rail-scrim").hidden = false;
    el("filters-open").setAttribute("aria-expanded", "true");
    var first = el("rail").querySelector("button, input");
    if (first) first.focus();
  }
  function closeRail() {
    el("rail").dataset.open = "false";
    el("rail-scrim").hidden = true;
    el("filters-open").setAttribute("aria-expanded", "false");
    el("filters-open").focus();
  }

  function syncTopbarHeight() {
    var h = document.querySelector(".topbar").getBoundingClientRect().height;
    document.documentElement.style.setProperty("--topbar-h", Math.round(h) + "px");
  }

  function initTheme() {
    var saved = new URLSearchParams(location.search).get("theme");
    if (saved !== "dark" && saved !== "light") {
      saved = null;
      try { saved = localStorage.getItem("rl-theme"); } catch (err) { /* storage blocked */ }
    }
    if (saved === "dark" || saved === "light") document.documentElement.setAttribute("data-theme", saved);
    var dark = saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    el("theme-glyph").textContent = dark ? "Light" : "Dark";
  }

  /* ── boot ────────────────────────────────────────────────── */
  function boot() {
    initTheme();
    syncTopbarHeight();
    window.addEventListener("resize", syncTopbarHeight);
    readHash();
    el("q").value = S.q;
    wire();

    Promise.all([
      fetch("data/catalog.json").then(function (r) { return r.json(); }),
      fetch("data/digests.json").then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; })
    ]).then(function (out) {
      catalog = out[0];
      catalog.forEach(function (r, i) { r._i = i; });
      digests = Array.isArray(out[1]) ? out[1] : [];
      indexDigests();

      // The Posted column and facet only exist once something has been posted.
      if (!catalog.some(function (r) { return r._issues && r._issues.length; })) {
        document.getElementById("table").classList.add("no-digests");
      }

      var newest = catalog.reduce(function (m, r) {
        return r.first_seen && r.first_seen > m ? r.first_seen : m;
      }, "");
      el("foot-count").textContent = nfmt(catalog.length) + " records";
      el("foot-updated").textContent = newest ? "Last updated " + newest : "Last updated —";

      render();

      var idle = window.requestIdleCallback || function (f) { return setTimeout(f, 200); };
      idle(function () { if (!mini) buildIndex(false); });
    }).catch(function (err) {
      el("count").textContent = "Could not load the catalogue.";
      el("empty").hidden = false;
      el("empty").textContent = "The catalogue file could not be loaded (" + err.message + ").";
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
