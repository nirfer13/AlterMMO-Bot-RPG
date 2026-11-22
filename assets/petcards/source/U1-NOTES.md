## Zmiany w ramce U1 „Rubinowa Korona” (na prośbę właściciela, 2026-11-21)

`frames/U1.png` i `frames/U1-A.apng` różnią się od plików z paczki (`source/U1-rubinowa-korona.png`
i animacji A12 `source/A12-puls-rubinow.apng`). To ta sama czerwono-złota ramka co `frame-ultra.png`,
więc dostała te same poprawki, żeby U0, U1 i U1-A wyglądały jak jedna rodzina:

1. usunięty złoty kryształ pośrodku górnej belki i łuki z „soplami” pod nią,
2. ramka powiększona o ok. 5% i dosunięta do krawędzi - belki i narożniki poza medalionem są
   piksel w piksel takie jak w `frame-ultra.png`,
3. cienka złota linia wokół całego obrazka usunięta (po powiększeniu wypadłaby poza kadr;
   `frame-ultra.png` też jej nie ma),
4. medalion z koroną (krąg, korona, pierścień z gwiazdami) powiększony tak samo, ale
   przesunięty o ok. 13 px w prawo i 22 px w dół względem belek, żeby korona nie wychodziła poza
   kadr także wtedy, gdy w animacji się unosi; za nim zostaje kolczasty pierścień z U0,
5. animacja ma identyczną poprawkę w każdej klatce: części nieruchome to `U1.png` pomniejszone do
   512 px, z animacji wzięta jest tylko unosząca się korona z łańcuszkami i pulsującymi rubinami
   (84 klatki po 60 ms, pętla bez końca).

## Rubinowe tło za koroną (na prośbę właściciela, 2026-11-21)

Właściciel: „Może w rzadszej ramce rubinowej z koroną zamiast czarnego za koroną zrób rubinowy
kolor z cieniami.” Do tego zasada dla wszystkich ramek: styl gry Darkest Dungeon (gruby czarny
tusz, twarde czarne plamy cienia, płaskie chropowate pola koloru; bez miękkich gradientów,
rozmytych cieni i poświat).

Co się zmieniło - w `U1.png` i w każdej klatce `U1-A.apng`:

- czarny krąg za koroną jest teraz rubinowy i „malowany tuszem”: płaska, chropowata czerwień
  w tym samym odcieniu co czerwony medalion `frame-ultra.png` (106, 15, 19), ciemniejszy pas
  półcienia, czarne poszarpane pociągnięcia wcinające się od brzegu, wąski czarny sierp cienia
  po prawej, kilka jaśniejszych przetartych plam z lewej u góry i czarny obrys brzegu kręgu,
- korona (z łańcuszkami i rubinami) zachowuje czarny obrys ok. 5 px - na rubin zamienia się
  tylko tusz leżący dalej od złota, więc korona nie wygląda na wyciętą,
- korona rzuca twardy, lekko postrzępiony cień (ciemny rubin, nie czerń) w prawo w dół:
  jednolitą plamę o prostych bokach, wyraźnie wychodzącą poza czarny obrys korony (zwłaszcza
  na prawo od środkowego i prawego szpica), jak cień rzucony w rysunku tuszem;
  w animacji cień idzie za koroną i odsuwa się od niej trochę dalej, im wyżej korona się unosi
  (poprawka z drugiego przejścia tego samego dnia: pierwszy cień był przesunięty tylko o 7 i 9 px,
  chował się pod obrysem i było go widać tylko jako cienkie paski),
- wzór rubinu jest nieruchomy (ten sam w obrazku i we wszystkich klatkach) - ruszają się tylko
  korona, łańcuszki, rubiny i cień,
- pierścień z gwiazdami, jego ciemnoczerwone wnętrze, belki i wszystko poza kręgiem zostały bez
  zmian: w `U1.png` piksel w piksel (różnice sięgają najwyżej 2 px poza brzeg kręgu - wygładzona
  krawędź), w `U1-A.apng` poza kręgiem różnice najwyżej 3 poziomy jasności tuż przy jego brzegu
  (skutek skalowania do 512 px).

Wzór jest rysowany przez skrypt z ustalonym ziarnem losowania, więc każde uruchomienie daje te
same bajty.

### Jak zmienić wariant albo wrócić do czerni

Na górze `source/build_u1.py` jest stała `SURFACE`:

- `"B"` (użyty) - opisany wyżej, w duchu czerwonego medalionu U0,
- `"A"` - ciemny, winny rubin z ciężkim czarnym sierpem cienia po lewej u góry,
- `"C"` - jaśniejszy rubin z grubym czarnym sierpem na całej prawej stronie,
- `"black"` - powrót do czarnego kręgu sprzed tej zmiany (pliki wychodzą bajt w bajt takie
  jak wcześniej).

Po zmianie stałej trzeba uruchomić skrypt jeszcze raz. Podgląd innego wariantu bez zmiany stałej:
`python source/build_u1.py <folder> A` (zapisuje do podanego folderu, nie do `frames/`).

## Budowanie

`source/build_u1.py` odtwarza oba pliki z oryginałów (potrzebuje Pillow i numpy):
`python source/build_u1.py` (zapisuje do `frames/`; inny folder można podać jako argument).
Skrypt korzysta też z `frame-ultra.png` i `source/ultra-shiny.png`. Pliki są zapisywane pod
tymczasową nazwą i dopiero potem podmieniane, więc nikt nie przeczyta połowy pliku.

Żeby wrócić do oryginałów z paczki, wystarczy skopiować `source/U1-rubinowa-korona.png` na
`frames/U1.png` i `source/A12-puls-rubinow.apng` na `frames/U1-A.apng`.
