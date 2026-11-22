# Grafiki kart towarzyszy

Warstwy, z których `altermmo/features/petcards.py` składa kartę towarzysza - także jajka
(ilustracja -> folia Holo -> jedna warstwa ramki -> pieczęć). Opis zasad i zmierzone czasy / rozmiary
są w docstringu modułu.

## Paczka z 2026-11-20 (`pieczecie-dla-bota.zip`)

| Plik | Co to jest | Plik w paczce |
|---|---|---|
| `frame-shiny.png` | srebrne okucia - baza Shiny (`S0`) | `shiny.png` |
| `frame-ultra.png` | złota ramka - baza Ultra Shiny (`U0`) - **zmieniona, patrz niżej** | `ultra-shiny.png` |
| `seal-1.png` | pieczęć Tropiciela | `pieczec-bestiariusza.png` |
| `seal-2.png` | pieczęć Łowcy | `pieczec-lowcy.png` |
| `seal-3.png` | pieczęć Arcyłowcy | `pieczec-arcylowcy.png` |
| `holo.apng` | folia Holo, 64 klatki po 50 ms (pełne i częściowe Holo) | `holo-folia.apng` |

Wszystkie PNG to przezroczyste nakładki 1024 x 1024.

## Paczka z 2026-11-21 (`alterage-ramki-i-animacje-do-oceny`)

Rzadsze ramki każdej klasy. Pule, nazwy i procenty: `source/pule.json`, zasady: `source/dla-bota.md`
(oba skopiowane z paczki bez zmian; w kodzie: `petcards.POOLS`). Wynik z sufiksem `-A` to animowane
wydanie tej samej ramki - **zastępuje** statyczną ramkę (pełna przezroczysta ramka APNG 512 x 512,
84 klatki po 60 ms = 5040 ms, w pętli). Statyczne ramki to PNG 1024 x 1024 jak wyżej. Mityczny `M0`
nie ma pliku - pet ma własny GIF.

| Wynik | Nazwa | Plik | Plik w paczce |
|---|---|---|---|
| `S1` / `S1-A` | Gwiazda Szronu | `frames/S1.png` / `frames/S1-A.apng` | `ramki/S1-gwiazda-szronu.png` / `animacje/A11-szlif-szafiru.apng` |
| `R1` / `R1-A` | Księżycowa stal | `frames/R1.png` / `frames/R1-A.apng` | `ramki/R1-ksiezycowa-stal.png` / `animacje/A1-zimny-szlif.apng` |
| `R2` / `R2-A` | Miedziane ciernie | `frames/R2.png` / `frames/R2-A.apng` | `ramki/R2-miedziane-ciernie.png` / `animacje/A2-kwitnacy-ciern.apng` |
| `R7` / `R7-A` | Przeszywana skóra | `frames/R7.png` / `frames/R7-A.apng` | `ramki/R7-przeszywana-skora.png` / `animacje/A8-srebrna-nic.apng` |
| `U1` / `U1-A` | Rubinowa Korona | `frames/U1.png` / `frames/U1-A.apng` | **przerobione, patrz niżej** |
| `R3` / `R3-A` | Złote wrota | `frames/R3.png` / `frames/R3-A.apng` | `ramki/R3-zlote-wrota.png` / `animacje/A3-platki-zlota.apng` |
| `R4` / `R4-A` | Rozbite zwierciadło | `frames/R4.png` / `frames/R4-A.apng` | `ramki/R4-rozbite-zwierciadlo.png` / `animacje/A4-pekniety-pryzmat.apng` |
| `R8` / `R8-A` | Bursztynowy skarabeusz | `frames/R8.png` / `frames/R8-A.apng` | `ramki/R8-bursztynowy-skarabeusz.png` / `animacje/A9-oddech-bursztynu.apng` |
| `R5` / `R5-A` | Astrolabium | `frames/R5.png` / `frames/R5-A.apng` | `ramki/R5-astrolabium.png` / `animacje/A5-korona-zacmienia.apng` |
| `R6` / `R6-A` | Kręgosłup Pustki | `frames/R6.png` / `frames/R6-A.apng` | `ramki/R6-kregoslup-pustki.png` / `animacje/A6-zyly-pustki.apng` |
| `R9` / `R9-A` | Oko Otchłani | `frames/R9.png` / `frames/R9-A.apng` | `ramki/R9-oko-otchlani.png` / `animacje/A10-spojrzenie-otchlani.apng` |

Pliki poza `U1` są kopiami plików z paczki bez zmian (sprawdzone sumą SHA-256). Folder `poprzednie/`
paczki (starsze wersje) nie jest używany.

**Częściowe Holo** (wynik rzutu na folię, 1%) nie ma osobnego pliku: to ta sama folia `holo.apng`,
widoczna tylko przez maskę detali towarzysza, którą bot liczy z samego obrazka (jasne krawędzie,
pęknięcia, klejnoty blisko środka). Ręcznie malowane przykłady z paczki (`A7-holo-czesciowe-kot.apng`,
`A7-holo-czesciowe-pustka.apng`) posłużyły tylko do dostrojenia tej maski i nie są w grze; liczby
porównania są w docstringu `petcards.py`.

### Rubinowa Korona (`U1`, `U1-A`) - przerobiona jak złota ramka

Ta ramka to wariant złotej ramki `U0` i miała te same elementy, które właściciel kazał usunąć
2026-11-20 (kryształ pośrodku górnej belki, łuki z „soplami” pod nią, stara geometria).
`frames/U1.png` i `frames/U1-A.apng` są przerobionymi wersjami, które tworzy
`source/build_u1.py` z oryginałów z paczki: `source/U1-rubinowa-korona.png` (statyczna) i
`source/A12-puls-rubinow.apng` (animacja A12 „Koronacja”).

## Zmiany w złotej ramce (na prośbę właściciela, 2026-11-20)

`frame-ultra.png` różni się od `ultra-shiny.png` z paczki:

1. usunięty złoty kryształ pośrodku górnej belki (zasłaniał twarz towarzysza),
2. usunięte łuki z czterema zwisającymi „soplami” pod górną belką - została sama belka,
3. cała ramka powiększona o ok. 5% i dosunięta do krawędzi obrazka (belki leżą na samym brzegu;
   zewnętrzne kolce narożników wychodzą poza kadr).

Oryginał leży w `source/ultra-shiny.png`, a `source/build_frame.py` odtwarza z niego obecną ramkę
(potrzebuje Pillow i numpy: `python source/build_frame.py source/ultra-shiny.png frame-ultra.png`).
Żeby wrócić do oryginału, wystarczy skopiować `source/ultra-shiny.png` na `frame-ultra.png`.

Po podmianie któregokolwiek pliku karty przerysują się same (ich pamięć podręczna zależy od
rozmiaru i daty każdego użytego pliku).
