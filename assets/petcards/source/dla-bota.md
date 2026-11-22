# Ramki i kosmetyki petów

Propozycja do akceptacji. Wagi procentowe są w `pule.json`. Dotyczą jednego egzemplarza po ustaleniu jego klasy.

| Klasa | Baza | Rzadsza odmiana statyczna | Inne statyczne | Animowane łącznie |
| --- | --- | --- | --- | --- |
| Shiny | S0 Srebrne okucia 84% | S1 Gwiazda Szronu 8% | R1 4%, R2 2%, R7 1% | 1% |
| Ultra Shiny | U0 Złoty relikwiarz 84% | U1 Rubinowa Korona 8% | R3 4%, R4 2%, R8 1% | 1% |
| Mityczny | M0 własny GIF 90% | — | R5 5%, R6 3%, R9 1% | 1% |

- Przy zdobyciu peta losuj jeden wynik z `framePools[klasa]`. Wagi sumują się do 100% w obrębie każdej klasy. Sufiks `-A` oznacza animowane wydanie konkretnej ramki. Przykładowo S1 ma 8%, a S1-A z animacją A11 ma 0,55%. Animacja ramki jest częścią tego samego wyniku. S0 i U0 to pierwsze oryginalne PNG. Mityczny używa własnego GIF-a w każdym wydaniu. Zwykły pet ma bazową ilustrację.
- Patron, wspierający YouTube, subskrybent Twitch lub crafter dostaje jeden rzut na ramkę w obrębie dostępnej puli i po jednym niezależnym rzucie na pieczęć i folię. Wystarczy jedna rola. Pozostali dostają wygląd bazowy swojej klasy. Statystyki wynikają z klasy peta.
- Pieczęć ma cztery rozłączne wyniki. Brak 85%, Tropiciela 10%, Łowcy 4,8%, Arcyłowcy 0,2%. **Mityczny również może mieć każdą z trzech pieczęci, jednocześnie z ramką i folią.**
- Folia ma trzy rozłączne wyniki. Brak 98,9%, częściowe Holo 1%, pełne Holo 0,1%. Częściowe działa na wybranych detalach peta. Pełne obejmuje powierzchnię karty. A7 oznacza częściowe Holo. Jeśli gra ma już flagę Holo, istniejące `true` oznacza pełne Holo.
- Zapisuj na egzemplarzu ID wyniku ramki (np. `S1-A`), `seal` i `foil`. Po transferze i wygaśnięciu wsparcia zachowaj kosmetyki. Po ewolucji zachowaj folię i pieczęć, a ramkę w obrębie tej samej klasy. Starszy egzemplarz bez ramki używa bazy swojej klasy. Renderuj kolejno peta, folię, jedną warstwę ramki i pieczęć. Dla statycznej ramki użyj PNG 1024 × 1024. Dla wyniku z sufiksem `-A` użyj pełnej przezroczystej ramki APNG 512 × 512 z pola `animations[n].file`. Tempo mitycznego GIF-a i tempo ramki licz względem czasu. Przy ruchomej warstwie wynikowym podglądem jest GIF.
