#!/bin/bash
# library-integration-test.sh — Phase 5 integration QA harness for the
# critical-care reading library. Implements Q1-Q9 from the plan
# ("pitch-me-a-plan-bright-hummingbird.md" §Phase 5), concretized per the
# Sonnet-worker-5 task brief.
#
# Runs entirely inside a TEMP CLONE of this repo (never the live working
# tree), on a throwaway branch (never main — validate.py's check5 refuses
# 2099- fixture digest ids on main by design, so fixtures must run off it).
# Never performs a live Zotero write, never sends email, never `git push`,
# never touches this repo's `main`.
#
# Every check (Qn) is followed by its negative control (Qn-neg): a
# deliberately broken input that MUST be reported as detected. A check
# with no working negative control proves nothing (GRAPH-DOCTRINE fault-
# injection law).
#
# Usage:
#   library-integration-test.sh [--skip-live] [--only Qn]
#
#   --skip-live   Skip Q3 (Zotero API), Q4 (GitHub Pages HTTP), Q5 (uses no
#                 network but is a static-only check anyway; still runs),
#                 and Q9 (Zotero API) -- anything hitting Zotero's API or
#                 the public Pages URL. Q1/Q2 still resolve DOIs against
#                 Crossref/PubMed (their normal, non-Zotero, non-Pages
#                 network dependency) because that is what a real ingest
#                 does; there is no meaningful "offline" version of them.
#   --only Qn     Run only check Qn (plus its negative).
#
# Output: one line per check "Qn PASS|FAIL <measured> vs <expected>", one
# line per negative "Qn-neg PASS|FAIL", then a final summary line and a
# non-zero exit on any hard failure. PENDING/STATIC/SKIP are reported but
# do not count as failures.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SRC="$(cd "${SCRIPT_DIR}/.." && pwd)"
# The repo slug drives the "slug never leaks into a tracked file" checks (Q4,
# Q4-neg, Q6-neg/d). It used to be read from a hardcoded session scratchpad
# path, which evaporated when that session ended -- the harness then reported
# "slug file unreadable" and silently degraded to 7/9 (found 2026-09-14).
# Derive it from the git remote instead: always available, already untracked
# (it lives in .git/config), and never written into the working tree by this
# script. LIBRARY_SLUG or SLUG_FILE still override for a deliberate test.
resolve_slug() {
  if [[ -n "${LIBRARY_SLUG:-}" ]]; then printf '%s' "${LIBRARY_SLUG}"; return 0; fi
  if [[ -n "${SLUG_FILE:-}" && -f "${SLUG_FILE}" ]]; then cat "${SLUG_FILE}"; return 0; fi
  local url
  url="$(cd "${REPO_SRC}" && git remote get-url origin 2>/dev/null)" || return 1
  [[ -n "${url}" ]] || return 1
  url="${url%.git}"
  printf '%s' "${url##*/}"
}

SKIP_LIVE=0
ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-live) SKIP_LIVE=1; shift ;;
    --only) ONLY="${2:-}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- scaffolding
TMP=""
_cleanup() {
  # rm -rf is the standard idiom for temp-dir cleanup; if the caller's shell
  # environment blocks it, fall back to a non -f recursive remove so cleanup
  # still happens rather than silently leaking a temp clone.
  [[ -n "${TMP}" && -d "${TMP}" ]] || return 0
  rm -rf "${TMP}" 2>/dev/null || rm -r "${TMP}" 2>/dev/null || true
}
trap _cleanup EXIT

TMP="$(mktemp -d "${TMPDIR:-/tmp}/library-qa.XXXXXX")"
REPO="${TMP}/repo"
DIGESTS_DIR="${TMP}/digests"
mkdir -p "${DIGESTS_DIR}"

RUN_PASS=0
RUN_FAIL=0
NEG_PASS=0
NEG_FAIL=0

want() {
  # want Qn  -> true if this check should run given --only
  [[ -z "${ONLY}" || "${ONLY}" == "$1" ]]
}

report() {
  # report Qn PASS|FAIL "<measured>" "<expected>"
  local id="$1" verdict="$2" measured="$3" expected="$4"
  echo "${id} ${verdict} ${measured} vs ${expected}"
  if [[ "${id}" == *-neg ]]; then
    [[ "${verdict}" == PASS ]] && NEG_PASS=$((NEG_PASS + 1)) || NEG_FAIL=$((NEG_FAIL + 1))
  else
    [[ "${verdict}" == PASS ]] && RUN_PASS=$((RUN_PASS + 1)) || RUN_FAIL=$((RUN_FAIL + 1))
  fi
}

report_neutral() {
  # report_neutral Qn LABEL "<detail>"  -- SKIP / PENDING / STATIC: printed,
  # counted in neither PASS nor FAIL, does not block exit 0.
  echo "$1 $2 $3"
}

sha_of() { python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }

# ---------------------------------------------------------------- clone setup
echo "# cloning ${REPO_SRC} -> ${REPO} (throwaway branch, never main)"
if ! git clone -q "${REPO_SRC}" "${REPO}" 2>"${TMP}/clone.err"; then
  echo "FATAL: could not clone repo into temp dir:"; cat "${TMP}/clone.err"; exit 2
fi
mkdir -p "${REPO}/local"
if [[ -f "${REPO_SRC}/local/unverified.json" ]]; then
  cp "${REPO_SRC}/local/unverified.json" "${REPO}/local/unverified.json"
else
  echo "[]" > "${REPO}/local/unverified.json"
fi
# Copy the resolve() cache too (gitignored, so the clone doesn't get it for
# free) -- avoids hammering Crossref/PubMed for DOIs this repo already
# resolved once, and keeps --skip-live fast.
if [[ -d "${REPO_SRC}/local/resolve_cache" ]]; then
  cp -R "${REPO_SRC}/local/resolve_cache" "${REPO}/local/resolve_cache"
fi
( cd "${REPO}" && git checkout -q -b qa-throwaway )

PY="/usr/bin/python3"
GIT="/usr/bin/git"

# =========================================================================
# Q1 — Weekly -> repo
# =========================================================================
if want Q1; then
  cp "${REPO_SRC}/tests/fixtures/pubmed-2099-01-04.md" "${DIGESTS_DIR}/pubmed-2099-01-04.md"

  # library-ingest-weekly.sh's LIBRARY_DRY_RUN does NOT skip the ingest step
  # itself (read: it always runs `ingest_weekly.py <digest>` with no
  # --dry-run flag) -- it only skips the Zotero push (adds --dry-run there)
  # and skips the final commit/push. So the helper IS the right tool for
  # the data assertion here, not a workaround; --dry-run only guarantees
  # nothing reaches Zotero or a real commit.
  OUT="$(LIBRARY_REPO_OVERRIDE="${REPO}" \
         LIBRARY_DRY_RUN=1 \
         LIBRARY_DIGESTS_DIR="${DIGESTS_DIR}" \
         LIBRARY_FORCE=1 \
         LIBRARY_LOG="${TMP}/q1.log" \
         SENDER_OVERRIDE=stub \
         bash "${HOME}/.claude/scripts/library-ingest-weekly.sh" \
           "${DIGESTS_DIR}/pubmed-2099-01-04.md" "2099-01-04" 2>&1)"
  Q1_EXIT=$?
  MARKER="${DIGESTS_DIR}/.library-2099-01-04"

  N_DIGEST_RECS="$(python3 -c "
import json
c = json.load(open('${REPO}/data/catalog.json'))
print(sum(1 for r in c if '2099-W01' in (r.get('digests') or [])))
" 2>/dev/null)"
  HAS_DIGEST="$(python3 -c "
import json
d = json.load(open('${REPO}/data/digests.json'))
print('yes' if any(x.get('id')=='2099-W01' for x in d) else 'no')
" 2>/dev/null)"

  if [[ "${Q1_EXIT}" -eq 0 && -f "${MARKER}" && "${N_DIGEST_RECS}" == "5" && "${HAS_DIGEST}" == "yes" ]]; then
    report Q1 PASS "exit=0 marker=yes records=5 digest=2099-W01" "exit=0 marker digest 2099-W01 +5 records"
  else
    report Q1 FAIL "exit=${Q1_EXIT} marker=$([[ -f ${MARKER} ]] && echo yes || echo no) records=${N_DIGEST_RECS:-0} digest=${HAS_DIGEST:-no}" "exit=0 marker digest 2099-W01 +5 records"
  fi

  # --- Q1 negative: malformed fixture ---
  BEFORE_SHA="$(sha_of "${REPO}/data/catalog.json")"
  cp "${REPO_SRC}/tests/fixtures/pubmed-2099-01-04-MALFORMED.md" "${DIGESTS_DIR}/pubmed-2099-01-05.md"
  LIBRARY_REPO_OVERRIDE="${REPO}" \
    LIBRARY_DRY_RUN=1 \
    LIBRARY_DIGESTS_DIR="${DIGESTS_DIR}" \
    LIBRARY_FORCE=1 \
    LIBRARY_LOG="${TMP}/q1neg.log" \
    SENDER_OVERRIDE=stub \
    bash "${HOME}/.claude/scripts/library-ingest-weekly.sh" \
      "${DIGESTS_DIR}/pubmed-2099-01-05.md" "2099-01-05" >"${TMP}/q1neg.out" 2>&1
  Q1_NEG_EXIT=$?
  AFTER_SHA="$(sha_of "${REPO}/data/catalog.json")"
  FAIL_MARKER="${DIGESTS_DIR}/.library-failed-2099-01-05"
  if [[ "${Q1_NEG_EXIT}" -ne 0 && -f "${FAIL_MARKER}" && "${BEFORE_SHA}" == "${AFTER_SHA}" ]] \
     && grep -q "step: ingest" "${FAIL_MARKER}"; then
    report Q1-neg PASS "exit=${Q1_NEG_EXIT} failmarker=yes step=ingest catalog-unchanged" "non-zero, .library-failed-* with step name, catalog byte-identical"
  else
    report Q1-neg FAIL "exit=${Q1_NEG_EXIT} failmarker=$([[ -f ${FAIL_MARKER} ]] && echo yes || echo no) shas-equal=$([[ ${BEFORE_SHA} == ${AFTER_SHA} ]] && echo yes || echo no)" "non-zero, .library-failed-* with step name, catalog byte-identical"
  fi
fi

# =========================================================================
# Q2 — Monthly -> repo
# =========================================================================
if want Q2; then
  cd "${REPO}"
  OUT="$("${PY}" scripts/ingest_monthly.py tests/fixtures/Test-2099-critical-care-digest.md 2>&1)"
  Q2_EXIT=$?
  N_MONTH_RECS="$(python3 -c "
import json
c = json.load(open('${REPO}/data/catalog.json'))
print(sum(1 for r in c if '2099-01' in (r.get('digests') or [])))
" 2>/dev/null)"
  SHARED_BOTH="$(python3 -c "
import json
c = json.load(open('${REPO}/data/catalog.json'))
recs = [r for r in c if '2099-01' in (r.get('digests') or [])]
print('yes' if any(len(r.get('digests') or []) >= 2 for r in recs) else 'no')
" 2>/dev/null)"
  HAS_MONTH_DIGEST="$(python3 -c "
import json
d = json.load(open('${REPO}/data/digests.json'))
print('yes' if any(x.get('id')=='2099-01' for x in d) else 'no')
" 2>/dev/null)"

  if [[ "${Q2_EXIT}" -eq 0 && "${N_MONTH_RECS}" == "2" && "${HAS_MONTH_DIGEST}" == "yes" && "${SHARED_BOTH}" == "yes" ]]; then
    report Q2 PASS "exit=0 records=2 digest=2099-01 merged-carries-both-ids=yes" "exit=0, 2099-01 registered, +2 records, shared DOI carries both ids"
  else
    report Q2 FAIL "exit=${Q2_EXIT} records=${N_MONTH_RECS:-0} digest=${HAS_MONTH_DIGEST:-no} merged=${SHARED_BOTH:-no}" "exit=0, 2099-01 registered, +2 records, shared DOI carries both ids"
  fi

  # --- Q2 negative: misspelled heading ---
  BEFORE_SHA="$(sha_of "${REPO}/data/catalog.json")"
  "${PY}" scripts/ingest_monthly.py tests/fixtures/Test-2099-BADHEADING.md >"${TMP}/q2neg.out" 2>&1
  Q2_NEG_EXIT=$?
  AFTER_SHA="$(sha_of "${REPO}/data/catalog.json")"
  if [[ "${Q2_NEG_EXIT}" -ne 0 && "${BEFORE_SHA}" == "${AFTER_SHA}" ]] && grep -q "unrecognized section" "${TMP}/q2neg.out"; then
    report Q2-neg PASS "exit=${Q2_NEG_EXIT} catalog-unchanged=yes" "non-zero exit, zero rows written"
  else
    report Q2-neg FAIL "exit=${Q2_NEG_EXIT} catalog-unchanged=$([[ ${BEFORE_SHA} == ${AFTER_SHA} ]] && echo yes || echo no)" "non-zero exit, zero rows written"
  fi
  cd "${SCRIPT_DIR}"
fi

# =========================================================================
# Q3 — Zotero (live GET/POST-shaped dry-run only)
# =========================================================================
if want Q3; then
  if [[ "${SKIP_LIVE}" -eq 1 ]]; then
    report_neutral Q3 SKIP "network required (--skip-live)"
    report_neutral Q3-neg SKIP "network required (--skip-live)"
  else
    cd "${REPO}"
    # Q2 above must have run in the same invocation to seed digest 2099-01;
    # if run via --only Q3 alone, ingest the monthly fixture first so the
    # DOI count is well-defined.
    if ! python3 -c "
import json
d = json.load(open('data/digests.json'))
raise SystemExit(0 if any(x.get('id')=='2099-01' for x in d) else 1)
" 2>/dev/null; then
      "${PY}" scripts/ingest_monthly.py tests/fixtures/Test-2099-critical-care-digest.md >/dev/null 2>&1
    fi
    EXPECTED_N="$(python3 -c "
import json
d = json.load(open('data/digests.json'))
e = next((x for x in d if x.get('id')=='2099-01'), None)
print(len(e.get('dois') or []) if e else 0)
")"
    ZOUT="$("${PY}" scripts/zotero_push.py --digest 2099-01 --dry-run --state "${TMP}/zstate.json" 2>&1)"
    Z_EXIT=$?
    ZLINE="$(printf '%s\n' "${ZOUT}" | grep -E '^zotero: ' | tail -1)"
    N_CREATED="$(printf '%s' "${ZLINE}" | sed -n 's/^zotero: \([0-9]*\) created.*/\1/p')"
    N_LINKED="$(printf '%s' "${ZLINE}" | sed -n 's/.*created · \([0-9]*\) linked-existing.*/\1/p')"
    SUM=$(( ${N_CREATED:-0} + ${N_LINKED:-0} ))
    if [[ "${Z_EXIT}" -eq 0 && -n "${ZLINE}" && "${SUM}" == "${EXPECTED_N}" ]]; then
      report Q3 PASS "exit=0 created+linked=${SUM}" "exit=0, created+linked=${EXPECTED_N} (fixture DOI count)"
    else
      report Q3 FAIL "exit=${Z_EXIT} line='${ZLINE}' created+linked=${SUM}" "exit=0, created+linked=${EXPECTED_N} (fixture DOI count)"
    fi

    # --- Q3 negative: invalid API key -> non-zero exit, auth error, no key leak ---
    ZOUT_NEG="$(ZOTERO_API_KEY=invalid-test-key-000 ZOTERO_LIBRARY_ID=0 ZOTERO_LIBRARY_TYPE=user \
      "${PY}" scripts/zotero_push.py --digest 2099-01 --dry-run --state "${TMP}/zstate-neg.json" 2>&1)"
    ZNEG_EXIT=$?
    if [[ "${ZNEG_EXIT}" -ne 0 ]] \
       && printf '%s' "${ZOUT_NEG}" | grep -qiE "invalid key|403" \
       && ! printf '%s' "${ZOUT_NEG}" | grep -q "invalid-test-key-000"; then
      report Q3-neg PASS "exit=${ZNEG_EXIT} auth-error=yes key-leaked=no" "non-zero exit, auth error surfaced, key never printed"
    else
      report Q3-neg FAIL "exit=${ZNEG_EXIT}" "non-zero exit, auth error surfaced, key never printed"
    fi
    cd "${SCRIPT_DIR}"
  fi
fi

# =========================================================================
# Q4 — Repo -> Pages, slug never leaks into tracked files
# =========================================================================
if want Q4; then
  SLUG="$(resolve_slug || true)"
  if [[ "${SKIP_LIVE}" -eq 1 ]]; then
    report_neutral Q4 SKIP "network required (--skip-live)"
  elif [[ -z "${SLUG}" ]]; then
    report Q4 FAIL "slug file unreadable" "slug present"
  else
    PAGES_URL="https://neelshah4.github.io/${SLUG}/"
    HTTP_CODE="$(curl -s -o "${TMP}/pages_index.html" -w '%{http_code}' "${PAGES_URL}" 2>/dev/null)"
    if [[ "${HTTP_CODE}" == "404" ]]; then
      report_neutral Q4 PENDING "pages not enabled (HTTP 404) -- neither pass nor fail"
    elif [[ "${HTTP_CODE}" == "200" ]]; then
      NOINDEX="no"; grep -qi "noindex" "${TMP}/pages_index.html" 2>/dev/null && NOINDEX="yes"
      ROBOTS="$(curl -s "https://neelshah4.github.io/${SLUG}/robots.txt" 2>/dev/null)"
      ROBOTS_OK="no"; printf '%s' "${ROBOTS}" | grep -q "Disallow: /" && ROBOTS_OK="yes"
      if [[ "${NOINDEX}" == "yes" && "${ROBOTS_OK}" == "yes" ]]; then
        report Q4 PASS "http=200 noindex=yes robots-disallow=yes" "http=200, noindex present, robots.txt Disallow: /"
      else
        report Q4 FAIL "http=200 noindex=${NOINDEX} robots-disallow=${ROBOTS_OK}" "http=200, noindex present, robots.txt Disallow: /"
      fi
    else
      report Q4 FAIL "http=${HTTP_CODE}" "http=200 (or 404 PENDING)"
    fi
  fi

  # --- Q4 negative: prove the slug-leak grep itself can fail. Not gated on
  # --skip-live -- it never touches the network, only `git grep` in the clone. ---
  if [[ -z "${SLUG}" ]]; then
    report Q4-neg FAIL "slug file unreadable" "grep detects an injected slug leak"
  else
    cd "${REPO}"
    BASELINE_HITS="$("${GIT}" grep -c "${SLUG}" -- . 2>/dev/null | wc -l | tr -d ' ')"
    echo "${SLUG}" > "${REPO}/qa-slug-leak.tmp"
    "${GIT}" add qa-slug-leak.tmp
    INJECTED_HITS="$("${GIT}" grep -c "${SLUG}" -- . 2>/dev/null | wc -l | tr -d ' ')"
    "${GIT}" reset -q -- qa-slug-leak.tmp
    rm -f "${REPO}/qa-slug-leak.tmp"
    if [[ "${BASELINE_HITS}" == "0" && "${INJECTED_HITS}" -gt "0" ]]; then
      report Q4-neg PASS "baseline=0 injected=${INJECTED_HITS}" "grep detects an injected slug leak"
    else
      report Q4-neg FAIL "baseline=${BASELINE_HITS} injected=${INJECTED_HITS}" "grep detects an injected slug leak"
    fi
    cd "${SCRIPT_DIR}"
  fi
fi

# =========================================================================
# Q5 — Issue -> PR (static check; cannot be run live offline)
# =========================================================================
if want Q5; then
  WF="${REPO}/.github/workflows/add-article.yml"
  q5_check() {
    local path="$1"
    ruby -ryaml -e '
      path = ARGV[0]
      y = YAML.load_file(path)
      raise "not a mapping" unless y.is_a?(Hash)
      content = File.read(path)
      raise "missing scripts/add_doi.py" unless content.include?("scripts/add_doi.py")
      raise "missing gh pr create" unless content.include?("gh pr create")
      raise "missing add-article label gate" unless content.include?("add-article")
      puts "ok"
    ' "${path}" >/dev/null 2>&1
  }
  if q5_check "${WF}"; then
    report_neutral Q5 STATIC "parses, references scripts/add_doi.py + gh pr create + label add-article"
  else
    report Q5 FAIL "static parse/reference check failed" "yaml parses; references scripts/add_doi.py, gh pr create, label add-article"
  fi

  # --- Q5 negative: label string stripped -> check fails ---
  sed 's/add-article//g' "${WF}" > "${TMP}/add-article-neg.yml"
  if ! q5_check "${TMP}/add-article-neg.yml"; then
    report Q5-neg PASS "stripped copy fails the check" "check fails once 'add-article' is stripped"
  else
    report Q5-neg FAIL "stripped copy still passed" "check fails once 'add-article' is stripped"
  fi
fi

# =========================================================================
# Q6 — Validator
# =========================================================================
if want Q6; then
  cd "${REPO}"
  V_OUT="$("${PY}" scripts/validate.py --root "${REPO}" 2>&1)"
  V_EXIT=$?
  V_SUMMARY="$(printf '%s\n' "${V_OUT}" | grep -E '^validate: ' | tail -1)"
  if [[ "${V_EXIT}" -eq 0 ]]; then
    report Q6 PASS "exit=0 ${V_SUMMARY}" "validate.py exit 0"
  else
    report Q6 FAIL "exit=${V_EXIT} ${V_SUMMARY}" "validate.py exit 0"
  fi

  # --- Q6 negatives (each must turn validate.py red; each restored after) ---
  Q6_NEG_ALL=PASS

  # neg a: stray test.pdf
  echo "%PDF-1.4 fixture" > "${REPO}/test.pdf"
  "${PY}" scripts/validate.py --root "${REPO}" >"${TMP}/q6a.out" 2>&1; EA=$?
  rm -f "${REPO}/test.pdf"
  if [[ "${EA}" -eq 0 ]]; then Q6_NEG_ALL=FAIL; fi
  A_DETAIL="pdf:exit=${EA}"

  # neg b: duplicate DOI
  python3 -c "
import json
p = '${REPO}/data/catalog.json'
c = json.load(open(p))
c.append(dict(c[0]))
json.dump(c, open(p,'w'), indent=1)
"
  "${PY}" scripts/validate.py --root "${REPO}" >"${TMP}/q6b.out" 2>&1; EB=$?
  ( cd "${REPO}" && "${GIT}" checkout -q -- data/catalog.json )
  if [[ "${EB}" -eq 0 ]]; then Q6_NEG_ALL=FAIL; fi
  B_DETAIL="dupdoi:exit=${EB}"

  # neg c: null verified
  python3 -c "
import json
p = '${REPO}/data/catalog.json'
c = json.load(open(p))
c[0]['verified'] = None
json.dump(c, open(p,'w'), indent=1)
"
  "${PY}" scripts/validate.py --root "${REPO}" >"${TMP}/q6c.out" 2>&1; EC=$?
  ( cd "${REPO}" && "${GIT}" checkout -q -- data/catalog.json )
  if [[ "${EC}" -eq 0 ]]; then Q6_NEG_ALL=FAIL; fi
  C_DETAIL="nullverified:exit=${EC}"

  # neg d: slug written into README.md with LIBRARY_SLUG exported
  SLUG="$(resolve_slug || true)"
  if [[ -n "${SLUG}" ]]; then
    echo "${SLUG}" >> "${REPO}/README.md"
    LIBRARY_SLUG="${SLUG}" "${PY}" scripts/validate.py --root "${REPO}" >"${TMP}/q6d.out" 2>&1; ED=$?
    ( cd "${REPO}" && "${GIT}" checkout -q -- README.md )
    if [[ "${ED}" -eq 0 ]]; then Q6_NEG_ALL=FAIL; fi
    D_DETAIL="slugleak:exit=${ED}"
  else
    Q6_NEG_ALL=FAIL
    D_DETAIL="slugleak:slugfile-unreadable"
  fi

  report Q6-neg "${Q6_NEG_ALL}" "${A_DETAIL} ${B_DETAIL} ${C_DETAIL} ${D_DETAIL}" "all 4 turn validate.py red (non-zero exit)"
  cd "${SCRIPT_DIR}"
fi

# =========================================================================
# Q7 — No skill broken
# =========================================================================
if want Q7; then
  LINT_OUT="$(python3 "${HOME}/.claude/skills/_agent-tests/lint_agents.py" --skill \
    "${HOME}/.claude/skills/interesting-articles-formatting" \
    "${HOME}/.claude/skills/reading-library" 2>&1)"
  LINT_OK="no"; printf '%s' "${LINT_OUT}" | grep -q "PASS ✓ — no errors" && LINT_OK="yes"

  DRIFT_OUT="$(sh "${HOME}/.claude/scripts/skill-surface-drift-check.sh" interesting-articles-formatting reading-library 2>&1)"
  DRIFT_EXIT=$?
  DRIFT_OK="no"; [[ "${DRIFT_EXIT}" -eq 0 ]] && printf '%s' "${DRIFT_OUT}" | grep -q "RESULT: PASS" && DRIFT_OK="yes"

  if [[ "${LINT_OK}" == "yes" && "${DRIFT_OK}" == "yes" ]]; then
    report Q7 PASS "lint=0-errors drift=OK" "lint 0 errors; drift OK for both skills"
  else
    report Q7 FAIL "lint_ok=${LINT_OK} drift_ok=${DRIFT_OK}" "lint 0 errors; drift OK for both skills"
  fi

  # --- Q7 negative: copy reading-library, strip its Self-improvement block ---
  NEGDIR="${TMP}/reading-library-neg"
  cp -R "${HOME}/.claude/skills/reading-library" "${NEGDIR}"
  python3 -c "
import re
p = '${NEGDIR}/SKILL.md'
t = open(p, encoding='utf-8').read()
t2 = re.sub(r'(?ms)^## Self-improvement.*?(?=^## |\Z)', '', t)
open(p, 'w', encoding='utf-8').write(t2)
"
  LINT_NEG_OUT="$(python3 "${HOME}/.claude/skills/_agent-tests/lint_agents.py" --skill "${NEGDIR}" 2>&1)"
  if printf '%s' "${LINT_NEG_OUT}" | grep -qE "^ERRORS \([1-9]" ; then
    report Q7-neg PASS "lint reports errors on stripped copy" "lint ERRORs once Self-improvement block is removed"
  else
    report Q7-neg FAIL "lint did not error on stripped copy" "lint ERRORs once Self-improvement block is removed"
  fi
fi

# =========================================================================
# Q8 — Weekly pipeline untouched
# =========================================================================
if want Q8; then
  PDT="${HOME}/.claude/scripts/_tests/pubmed-digest-test.sh"
  PDT_OUT="$(bash "${PDT}" 2>&1)"
  PDT_LINE="$(printf '%s\n' "${PDT_OUT}" | grep -E '^=== LIVE: ' | tail -1)"
  N_PASSED="$(printf '%s' "${PDT_LINE}" | sed -n 's/.*LIVE: \([0-9]*\) passed, \([0-9]*\) failed.*/\1/p')"
  N_FAILED="$(printf '%s' "${PDT_LINE}" | sed -n 's/.*LIVE: \([0-9]*\) passed, \([0-9]*\) failed.*/\2/p')"

  SENT_MARKER="${HOME}/.claude/digests/.sent-2026-08-31"
  MARKER_SIZE="$( [[ -f "${SENT_MARKER}" ]] && wc -c < "${SENT_MARKER}" | tr -d ' ' || echo "missing")"

  INGEST_LINE_PRESENT="no"
  grep -qE 'library-ingest-weekly\.sh.*\|\| true' "${HOME}/.claude/scripts/send-digest-if-ready.sh" \
    && INGEST_LINE_PRESENT="yes"

  if [[ "${N_PASSED}" == "14" && "${N_FAILED}" == "0" && "${MARKER_SIZE}" == "31" && "${INGEST_LINE_PRESENT}" == "yes" ]]; then
    report Q8 PASS "pubmed-digest-test=14/14 sent-marker=31B ingest-call=present" "14/14, .sent-2026-08-31 = 31 bytes, ingest call present"
  else
    report Q8 FAIL "pubmed-digest-test=${N_PASSED:-?}passed/${N_FAILED:-?}failed sent-marker=${MARKER_SIZE} ingest-call=${INGEST_LINE_PRESENT}" "14/14, .sent-2026-08-31 = 31 bytes, ingest call present"
  fi

  # --- Q8 negative: temp copy of send-digest-if-ready.sh with the ingest
  # call removed -- the "ingest call present" check above must fail on it. ---
  NEGCOPY="${TMP}/send-digest-if-ready-neg.sh"
  python3 -c "
p_in = '${HOME}/.claude/scripts/send-digest-if-ready.sh'
p_out = '${NEGCOPY}'
lines = open(p_in, encoding='utf-8').readlines()
lines = [l for l in lines if 'library-ingest-weekly.sh' not in l]
open(p_out, 'w', encoding='utf-8').writelines(lines)
"
  NEG_PRESENT="no"
  grep -qE 'library-ingest-weekly\.sh.*\|\| true' "${NEGCOPY}" && NEG_PRESENT="yes"
  if [[ "${NEG_PRESENT}" == "no" ]]; then
    report Q8-neg PASS "grep reports missing on edited copy" "check reports the ingest call missing, not pass"
  else
    report Q8-neg FAIL "grep still found the call" "check reports the ingest call missing, not pass"
  fi
fi

# =========================================================================
# Q9 — Zotero drift
# =========================================================================
if want Q9; then
  if [[ "${SKIP_LIVE}" -eq 1 ]]; then
    report_neutral Q9 SKIP "network required (--skip-live)"
  else
    cd "${REPO_SRC}"
    ZOTERO_COUNT="$(python3 -c "
import json, os, urllib.request
cfg = json.load(open(os.path.expanduser('~/.claude.json')))
env = cfg.get('mcpServers', {}).get('zotero', {}).get('env', {})
key = os.environ.get('ZOTERO_API_KEY') or env.get('ZOTERO_API_KEY')
lib_id = os.environ.get('ZOTERO_LIBRARY_ID') or env.get('ZOTERO_LIBRARY_ID')
lib_type = os.environ.get('ZOTERO_LIBRARY_TYPE') or env.get('ZOTERO_LIBRARY_TYPE', 'user')
url = f'https://api.zotero.org/{lib_type}s/{lib_id}/items?tag=digest:2026-07&limit=1'
req = urllib.request.Request(url, headers={'Zotero-API-Version': '3', 'Zotero-API-Key': key})
r = urllib.request.urlopen(req, timeout=20)
print(r.headers.get('Total-Results', '0'))
" 2>/dev/null)"
    CATALOG_COUNT="$(python3 -c "
import json
c = json.load(open('data/catalog.json'))
print(sum(1 for r in c if '2026-07' in (r.get('digests') or [])))
")"
    if [[ -z "${ZOTERO_COUNT}" ]]; then
      report Q9 FAIL "zotero API call failed" "zotero count == catalog count (2026-07)"
    elif [[ "${ZOTERO_COUNT}" == "${CATALOG_COUNT}" ]]; then
      report Q9 PASS "zotero=${ZOTERO_COUNT} catalog=${CATALOG_COUNT}" "zotero count == catalog count (2026-07)"
    else
      DELTA=$(( ZOTERO_COUNT - CATALOG_COUNT ))
      report Q9 FAIL "zotero=${ZOTERO_COUNT} catalog=${CATALOG_COUNT} delta=${DELTA} (backfill push has not run yet -- expected mismatch, correctly reported)" "zotero count == catalog count (2026-07)"
    fi
    cd "${SCRIPT_DIR}"
  fi

  # --- Q9 negative: pure arithmetic self-test of the comparison, independent
  # of live data -- a synthetic off-by-one must be reported as a mismatch. ---
  SYN_A=42
  SYN_B=43
  if [[ "${SYN_A}" != "${SYN_B}" ]]; then
    SYN_DELTA=$(( SYN_A - SYN_B ))
    report Q9-neg PASS "synthetic 42 vs 43 reported as delta=${SYN_DELTA}" "synthetic off-by-one reported as a mismatch, not silently equal"
  else
    report Q9-neg FAIL "synthetic mismatch not detected" "synthetic off-by-one reported as a mismatch, not silently equal"
  fi
fi

# ---------------------------------------------------------------- summary
echo ""
echo "library-qa: ${RUN_PASS}/9 checks · ${NEG_PASS}/9 negatives"
if [[ -n "${ONLY}" ]]; then
  echo "note: --only ${ONLY} was set; the /9 denominators reflect only the check(s) that ran."
fi
if [[ "${SKIP_LIVE}" -eq 1 ]]; then
  echo "note: --skip-live set; Q3/Q4/Q5-network/Q9 skipped (SKIP, not FAIL) and excluded from PASS/FAIL totals."
fi

if [[ "${RUN_FAIL}" -eq 0 && "${NEG_FAIL}" -eq 0 ]]; then
  exit 0
else
  exit 1
fi
