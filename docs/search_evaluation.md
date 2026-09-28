# Sökevaluering (FTS5 vs. direktsökning i chunktext)

| Sökterm | Kända dokument (direktscan) | Träffade av FTS | Missade | Status |
|---|---|---|---|---|
| Sommarteatern | 10 | 10 | 0 | OK |
| Gränby | 53 | 49 | 4 | MISS |
| Stenhagen | 49 | 41 | 8 | MISS |
| Fyrishov | 36 | 27 | 9 | MISS |
| Detaljplan | 87 | 50 | 37 | MISS |
| kollektivtrafik | 106 | 83 | 23 | MISS |
| massflyktsdirektivet | 5 | 4 | 1 | MISS |
| skyddsbehövande | 6 | 6 | 0 | OK |
| Evelina Solem | 53 | 53 | 0 | OK |
| Ehsan Nasari | 186 | 183 | 3 | MISS |
| arvodering | 1 | 1 | 0 | OK |
| Kulturhuset | 3 | 3 | 0 | OK |
| dnr 402-3236 | 3 | 3 | 0 | OK |
| dnr 4277-2023 | 2 | 2 | 0 | OK |
| dnr 2852–2016 | 3 | 3 | 0 | OK |

Total missade: **85** av 15 termer.

Kända orsaker till missade träffar (verifierade 2026-09-28, inte maskerade):
1. **Böjningsformer.** Alla saknade träffar beror på svenska böjningar:
   'Detaljplan' → 'detaljplanen/detaljplanerna/detaljplanering',
   'Fyrishov' → 'fyrishovs', 'massflyktsdirektivet' → '-ets'. FTS matchar
   hela ord; LIKE i kontrollen matchar delsträngar. Prefix-sökning
   (t.ex. `detaljplan*`) kringgår problemet redan idag. Svensk stemning
   är medvetet uppdragen tills sökkvalitetsarbetet prioriterar den —
   denna mätning är det belägg specen efterfrågar.
2. Inga träffar gick förlorade till OCR-luckor eller indexfel.
[
 {
  "term": "Sommarteatern",
  "expected": 10,
  "found": 10,
  "missed": []
 },
 {
  "term": "Gränby",
  "expected": 53,
  "found": 49,
  "missed": [
   74,
   107,
   389,
   455
  ]
 },
 {
  "term": "Stenhagen",
  "expected": 49,
  "found": 41,
  "missed": [
   39,
   48,
   147,
   173,
   238,
   255,
   291,
   515
  ]
 },
 {
  "term": "Fyrishov",
  "expected": 36,
  "found": 27,
  "missed": [
   84,
   225,
   257,
   259,
   260,
   261,
   288,
   371,
   520
  ]
 },
 {
  "term": "Detaljplan",
  "expected": 87,
  "found": 50,
  "missed": [
   4,
   13,
   24,
   31,
   37,
   48,
   54,
   74,
   87,
   104,
   112,
   120,
   139,
   146,
   158,
   172,
   193,
   201,
   220,
   237,
   254,
   256,
   257,
   260,
   267,
   309,
   345,
   350,
   388,
   406,
   421,
   438,
   472,
   485,
   493,
   512,
   515
  ]
 },
 {
  "term": "kollektivtrafik",
  "expected": 106,
  "found": 83,
  "missed": [
   27,
   47,
   50,
   79,
   84,
   87,
   133,
   142,
   168,
   204,
   233,
   248,
   309,
   374,
   408,
   417,
   422,
   438,
   443,
   467,
   468,
   485,
   515
  ]
 },
 {
  "term": "massflyktsdirektivet",
  "expected": 5,
  "found": 4,
  "missed": [
   112
  ]
 },
 {
  "term": "skyddsbehövande",
  "expected": 6,
  "found": 6,
  "missed": []
 },
 {
  "term": "Evelina Solem",
  "expected": 53,
  "found": 53,
  "missed": []
 },
 {
  "term": "Ehsan Nasari",
  "expected": 186,
  "found": 183,
  "missed": [
   386,
   411,
   449
  ]
 },
 {
  "term": "arvodering",
  "expected": 1,
  "found": 1,
  "missed": []
 },
 {
  "term": "Kulturhuset",
  "expected": 3,
  "found": 3,
  "missed": []
 },
 {
  "term": "dnr 402-3236",
  "expected": 3,
  "found": 3,
  "missed": []
 },
 {
  "term": "dnr 4277-2023",
  "expected": 2,
  "found": 2,
  "missed": []
 },
 {
  "term": "dnr 2852–2016",
  "expected": 3,
  "found": 3,
  "missed": []
 }
]
