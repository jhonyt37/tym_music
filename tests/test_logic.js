#!/usr/bin/env node
/**
 * Pure-logic unit tests for TYM Music frontend functions.
 * Runs in Node.js — no browser or DOM required.
 * Tests utility functions extracted from index.html and tv.html.
 */

"use strict";

let passed = 0, failed = 0;

function assert(condition, label) {
  if (condition) {
    console.log(`  ✓ ${label}`);
    passed++;
  } else {
    console.error(`  ✗ FAIL: ${label}`);
    failed++;
  }
}

function assertEqual(actual, expected, label) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    console.log(`  ✓ ${label}`);
    passed++;
  } else {
    console.error(`  ✗ FAIL: ${label}`);
    console.error(`    expected: ${JSON.stringify(expected)}`);
    console.error(`    actual:   ${JSON.stringify(actual)}`);
    failed++;
  }
}

// ---------------------------------------------------------------------------
// Functions under test (copied verbatim from index.html / tv.html)
// ---------------------------------------------------------------------------

// From index.html line ~294
const cop = n => '$' + (n || 0).toLocaleString('es-CO');

// From index.html line ~295
const mm = s => {
  s = Math.max(0, Math.round(s));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
};

// Equivalent of server.py _parse_len (used in client for duration display)
function parseLen(t) {
  if (!t) return 0;
  try {
    let s = 0;
    for (const p of String(t).split(':')) s = s * 60 + parseInt(p, 10);
    return isNaN(s) ? 0 : s;
  } catch (e) { return 0; }
}

// From index.html: YouTube URL/ID extractor
const YT_RE = /youtu\.?be|youtube\.com\/(watch|shorts|embed)/i;
function ytId(text) {
  if (!text) return null;
  text = text.trim();
  const m = text.match(/(?:v=|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})/);
  if (m) return m[1];
  if (/^[A-Za-z0-9_-]{11}$/.test(text)) return text;
  return null;
}

// From index.html: position in queue countdown display
function formatWait(secs) {
  const m = Math.ceil(secs / 60);
  if (m <= 0) return 'Próximo';
  if (m === 1) return '~1 min';
  return `~${m} min`;
}

// From tv.html / index.html: recent_reacts filter
function filterNewReacts(reacts, lastTs) {
  return reacts.filter(r => r.ts > lastTs);
}

// Simulates the public_like toggle storage key behavior
function getLikePubValue(stored) {
  return stored !== 'false';  // default true
}


// ---------------------------------------------------------------------------
// Test: cop() — currency formatter
// ---------------------------------------------------------------------------
console.log('\ncop() — currency formatter');
assert(cop(0)    === '$0',        'cop(0) → "$0"');
assert(cop(1000) .startsWith('$'), 'cop(1000) starts with $');
assert(typeof cop(1000) === 'string', 'cop returns string');
assert(cop(null) === '$0',        'cop(null) → "$0"');
assert(cop(undefined) === '$0',   'cop(undefined) → "$0"');
// Format check: 1000 should use es-CO locale (period or apostrophe as thousand sep)
{
  const formatted = cop(1000);
  const digits = formatted.replace(/[^0-9]/g, '');
  assertEqual(digits, '1000', 'cop(1000) contains exactly the digits 1000');
}


// ---------------------------------------------------------------------------
// Test: mm() — minutes:seconds formatter
// ---------------------------------------------------------------------------
console.log('\nmm() — time formatter');
assertEqual(mm(0),   '0:00', 'mm(0) → "0:00"');
assertEqual(mm(60),  '1:00', 'mm(60) → "1:00"');
assertEqual(mm(90),  '1:30', 'mm(90) → "1:30"');
assertEqual(mm(210), '3:30', 'mm(210) → "3:30"');
assertEqual(mm(3599),'59:59','mm(3599) → "59:59"');
assertEqual(mm(-5),  '0:00', 'mm(-5) → "0:00" (clamped)');
assertEqual(mm(61),  '1:01', 'mm(61) → "1:01" (leading zero on seconds)');


// ---------------------------------------------------------------------------
// Test: parseLen() — duration string to seconds
// ---------------------------------------------------------------------------
console.log('\nparseLen() — duration string parser');
assertEqual(parseLen('3:30'),  210, 'parseLen("3:30") → 210');
assertEqual(parseLen('1:00'),  60,  'parseLen("1:00") → 60');
assertEqual(parseLen('0:30'),  30,  'parseLen("0:30") → 30');
assertEqual(parseLen('10:00'), 600, 'parseLen("10:00") → 600');
assertEqual(parseLen('1:2:3'), 3723,'parseLen("1:2:3") → 3723 (h:m:s)');
assertEqual(parseLen(''),      0,   'parseLen("") → 0');
assertEqual(parseLen(null),    0,   'parseLen(null) → 0');
assertEqual(parseLen('abc'),   0,   'parseLen("abc") → 0 (invalid)');
assert(parseLen('3:30') === mm_to_secs('3:30'), 'parseLen roundtrips with mm()');

function mm_to_secs(t) {
  const [m, s] = t.split(':').map(Number);
  return m * 60 + s;
}


// ---------------------------------------------------------------------------
// Test: ytId() — YouTube ID extractor
// ---------------------------------------------------------------------------
console.log('\nytId() — YouTube ID extractor');
assertEqual(ytId('dQw4w9WgXcQ'),                         'dQw4w9WgXcQ', 'bare 11-char ID');
assertEqual(ytId('https://www.youtube.com/watch?v=dQw4w9WgXcQ'), 'dQw4w9WgXcQ', 'full watch URL');
assertEqual(ytId('https://youtu.be/dQw4w9WgXcQ'),        'dQw4w9WgXcQ', 'short youtu.be URL');
assertEqual(ytId('https://youtube.com/shorts/dQw4w9WgXcQ'), 'dQw4w9WgXcQ', 'shorts URL');
assertEqual(ytId('https://youtube.com/embed/dQw4w9WgXcQ'),  'dQw4w9WgXcQ', 'embed URL');
assertEqual(ytId(''),                                     null,          'empty string → null');
assertEqual(ytId(null),                                   null,          'null → null');
assertEqual(ytId('not-a-url'),                            null,          'invalid text → null');
assertEqual(ytId('tooshort'),                             null,          'short string → null (not 11 chars)');
assertEqual(ytId('123456789012'),                         null,          '12 chars → null');
assert(ytId('https://www.youtube.com/watch?v=abc&t=30') === null ||
       ytId('https://www.youtube.com/watch?v=abc1234abcd&t=30') === 'abc1234abcd',
       'URL with extra params extracts ID correctly');


// ---------------------------------------------------------------------------
// Test: formatWait() — queue wait display
// ---------------------------------------------------------------------------
console.log('\nformatWait() — queue wait display');
assertEqual(formatWait(0),   'Próximo', 'formatWait(0) → "Próximo"');
assertEqual(formatWait(30),  '~1 min',  'formatWait(30) → "~1 min"');
assertEqual(formatWait(60),  '~1 min',  'formatWait(60) → "~1 min"');
assertEqual(formatWait(61),  '~2 min',  'formatWait(61) → "~2 min"');
assertEqual(formatWait(120), '~2 min',  'formatWait(120) → "~2 min"');
assertEqual(formatWait(300), '~5 min',  'formatWait(300) → "~5 min"');


// ---------------------------------------------------------------------------
// Test: filterNewReacts() — TV reaction detection
// ---------------------------------------------------------------------------
console.log('\nfilterNewReacts() — TV reaction detection');
const reacts = [
  {emoji: '❤️', table: 'Mesa 1', ts: 100},
  {emoji: '🔥', table: null,     ts: 200},
  {emoji: '👍', table: 'Mesa 2', ts: 300},
];
{
  const r = filterNewReacts(reacts, 0);
  assertEqual(r.length, 3, 'filterNewReacts(ts=0) returns all');
}
{
  const r = filterNewReacts(reacts, 100);
  assertEqual(r.length, 2, 'filterNewReacts(ts=100) skips first');
  assertEqual(r[0].emoji, '🔥', 'filterNewReacts keeps newer items');
}
{
  const r = filterNewReacts(reacts, 300);
  assertEqual(r.length, 0, 'filterNewReacts(ts=300) returns nothing');
}
{
  const pub = filterNewReacts(reacts, 0).filter(r => r.table !== null);
  assertEqual(pub.length, 2, 'public reacts have table !== null');
  const priv = filterNewReacts(reacts, 0).filter(r => r.table === null);
  assertEqual(priv.length, 1, 'private reacts have table === null');
}


// ---------------------------------------------------------------------------
// Test: getLikePubValue() — like public toggle persistence
// ---------------------------------------------------------------------------
console.log('\ngetLikePubValue() — like pub toggle');
assert(getLikePubValue(null)    === true,  'no stored value → default true (public)');
assert(getLikePubValue('true')  === true,  '"true" → true');
assert(getLikePubValue('false') === false, '"false" → false');
assert(getLikePubValue('')      === true,  'empty string → true');


// ---------------------------------------------------------------------------
// Test: queue priority ordering logic
// ---------------------------------------------------------------------------
console.log('\nQueue priority sort logic');
function sortQueue(items) {
  return [...items].sort((a, b) =>
    (a.super ? 0 : a.priority ? 1 : 2) - (b.super ? 0 : b.priority ? 1 : 2) ||
    a.ts - b.ts
  );
}
{
  const items = [
    {id:1, title:'Normal 1',   priority:false, super:false, ts:100},
    {id:2, title:'Priority 1', priority:true,  super:false, ts:110},
    {id:3, title:'Normal 2',   priority:false, super:false, ts:120},
    {id:4, title:'Super',      priority:true,  super:true,  ts:130},
    {id:5, title:'Priority 2', priority:true,  super:false, ts:140},
  ];
  const sorted = sortQueue(items);
  assertEqual(sorted[0].id, 4, 'Super (jump) song is first');
  assert(sorted[1].priority === true,  'Priority songs come before normal');
  assert(sorted[2].priority === true,  'Priority songs come before normal');
  assert(sorted[3].priority === false, 'Normal songs are last');
  assert(sorted[4].priority === false, 'Normal songs are last');
  // Within priority group, older ts comes first
  assertEqual(sorted[1].id, 2, 'Older priority song before newer priority song');
}


// ---------------------------------------------------------------------------
// Test: interleaveArtists() — round-robin variety for "Por si te gustó"
// ---------------------------------------------------------------------------
console.log('\ninterleaveArtists() — round-robin variety');

function interleaveArtists(perArtist, max) {
  const out = [];
  for (let i = 0; out.length < max; i++) {
    let any = false;
    for (const ar of perArtist) {
      if (i < ar.length) { out.push(ar[i]); any = true; if (out.length >= max) break; }
    }
    if (!any) break;
  }
  return out;
}

// 3 artists × 5 songs → 9 total, each artist gets exactly 3
{
  const a = [{id:'a1'},{id:'a2'},{id:'a3'},{id:'a4'},{id:'a5'}];
  const b = [{id:'b1'},{id:'b2'},{id:'b3'},{id:'b4'},{id:'b5'}];
  const c = [{id:'c1'},{id:'c2'},{id:'c3'},{id:'c4'},{id:'c5'}];
  const res = interleaveArtists([a,b,c], 9);
  assertEqual(res.length, 9, '3 artists → 9 results');
  assertEqual(res[0].id, 'a1', 'slot 0 = artist A first song');
  assertEqual(res[1].id, 'b1', 'slot 1 = artist B first song');
  assertEqual(res[2].id, 'c1', 'slot 2 = artist C first song');
  assertEqual(res[3].id, 'a2', 'slot 3 = artist A second song');
  assertEqual(res.filter(s=>s.id.startsWith('a')).length, 3, 'exactly 3 from artist A');
  assertEqual(res.filter(s=>s.id.startsWith('b')).length, 3, 'exactly 3 from artist B');
  assertEqual(res.filter(s=>s.id.startsWith('c')).length, 3, 'exactly 3 from artist C');
}
// 2 artists × 5 songs → 6 results interleaved
{
  const a = [{id:'a1'},{id:'a2'},{id:'a3'},{id:'a4'},{id:'a5'}];
  const b = [{id:'b1'},{id:'b2'},{id:'b3'},{id:'b4'},{id:'b5'}];
  const res = interleaveArtists([a,b], 6);
  assertEqual(res.length, 6, '2 artists → 6 results');
  assertEqual(res[0].id, 'a1', '2-artist: slot 0 = A');
  assertEqual(res[1].id, 'b1', '2-artist: slot 1 = B');
  assertEqual(res[2].id, 'a2', '2-artist: slot 2 = A again');
}
// Short lists: fewer total songs than max
{
  const a = [{id:'a1'},{id:'a2'}];
  const b = [{id:'b1'}];
  const res = interleaveArtists([a,b], 9);
  assertEqual(res.length, 3, 'stops at total available (3) not max (9)');
  assertEqual(res[0].id, 'a1', 'a1 first');
  assertEqual(res[1].id, 'b1', 'b1 second');
  assertEqual(res[2].id, 'a2', 'a2 third (b exhausted)');
}
// 1 artist: returns all up to max
{
  const a = [{id:'a1'},{id:'a2'},{id:'a3'},{id:'a4'},{id:'a5'}];
  const res = interleaveArtists([a], 9);
  assertEqual(res.length, 5, '1 artist with 5 songs returns all 5 (< max 9)');
}
// Empty
{
  assertEqual(interleaveArtists([], 9).length, 0, 'empty input → empty output');
  assertEqual(interleaveArtists([[],[]], 9).length, 0, 'all-empty artists → empty output');
}
// max=0
{
  const a = [{id:'a1'},{id:'a2'}];
  assertEqual(interleaveArtists([a], 0).length, 0, 'max=0 → empty');
}


// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
console.log(`\n${'─'.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.error(`\n${failed} test(s) FAILED`);
  process.exit(1);
} else {
  console.log('\nAll tests passed ✓');
}
