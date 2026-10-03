# Klassificeringsutvärdering (stage D)

Grund: 55 manuell granskade taggar av 520 (urval: per ämne de 2 sämsta konfidenserna + 3 slumpmässiga, seed 2026). Bedömning per post: rätt/fel/osäkert med förslag på rätt ämne vid fel.

- Total precision: **86%** (38/44 rätt, 11 osäkra)

## Precision per ämne

| Ämne | Rätt | Fel | Precision |
|---|---|---|---|
| boende | 3 | 2 | 60% |
| ekonomi | 4 | 1 | 80% |
| kultur | 5 | 0 | 100% |
| omsorg | 5 | 0 | 100% |
| raddning | 4 | 1 | 80% |
| trafik | 5 | 0 | 100% |
| miljo | 3 | 0 | 100% |
| organisation | 2 | 1 | 67% |
| skola | 3 | 0 | 100% |
| stadsbyggnad | 2 | 1 | 67% |
| naringsliv | 2 | 0 | 100% |

## Förväxlingar (tilldelat → föreslaget)

- boende → **kultur** (2)
- ekonomi → **kultur** (1)
- organisation → **stadsbyggnad** (1)
- raddning → **omsorg** (1)
- stadsbyggnad → **kultur** (1)

## Konfidenskalibrering

| Konfidens | Rätt | Fel | Precision |
|---|---|---|---|
| <0.45 | 4 | 2 | 67% |
| >=0.45 | 34 | 4 | 89% |

## Felbedömda poster

- **boende → kultur** (0.54) 24. Interpellation om badhus i Gottsunda — Badhus är en fritidsanläggning – dubblett som inte vänt med i seedfixen
- **boende → kultur** (0.54) 33. Interpellation om badhus i Gottsunda — Samma interpellation (ompublicerad)
- **ekonomi → kultur** (0.50) 31. Interpellation om Stadsteaterns underskott — Stadsteaterns finansiering: ämnet är teatern, texten är dock ekonomitung
- **organisation → stadsbyggnad** (0.44) Interpellation om ett liberaliserat strandskydd från John Manders (M) — Strandskydd är en planfråga
- **raddning → omsorg** (0.57) 20. Interpellation om kvinnojourernas finansiering — Kvinnojourer = socialtjänst (jmf skyddade boenden)
- **stadsbyggnad → kultur** (0.37) 38.1. Svar interpellation om Saturnus i Uppsala — Uppsalas vetenskapshistoria/arv → kultur

## Osäkra bedömningar (räknas ej i precisionen)

- miljo (0.44) Svar på motion om att pausa målet om 100 procent ekologisk mat från Jennie Claes
- miljo (0.47) 39. Interpellation om matsvinn i skolan
- naringsliv (0.52) 10. Interpellation om att fler ska gå från bidrag till egen försörjning
- naringsliv (0.52) 23. Interpellation om att fler ska gå från bidrag till egen försörjning
- naringsliv (0.52) 32. Interpellation om att fler ska gå från bidrag till egen försörjning
- organisation (0.36) Interpellation om Saturnus från Jonas Segersam (KD)
- organisation (0.36) Svar på motion om att flagga för Uppsala istället för EU från Wouter Voortman (S
- skola (0.48) Interpellation om brandskyddet i Uppsalas skolor från Jennie Claesson (L)
- skola (0.57) 22. Interpellation om brandskyddet i Uppsalas skolor
- stadsbyggnad (0.32) Svar på motion om att flagga för Uppsala i stället för EU från Wouter Voortman (
- stadsbyggnad (0.60) Svar på motion om att flytta kommunala verksamheter från stad till landsbygd frå
