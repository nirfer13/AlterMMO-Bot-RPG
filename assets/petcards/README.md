# Grafiki kart towarzyszy

Warstwy, z których `altermmo/features/petcards.py` składa kartę towarzysza - także jajka
(ilustracja -> folia Holo -> ramka -> pieczęć). Źródło: paczka właściciela
`pieczecie-dla-bota.zip` (2026-11-20).

| Plik | Co to jest | Plik w paczce |
|---|---|---|
| `frame-shiny.png` | srebrne okucia (Shiny) | `shiny.png` |
| `frame-ultra.png` | złota ramka (Ultra Shiny) - **zmieniona, patrz niżej** | `ultra-shiny.png` |
| `seal-1.png` | pieczęć Tropiciela | `pieczec-bestiariusza.png` |
| `seal-2.png` | pieczęć Łowcy | `pieczec-lowcy.png` |
| `seal-3.png` | pieczęć Arcyłowcy | `pieczec-arcylowcy.png` |
| `holo.apng` | folia Holo, 64 klatki po 50 ms | `holo-folia.apng` |

Wszystkie PNG to przezroczyste nakładki 1024 x 1024.

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
rozmiaru i daty plików).
